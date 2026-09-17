"""AudioSocket TCP server.

Asterisk connects OUT to us (verified: host.docker.internal is the only
address reachable from the container). The first frame carries the UUID we
passed to ARI, which identifies the call.

Outbound audio is primed with a small jitter buffer, then paced to real time.
Asterisk does not throttle our writes, so dumping an entire utterance would
buffer speech the caller has not heard yet -- breaking both interruption and
the accounting of what was heard.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from typing import AsyncIterator, Callable, Iterable

from phonesys.audiosocket_codec import (
    FRAME_MS, TYPE_AUDIO, TYPE_UUID, FrameDecoder, encode,
)

logger = logging.getLogger(__name__)

# Keep a small amount of audio queued in Asterisk so ordinary asyncio/WSL/
# Docker scheduling jitter cannot starve playback between 20ms frames.  This
# is deliberately modest: a large buffer would make barge-in feel sluggish.
# Eight 20ms frames gives the WSL -> Docker -> Asterisk path 160ms of
# scheduling headroom.  The former four-frame/80ms cushion was small enough
# that an ordinary WSL or Docker scheduling stall could drain Asterisk's
# queue and produce the short gaps callers described as choppy speech.  This
# remains well below a conversationally significant delay and only adds at
# most 80ms to barge-in compared with the previous setting.
OUTBOUND_PREBUFFER_FRAMES = 8
LATE_WAKEUP_SECONDS = 0.005


class AudioSocketSession:
    def __init__(self, uuid: str, writer) -> None:
        self.uuid = uuid
        self._writer = writer
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._sending = False
        self._frame_interval = FRAME_MS / 1000.0

    # No feed()/decoder here: decoding happens once per CONNECTION, in
    # AudioSocketServer._handle, because frames arrive before the UUID frame
    # has created a session to hand them to. A second decoder on the session
    # was dead code that tests were pointed at, which left the real inbound
    # route untested -- see tests/test_audiosocket.py.

    def deliver(self, payload: bytes) -> None:
        """Queue one decoded inbound audio payload for frames()."""
        self._inbound.put_nowait(payload)

    def close(self) -> None:
        self._closed = True
        self._inbound.put_nowait(None)

    async def frames(self) -> AsyncIterator[bytes]:
        while True:
            item = await self._inbound.get()
            if item is None:
                return
            yield item

    def stop_sending(self) -> None:
        self._sending = False

    async def send_frames(
        self,
        frames: Iterable[tuple[bytes, int]],
        on_sent: Callable[[int], None],
    ) -> None:
        """Write (payload, sentence_index) pairs with a small initial cushion.

        Paced to real time: Asterisk does not throttle our writes, so
        dumping frames as fast as they're produced would let it buffer
        speech the caller has not heard yet, breaking both the ability to
        interrupt the agent and the accounting of what was actually heard.
        """
        self._sending = True
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        frames_sent = 0
        late_wakeups = 0
        max_lateness = 0.0
        cushion_underruns = 0
        for frame_number, (payload, sentence_index) in enumerate(frames):
            if not self._sending or self._closed:
                break
            try:
                self._writer.write(encode(TYPE_AUDIO, payload))
                await self._writer.drain()
            except Exception as exc:
                # A caller hanging up mid-sentence is the normal case, not
                # an edge case: the writer can be closed by _handle's
                # cleanup while we're sitting in drain(). Stop like
                # stop_sending() would, without raising -- but only credit
                # on_sent for frames that were actually written before this
                # one, since that's the record of what the caller heard.
                logger.info("audiosocket send failed for %s, stopping: %s", self.uuid, exc)
                self._sending = False
                self._closed = True
                break
            frames_sent += 1
            on_sent(sentence_index)
            # Prime Asterisk with 160ms, then maintain that lead at real-time
            # cadence.  Without this cushion a single late event-loop wakeup
            # becomes an audible gap on the PSTN call.
            if frame_number < OUTBOUND_PREBUFFER_FRAMES - 1:
                continue
            next_at += self._frame_interval
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            lateness = max(0.0, loop.time() - next_at)
            max_lateness = max(max_lateness, lateness)
            if lateness >= LATE_WAKEUP_SECONDS:
                late_wakeups += 1
            if lateness >= OUTBOUND_PREBUFFER_FRAMES * self._frame_interval:
                cushion_underruns += 1
        self._sending = False
        logger.info(
            "audiosocket pacing %s: frames=%s max_late_ms=%.1f "
            "late_ticks=%s cushion_underruns=%s",
            self.uuid, frames_sent, max_lateness * 1000.0,
            late_wakeups, cushion_underruns,
        )


class AudioSocketServer:
    # Generous relative to the 20ms inbound frame cadence: a healthy call
    # sends audio continuously, so anything this quiet means the peer
    # opened a connection and went silent (no UUID frame ever arrived, or
    # it hung mid-call) rather than being merely between frames.
    DEFAULT_READ_TIMEOUT = 30.0

    def __init__(
        self,
        host: str = "0.0.0.0",
        port: int = 9010,
        read_timeout: float = DEFAULT_READ_TIMEOUT,
    ) -> None:
        self._host, self._port = host, port
        self._read_timeout = read_timeout
        self._server = None
        self._sessions: dict[str, AudioSocketSession] = {}
        self._waiters: dict[str, asyncio.Future] = {}

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        logger.info("audiosocket listening on %s:%s", self._host, self._port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def wait_for_session(self, uuid: str, timeout: float = 10.0) -> AudioSocketSession:
        existing = self._sessions.get(uuid)
        if existing is not None:
            return existing
        fut = asyncio.get_running_loop().create_future()
        self._waiters[uuid] = fut
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._waiters.pop(uuid, None)

    async def _handle(self, reader, writer) -> None:
        # One decoder for the life of the connection. Before the UUID frame
        # arrives there is no session yet to hand frames to, so we route
        # each decoded frame here rather than duplicating the decode step
        # both before and after the session exists.
        decoder = FrameDecoder()
        session: AudioSocketSession | None = None
        try:
            while True:
                # Explicit timeout: without it, a connection that opens but
                # never sends a UUID frame (or goes silent mid-call) blocks
                # this task forever. A timeout here just means the peer is
                # gone -- close up and exit, same as an ordinary hangup.
                try:
                    data = await asyncio.wait_for(reader.read(4096), timeout=self._read_timeout)
                except asyncio.TimeoutError:
                    logger.info("audiosocket read timed out; treating peer as gone")
                    break
                if not data:
                    break
                for frame_type, payload in decoder.feed(data):
                    if frame_type == TYPE_UUID:
                        call_uuid = str(uuid.UUID(bytes=payload))
                        logger.info("audiosocket session established for %s", call_uuid)
                        session = AudioSocketSession(call_uuid, writer)
                        self._sessions[call_uuid] = session
                        waiter = self._waiters.get(call_uuid)
                        if waiter is not None and not waiter.done():
                            waiter.set_result(session)
                    elif frame_type == TYPE_AUDIO and session is not None:
                        session.deliver(payload)
        except Exception:
            logger.exception("audiosocket connection failed")
        finally:
            if session is not None:
                session.close()
                self._sessions.pop(session.uuid, None)
            writer.close()
