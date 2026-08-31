import asyncio
import time
import uuid as uuid_mod

from phonesys.audiosocket import AudioSocketServer, AudioSocketSession
from phonesys.audiosocket_codec import TYPE_AUDIO, TYPE_UUID, encode


class FakeWriter:
    def __init__(self): self.chunks = []
    def write(self, data): self.chunks.append((time.perf_counter(), data))
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass


class RecordingWriter:
    """Tracks whether close() was called, for _handle cleanup assertions."""
    def __init__(self):
        self.chunks = []
        self.closed = False
    def write(self, data): self.chunks.append(data)
    async def drain(self): pass
    def close(self): self.closed = True
    async def wait_closed(self): pass


class FailAfterWriter:
    """Raises on the Nth write() call, simulating a mid-call disconnect."""
    def __init__(self, fail_at):
        self.chunks = []
        self._fail_at = fail_at
        self._count = 0
    def write(self, data):
        self._count += 1
        if self._count == self._fail_at:
            raise ConnectionResetError("peer hung up")
        self.chunks.append(data)
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass


class ScriptedReader:
    """Returns a fixed sequence of byte chunks, then b"" (EOF) forever."""
    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, n):
        await asyncio.sleep(0)
        return self._chunks.pop(0) if self._chunks else b""


class HangingReader:
    """Never returns data -- simulates a connection that opens but never
    sends anything (e.g. no UUID frame ever arrives)."""
    async def read(self, n):
        await asyncio.sleep(3600)


async def test_paces_outbound_frames_to_real_time():
    """Load-bearing: dumping frames makes Asterisk buffer speech the caller
    has not heard, which breaks both barge-in and heard-tracking."""
    w = FakeWriter()
    s = AudioSocketSession("u", w)
    s._frame_interval = 0.02
    frames = [(b"\x00" * 320, 0) for _ in range(10)]
    t0 = time.perf_counter()
    await s.send_frames(frames, on_sent=lambda idx: None)
    elapsed = time.perf_counter() - t0
    assert elapsed >= 0.05, f"10 frames sent in {elapsed:.3f}s -- not paced"
    assert len(w.chunks) == 10


async def test_primes_a_small_outbound_jitter_buffer_before_pacing():
    """The first eight frames are queued together to prevent audible gaps."""
    w = FakeWriter()
    s = AudioSocketSession("u", w)
    s._frame_interval = 0.02

    await s.send_frames(
        [(b"\x00" * 320, 0) for _ in range(10)],
        on_sent=lambda idx: None,
    )

    first_eight = [stamp for stamp, _ in w.chunks[:8]]
    assert max(first_eight) - min(first_eight) < 0.01
    assert w.chunks[8][0] - w.chunks[7][0] >= 0.015


async def test_stop_sending_halts_mid_stream():
    w = FakeWriter()
    s = AudioSocketSession("u", w)
    s._frame_interval = 0.01
    sent = []
    frames = [(b"\x00" * 320, i) for i in range(20)]

    async def stop_soon():
        await asyncio.sleep(0.03)
        s.stop_sending()

    await asyncio.gather(s.send_frames(frames, on_sent=sent.append), stop_soon())
    assert 0 < len(sent) < 20, f"expected partial send, got {len(sent)}"


async def test_on_sent_reports_sentence_index():
    w = FakeWriter()
    s = AudioSocketSession("u", w)
    s._frame_interval = 0
    sent = []
    await s.send_frames([(b"\x00" * 320, 7), (b"\x00" * 320, 8)], on_sent=sent.append)
    assert sent == [7, 8]


async def test_inbound_audio_frames_are_yielded():
    """Drives the REAL inbound route: _handle decodes, hands payloads to the
    session, frames() yields them.

    This used to call a session-level feed()/decoder that _handle never
    touched, so the shipped path had no test at all while the suite looked
    green.
    """
    server = AudioSocketServer(read_timeout=1.0)
    call_uuid = uuid_mod.uuid4()
    # A frame deliberately split across two reads, to prove the decoder is
    # per-connection and survives TCP chunking.
    audio = encode(TYPE_AUDIO, b"\x01" * 320) + encode(TYPE_AUDIO, b"\x02" * 320)
    reader = ScriptedReader([
        encode(TYPE_UUID, call_uuid.bytes) + audio[:100],
        audio[100:],
        b"",                                  # EOF: peer closed
    ])
    handling = asyncio.create_task(server._handle(reader, RecordingWriter()))
    session = await asyncio.wait_for(
        server.wait_for_session(str(call_uuid), timeout=2.0), timeout=2.0
    )
    await asyncio.wait_for(handling, timeout=2.0)   # runs to EOF, then close()

    assert session.uuid == str(call_uuid)
    got = [f async for f in session.frames()]       # close() already queued EOF
    assert got == [b"\x01" * 320, b"\x02" * 320]


async def test_real_socket_delivers_inbound_frames_to_the_waiting_handler():
    """End-to-end over a real loopback TCP connection: bind, connect, send a
    UUID frame plus audio, and assert the frames reach the session that
    wait_for_session() hands to StreamingCallHandler.

    Everything between "Asterisk connects out to us" and "the pump gets a
    320-byte frame" runs here for real -- start_server, the read loop, the
    frame decoder, session registration and the waiter handoff.
    """
    server = AudioSocketServer(host="127.0.0.1", port=0, read_timeout=2.0)
    await server.start()
    try:
        port = server._server.sockets[0].getsockname()[1]
        call_uuid = uuid_mod.uuid4()

        waiting = asyncio.create_task(
            server.wait_for_session(str(call_uuid), timeout=5.0)
        )
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        try:
            writer.write(encode(TYPE_UUID, call_uuid.bytes))
            # Two audio frames in one TCP write: the decoder must split them,
            # and the split must not depend on how the kernel chunks the data.
            writer.write(encode(TYPE_AUDIO, b"\xaa" * 320))
            writer.write(encode(TYPE_AUDIO, b"\xbb" * 320))
            await writer.drain()

            session = await asyncio.wait_for(waiting, timeout=5.0)
            frames = session.frames()
            first = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            second = await asyncio.wait_for(frames.__anext__(), timeout=5.0)
            assert (first, second) == (b"\xaa" * 320, b"\xbb" * 320)

            # ...and outbound audio is really written back down the socket.
            await session.send_frames([(b"\xcc" * 320, 0)], on_sent=lambda i: None)
            echoed = await asyncio.wait_for(reader.readexactly(323), timeout=5.0)
            assert echoed == encode(TYPE_AUDIO, b"\xcc" * 320)
            await frames.aclose()
        finally:
            writer.close()
    finally:
        await server.stop()


async def test_send_frames_stops_cleanly_when_writer_raises_mid_stream():
    """A caller hanging up mid-sentence is normal, not exceptional: the
    writer can fail while send_frames is in flight and this must not
    propagate a traceback. Frames written before the failure still count."""
    w = FailAfterWriter(fail_at=3)
    s = AudioSocketSession("u", w)
    s._frame_interval = 0
    sent = []
    frames = [(b"\x00" * 320, i) for i in range(5)]

    await s.send_frames(frames, on_sent=sent.append)  # must not raise

    assert sent == [0, 1], f"expected only frames written before the failure, got {sent}"
    assert s._sending is False
    assert s._closed is True


async def test_handle_cleans_up_when_uuid_never_arrives():
    """A connection that opens but never sends a UUID frame must not block
    its handler task forever -- the read timeout treats it as gone."""
    server = AudioSocketServer(read_timeout=0.05)
    writer = RecordingWriter()
    reader = HangingReader()

    # Bounded by an outer timeout so a regression (no read timeout) fails
    # the test quickly instead of hanging the suite.
    await asyncio.wait_for(server._handle(reader, writer), timeout=2.0)

    assert writer.closed is True
    assert server._sessions == {}
