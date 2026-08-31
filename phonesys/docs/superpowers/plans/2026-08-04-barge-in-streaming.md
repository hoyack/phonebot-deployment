# Barge-In Streaming Pipeline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Dial extension 7001 and hold a conversation you can interrupt — speak over the agent and it stops, the way a person would.

**Architecture:** A new `StreamingCallHandler` bridges the caller with an AudioSocket channel, giving continuous duplex 8 kHz PCM over TCP. Inbound audio streams to Deepgram's live WebSocket for transcription and voice-activity detection; Hermes replies stream back sentence by sentence through ElevenLabs and out to the caller, paced to real time. Confirmed speech during playback cancels the reply and truncates conversation history to what was actually heard. Extension 7000 keeps its turn-based pipeline untouched.

**Tech Stack:** Python 3.14, `httpx`, `websockets`, `asyncio`. No new dependencies.

## Global Constraints

- Extension 7000 must keep working. `call.py`, `recordings.py`, and `pipeline.py` are not modified.
- Secrets stay in `.env`; never read, print, log, or commit their values.
- All facts below were measured on this host on 2026-08-04 — do not "correct" them from memory:
  - AudioSocket framing is `[type:1][len:2 big-endian][payload]`.
  - `0x01` = UUID (16 raw bytes). `0x10` = audio (320 bytes = 20 ms slin 8 kHz mono).
  - ARI origination **must** pass `formats=slin`. `format` (singular) is an `externalMedia` parameter, is silently ignored here, and the driver then negotiates `slin192` — 7680-byte frames at 375 KB/s.
  - Asterisk paces inbound audio at 20 ms. Outbound is not throttled by Asterisk; we must pace it.
  - Hermes `/chat/stream` is SSE. Reply text is `assistant.delta` events only. **`tool.progress` also carries a `delta` field** — filter on event name, never on the presence of `delta`.
  - AudioSocket port is **9010**; 9000 is taken by a minio container.
- Every outbound network call carries an explicit timeout.

---

### Task 1: AudioSocket frame codec

**Files:**
- Create: `src/phonesys/audiosocket_codec.py`
- Create: `tests/test_audiosocket_codec.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `TYPE_TERMINATE=0x00`, `TYPE_UUID=0x01`, `TYPE_AUDIO=0x10`, `TYPE_ERROR=0xff`; `encode(frame_type: int, payload: bytes) -> bytes`; `FrameDecoder` with `feed(data: bytes) -> list[tuple[int, bytes]]`; `FrameError`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_audiosocket_codec.py`:

```python
import pytest
from phonesys.audiosocket_codec import (
    TYPE_AUDIO, TYPE_UUID, encode, FrameDecoder, FrameError,
)


def test_encode_builds_type_length_payload():
    assert encode(TYPE_AUDIO, b"\x01\x02") == b"\x10\x00\x02\x01\x02"


def test_decode_single_frame():
    d = FrameDecoder()
    assert d.feed(b"\x10\x00\x02\x01\x02") == [(TYPE_AUDIO, b"\x01\x02")]


def test_uuid_frame_is_sixteen_raw_bytes():
    # Measured: Asterisk sends the UUID as 16 binary bytes, not text.
    raw = bytes(range(16))
    d = FrameDecoder()
    assert d.feed(encode(TYPE_UUID, raw)) == [(TYPE_UUID, raw)]


def test_frame_split_across_reads_is_reassembled():
    d = FrameDecoder()
    whole = encode(TYPE_AUDIO, b"abcd")
    assert d.feed(whole[:2]) == []
    assert d.feed(whole[2:5]) == []
    assert d.feed(whole[5:]) == [(TYPE_AUDIO, b"abcd")]


def test_multiple_frames_in_one_read():
    d = FrameDecoder()
    data = encode(TYPE_AUDIO, b"aa") + encode(TYPE_AUDIO, b"bb")
    assert d.feed(data) == [(TYPE_AUDIO, b"aa"), (TYPE_AUDIO, b"bb")]


def test_realistic_320_byte_audio_frame():
    # Measured frame size: 20ms of slin 8kHz mono.
    payload = b"\x00\x01" * 160
    d = FrameDecoder()
    frames = d.feed(encode(TYPE_AUDIO, payload))
    assert frames == [(TYPE_AUDIO, payload)]
    assert len(frames[0][1]) == 320


def test_oversized_length_is_rejected():
    d = FrameDecoder()
    with pytest.raises(FrameError):
        d.feed(b"\x10\xff\xff" + b"x" * 10)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_audiosocket_codec.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.audiosocket_codec'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/audiosocket_codec.py`:

```python
"""AudioSocket wire format.

Measured against Asterisk 21.10.2 on 2026-08-04:
    [type:1][length:2 big-endian][payload]
    0x01 UUID  -> 16 raw bytes
    0x10 audio -> 320 bytes (20ms of slin, 8kHz, mono)
"""
from __future__ import annotations

TYPE_TERMINATE = 0x00
TYPE_UUID = 0x01
TYPE_AUDIO = 0x10
TYPE_ERROR = 0xFF

MAX_PAYLOAD = 8192
AUDIO_FRAME_BYTES = 320
FRAME_MS = 20


class FrameError(RuntimeError):
    """The peer sent something that is not a valid frame."""


def encode(frame_type: int, payload: bytes) -> bytes:
    if len(payload) > MAX_PAYLOAD:
        raise FrameError(f"payload too large: {len(payload)}")
    return bytes([frame_type]) + len(payload).to_bytes(2, "big") + payload


class FrameDecoder:
    """Accumulates bytes and yields whole frames as they complete."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, bytes]]:
        self._buf.extend(data)
        out: list[tuple[int, bytes]] = []
        while len(self._buf) >= 3:
            frame_type = self._buf[0]
            length = int.from_bytes(self._buf[1:3], "big")
            if length > MAX_PAYLOAD:
                raise FrameError(f"declared length {length} exceeds maximum")
            if len(self._buf) < 3 + length:
                break
            out.append((frame_type, bytes(self._buf[3:3 + length])))
            del self._buf[:3 + length]
        return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_audiosocket_codec.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/audiosocket_codec.py tests/test_audiosocket_codec.py
git commit -m "feat: AudioSocket frame codec"
```

---

### Task 2: mu-law to linear conversion

ElevenLabs offers `ulaw_8000` but no `pcm_8000`; AudioSocket needs slin. G.711 expansion is a 256-entry table, which avoids a resampler and avoids `audioop` (removed in Python 3.13+).

**Files:**
- Create: `src/phonesys/ulaw.py`
- Create: `tests/test_ulaw.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `ulaw_to_slin(data: bytes) -> bytes` (1 byte in → 2 bytes out, little-endian signed 16-bit).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_ulaw.py`:

```python
import struct
from phonesys.ulaw import ulaw_to_slin


def _samples(b: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(b) // 2}h", b))


def test_output_is_two_bytes_per_input_byte():
    assert len(ulaw_to_slin(bytes(160))) == 320


def test_known_g711_values():
    # Standard G.711 mu-law anchors.
    assert _samples(ulaw_to_slin(bytes([0xFF]))) == [0]        # +zero
    assert _samples(ulaw_to_slin(bytes([0x7F]))) == [0]        # -zero
    assert _samples(ulaw_to_slin(bytes([0x00]))) == [-32124]   # most negative
    assert _samples(ulaw_to_slin(bytes([0x80]))) == [32124]    # most positive


def test_silence_maps_to_silence():
    assert _samples(ulaw_to_slin(bytes([0xFF] * 8))) == [0] * 8


def test_empty_input():
    assert ulaw_to_slin(b"") == b""
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ulaw.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.ulaw'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/ulaw.py`:

```python
"""G.711 mu-law expansion to signed 16-bit linear PCM.

A 256-entry table, so no resampling and no audioop (removed in Python 3.13+).
ElevenLabs emits ulaw_8000; AudioSocket wants slin at the same 8kHz rate, so
this is a pure per-sample widening with no rate conversion.
"""
from __future__ import annotations

import struct


def _decode(u: int) -> int:
    u = ~u & 0xFF
    sign = u & 0x80
    exponent = (u >> 4) & 0x07
    mantissa = u & 0x0F
    sample = (((mantissa << 3) + 0x84) << exponent) - 0x84
    return -sample if sign else sample


_TABLE: list[bytes] = [struct.pack("<h", _decode(i)) for i in range(256)]


def ulaw_to_slin(data: bytes) -> bytes:
    return b"".join(_TABLE[b] for b in data)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ulaw.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/ulaw.py tests/test_ulaw.py
git commit -m "feat: mu-law to linear PCM conversion"
```

---

### Task 3: AudioSocket server with paced writer

**Files:**
- Create: `src/phonesys/audiosocket.py`
- Create: `tests/test_audiosocket.py`

**Interfaces:**
- Consumes: `audiosocket_codec`.
- Produces: `AudioSocketServer(host: str, port: int)` with `async start()`, `async stop()`, `async wait_for_session(uuid: str, timeout: float) -> AudioSocketSession`; `AudioSocketSession` with `async frames() -> AsyncIterator[bytes]` (inbound 320-byte audio), `async send_frames(frames, on_sent)`, `stop_sending()`, `close()`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_audiosocket.py`:

```python
import asyncio
import time

from phonesys.audiosocket import AudioSocketSession
from phonesys.audiosocket_codec import TYPE_AUDIO, encode


class FakeWriter:
    def __init__(self): self.chunks = []
    def write(self, data): self.chunks.append((time.perf_counter(), data))
    async def drain(self): pass
    def close(self): pass
    async def wait_closed(self): pass


async def test_paces_outbound_frames_to_real_time():
    """Load-bearing: dumping frames makes Asterisk buffer speech the caller
    has not heard, which breaks both barge-in and heard-tracking."""
    w = FakeWriter()
    s = AudioSocketSession("u", w)
    s._frame_interval = 0.02
    frames = [(b"\x00" * 320, 0) for _ in range(5)]
    t0 = time.perf_counter()
    await s.send_frames(frames, on_sent=lambda idx: None)
    elapsed = time.perf_counter() - t0
    assert elapsed >= 0.06, f"5 frames sent in {elapsed:.3f}s -- not paced"
    assert len(w.chunks) == 5


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
    w = FakeWriter()
    s = AudioSocketSession("u", w)
    s.feed(encode(TYPE_AUDIO, b"\x01" * 320))
    s.feed(encode(TYPE_AUDIO, b"\x02" * 320))
    s.close()
    got = [f async for f in s.frames()]
    assert got == [b"\x01" * 320, b"\x02" * 320]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_audiosocket.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.audiosocket'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/audiosocket.py`:

```python
"""AudioSocket TCP server.

Asterisk connects OUT to us (verified: host.docker.internal is the only
address reachable from the container). The first frame carries the UUID we
passed to ARI, which identifies the call.

Outbound audio is paced to real time. Asterisk does not throttle our writes,
so dumping frames would buffer speech the caller has not heard yet -- breaking
both interruption and the accounting of what was heard.
"""
from __future__ import annotations

import asyncio
import logging
from typing import AsyncIterator, Callable, Iterable

from phonesys.audiosocket_codec import (
    AUDIO_FRAME_BYTES, FRAME_MS, TYPE_AUDIO, TYPE_UUID, FrameDecoder, encode,
)

logger = logging.getLogger(__name__)


class AudioSocketSession:
    def __init__(self, uuid: str, writer) -> None:
        self.uuid = uuid
        self._writer = writer
        self._decoder = FrameDecoder()
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._closed = False
        self._sending = False
        self._frame_interval = FRAME_MS / 1000.0

    def feed(self, data: bytes) -> None:
        for frame_type, payload in self._decoder.feed(data):
            if frame_type == TYPE_AUDIO:
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
        """Write (payload, sentence_index) pairs, one per FRAME_MS."""
        self._sending = True
        loop = asyncio.get_running_loop()
        next_at = loop.time()
        for payload, sentence_index in frames:
            if not self._sending or self._closed:
                return
            self._writer.write(encode(TYPE_AUDIO, payload))
            await self._writer.drain()
            on_sent(sentence_index)
            next_at += self._frame_interval
            delay = next_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        self._sending = False


class AudioSocketServer:
    def __init__(self, host: str = "0.0.0.0", port: int = 9010) -> None:
        self._host, self._port = host, port
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
        decoder = FrameDecoder()
        session: AudioSocketSession | None = None
        try:
            while True:
                data = await reader.read(4096)
                if not data:
                    break
                if session is None:
                    for frame_type, payload in decoder.feed(data):
                        if frame_type == TYPE_UUID:
                            uuid = str(__import__("uuid").UUID(bytes=payload))
                            session = AudioSocketSession(uuid, writer)
                            self._sessions[uuid] = session
                            waiter = self._waiters.get(uuid)
                            if waiter is not None and not waiter.done():
                                waiter.set_result(session)
                        elif frame_type == TYPE_AUDIO and session is not None:
                            session._inbound.put_nowait(payload)
                else:
                    session.feed(data)
        except Exception:
            logger.exception("audiosocket connection failed")
        finally:
            if session is not None:
                session.close()
                self._sessions.pop(session.uuid, None)
            writer.close()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_audiosocket.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/audiosocket.py tests/test_audiosocket.py
git commit -m "feat: AudioSocket server with real-time paced writer"
```

---

### Task 4: ARI bridge and AudioSocket origination

**Files:**
- Modify: `src/phonesys/ari.py`
- Modify: `tests/test_ari.py`

**Interfaces:**
- Consumes: existing `AriClient`.
- Produces: `async create_bridge() -> str`, `async add_to_bridge(bridge_id, channel_id) -> None`, `async destroy_bridge(bridge_id) -> None`, `async originate_audiosocket(host, port, uuid, app) -> str`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_ari.py`:

```python
async def test_create_bridge_returns_id():
    def handler(request):
        assert request.url.path == "/ari/bridges"
        assert request.url.params["type"] == "mixing"
        return httpx.Response(200, json={"id": "br-1"})

    assert await _client(handler).create_bridge() == "br-1"


async def test_originate_audiosocket_pins_slin_format():
    """Measured: without formats=slin the driver negotiates slin192 and sends
    7680-byte frames at 375 KB/s. 'format' singular is silently ignored."""
    seen = {}

    def handler(request):
        seen.update(dict(request.url.params))
        return httpx.Response(200, json={"id": "ch-1"})

    cid = await _client(handler).originate_audiosocket(
        "host.docker.internal", 9010, "abc-uuid", "phonesys"
    )
    assert cid == "ch-1"
    assert seen["endpoint"] == "AudioSocket/host.docker.internal:9010/abc-uuid"
    assert seen["formats"] == "slin"
    assert "format" not in seen
    assert seen["app"] == "phonesys"


async def test_add_to_bridge():
    seen = {}

    def handler(request):
        seen["path"] = request.url.path
        seen["channel"] = request.url.params["channel"]
        return httpx.Response(204)

    await _client(handler).add_to_bridge("br-1", "ch-1")
    assert seen["path"] == "/ari/bridges/br-1/addChannel"
    assert seen["channel"] == "ch-1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_ari.py -v`
Expected: FAIL — `AttributeError: 'AriClient' object has no attribute 'create_bridge'`

- [ ] **Step 3: Write the implementation**

Add to `src/phonesys/ari.py` inside `AriClient`:

```python
    async def create_bridge(self) -> str:
        resp = await self._post("/bridges", {"type": "mixing"})
        return resp.json()["id"]

    async def add_to_bridge(self, bridge_id: str, channel_id: str) -> None:
        await self._post(f"/bridges/{bridge_id}/addChannel", {"channel": channel_id})

    async def destroy_bridge(self, bridge_id: str) -> None:
        resp = await self._client.delete(f"/bridges/{bridge_id}")
        if resp.status_code >= 400 and resp.status_code != 404:
            raise AriError(f"destroy_bridge failed: HTTP {resp.status_code}", resp.status_code)

    async def originate_audiosocket(self, host: str, port: int, uuid: str, app: str) -> str:
        """Originate an AudioSocket channel.

        formats=slin is mandatory. The parameter is plural: 'format' belongs to
        externalMedia, is ignored here, and the channel driver then negotiates
        its highest capability (slin192 -- 192kHz, 7680-byte frames).
        """
        resp = await self._post("/channels", {
            "endpoint": f"AudioSocket/{host}:{port}/{uuid}",
            "app": app,
            "formats": "slin",
        })
        return resp.json()["id"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_ari.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/ari.py tests/test_ari.py
git commit -m "feat: ARI bridge management and AudioSocket origination"
```

---

### Task 5: Hermes streaming replies

**Files:**
- Modify: `src/phonesys/hermes.py`
- Modify: `tests/test_hermes.py`

**Interfaces:**
- Consumes: existing `HermesClient`.
- Produces: `async chat_stream(session_id: str, message: str) -> AsyncIterator[str]` yielding text deltas; `async append_assistant(session_id: str, text: str) -> None`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_hermes.py`:

```python
SSE = (
    b'event: run.started\ndata: {"session_id": "s"}\n\n'
    b'event: message.started\ndata: {"message": {"id": "m", "role": "assistant"}}\n\n'
    b'event: assistant.delta\ndata: {"delta": "Hello"}\n\n'
    b'event: assistant.delta\ndata: {"delta": " there."}\n\n'
    b'event: tool.progress\ndata: {"tool_name": "_thinking", "delta": "SECRET REASONING"}\n\n'
    b'event: assistant.delta\ndata: {"delta": " How are you?"}\n\n'
    b'event: assistant.completed\ndata: {"content": "Hello there. How are you?"}\n\n'
    b'event: done\ndata: {"seq": 9}\n\n'
)


async def test_chat_stream_yields_only_assistant_deltas():
    """tool.progress carries a delta field too. Filtering on the presence of
    'delta' rather than the event name would speak internal reasoning aloud."""
    def handler(request):
        assert request.url.path == "/api/sessions/s1/chat/stream"
        return httpx.Response(200, content=SSE,
                              headers={"content-type": "text/event-stream"})

    c = _client(handler)
    deltas = [d async for d in c.chat_stream("s1", "hi")]
    assert deltas == ["Hello", " there.", " How are you?"]
    assert "SECRET REASONING" not in "".join(deltas)


async def test_chat_stream_error_raises():
    def handler(request):
        return httpx.Response(500, json={"error": {"message": "boom"}})

    with pytest.raises(HermesError):
        async for _ in _client(handler).chat_stream("s1", "hi"):
            pass
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_hermes.py -v`
Expected: FAIL — `AttributeError: 'HermesClient' object has no attribute 'chat_stream'`

- [ ] **Step 3: Write the implementation**

Add to `src/phonesys/hermes.py`:

```python
import json
from typing import AsyncIterator
```

and inside `HermesClient`:

```python
    async def chat_stream(self, session_id: str, message: str) -> AsyncIterator[str]:
        """Yield reply text deltas from the SSE stream.

        Only assistant.delta carries reply text. tool.progress also has a
        'delta' field (tool_name '_thinking'), so events must be filtered by
        NAME -- keying off the presence of 'delta' would speak the model's
        internal reasoning down the phone.
        """
        async with self._client.stream(
            "POST", f"/api/sessions/{session_id}/chat/stream",
            json={"message": message},
        ) as resp:
            if resp.status_code >= 400:
                raise HermesError(f"chat_stream failed: HTTP {resp.status_code}")
            event = None
            async for line in resp.aiter_lines():
                if line.startswith("event:"):
                    event = line.split(":", 1)[1].strip()
                elif line.startswith("data:") and event == "assistant.delta":
                    try:
                        delta = json.loads(line.split(":", 1)[1].strip()).get("delta")
                    except json.JSONDecodeError:
                        continue
                    if delta:
                        yield delta

    async def append_assistant(self, session_id: str, text: str) -> None:
        """Record what the caller actually heard after an interruption."""
        resp = await self._client.post(
            f"/api/sessions/{session_id}/chat",
            json={"message": f"[system] Your previous reply was interrupted. "
                             f"The caller only heard: {text}"},
        )
        if resp.status_code >= 400:
            raise HermesError(f"append_assistant failed: HTTP {resp.status_code}")
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_hermes.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/hermes.py tests/test_hermes.py
git commit -m "feat: Hermes streaming replies via SSE"
```

---

### Task 6: Deepgram live STT

**Files:**
- Create: `src/phonesys/stream_stt.py`
- Create: `tests/test_stream_stt.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `SttEvent` dataclass with `.kind` (`"speech_started"` | `"transcript"` | `"utterance_end"`), `.text: str`, `.is_final: bool`; `LiveStt(api_key: str, sample_rate: int = 8000)` with `async connect()`, `async send(pcm: bytes)`, `async events() -> AsyncIterator[SttEvent]`, `async close()`; `parse_dg_message(raw: str) -> SttEvent | None`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stream_stt.py`:

```python
import json
from phonesys.stream_stt import parse_dg_message


def test_speech_started_event():
    ev = parse_dg_message(json.dumps({"type": "SpeechStarted", "timestamp": 1.5}))
    assert ev is not None and ev.kind == "speech_started"


def test_interim_transcript():
    ev = parse_dg_message(json.dumps({
        "type": "Results", "is_final": False, "speech_final": False,
        "channel": {"alternatives": [{"transcript": "hello wor"}]},
    }))
    assert ev.kind == "transcript" and ev.text == "hello wor" and ev.is_final is False


def test_final_transcript():
    ev = parse_dg_message(json.dumps({
        "type": "Results", "is_final": True, "speech_final": True,
        "channel": {"alternatives": [{"transcript": "hello world"}]},
    }))
    assert ev.kind == "transcript" and ev.text == "hello world" and ev.is_final is True


def test_utterance_end():
    ev = parse_dg_message(json.dumps({"type": "UtteranceEnd", "last_word_end": 3.2}))
    assert ev.kind == "utterance_end"


def test_empty_transcript_is_ignored():
    ev = parse_dg_message(json.dumps({
        "type": "Results", "is_final": True,
        "channel": {"alternatives": [{"transcript": ""}]},
    }))
    assert ev is None


def test_metadata_is_ignored():
    assert parse_dg_message(json.dumps({"type": "Metadata", "duration": 1.0})) is None


def test_malformed_json_is_ignored():
    assert parse_dg_message("not json") is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stream_stt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.stream_stt'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/stream_stt.py`:

```python
"""Deepgram live transcription over WebSocket.

Audio is slin 8kHz mono straight off AudioSocket, which Deepgram accepts
natively as encoding=linear16&sample_rate=8000 -- no conversion needed.
vad_events=true gives SpeechStarted, which is what drives barge-in.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import urlencode

import websockets

logger = logging.getLogger(__name__)

DEEPGRAM_WS = "wss://api.deepgram.com/v1/listen"


@dataclass(frozen=True)
class SttEvent:
    kind: str          # "speech_started" | "transcript" | "utterance_end"
    text: str = ""
    is_final: bool = False


def parse_dg_message(raw: str) -> SttEvent | None:
    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None
    kind = msg.get("type")
    if kind == "SpeechStarted":
        return SttEvent(kind="speech_started")
    if kind == "UtteranceEnd":
        return SttEvent(kind="utterance_end")
    if kind == "Results":
        alts = (msg.get("channel") or {}).get("alternatives") or []
        text = (alts[0].get("transcript") if alts else "") or ""
        if not text.strip():
            return None
        return SttEvent(kind="transcript", text=text.strip(),
                        is_final=bool(msg.get("is_final")))
    return None


class LiveStt:
    def __init__(self, api_key: str, sample_rate: int = 8000,
                 model: str = "nova-2-phonecall") -> None:
        self._api_key = api_key
        self._params = {
            "model": model,
            "encoding": "linear16",
            "sample_rate": str(sample_rate),
            "channels": "1",
            "interim_results": "true",
            "vad_events": "true",
            "endpointing": "300",
            "utterance_end_ms": "1000",
            "punctuate": "true",
            "smart_format": "true",
        }
        self._ws = None

    async def connect(self) -> None:
        url = f"{DEEPGRAM_WS}?{urlencode(self._params)}"
        self._ws = await websockets.connect(
            url, additional_headers={"Authorization": f"Token {self._api_key}"}
        )

    async def send(self, pcm: bytes) -> None:
        if self._ws is not None:
            await self._ws.send(pcm)

    async def events(self) -> AsyncIterator[SttEvent]:
        if self._ws is None:
            return
        async for raw in self._ws:
            ev = parse_dg_message(raw if isinstance(raw, str) else raw.decode())
            if ev is not None:
                yield ev

    async def close(self) -> None:
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
            except Exception:
                pass
            await self._ws.close()
            self._ws = None
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stream_stt.py -v`
Expected: 7 passed

- [ ] **Step 5: Verify against the live Deepgram service**

Generate 8 kHz test audio and stream it in real time:

```bash
cd /home/hoyack/work/phonesys
.venv/bin/python - <<'PY'
import asyncio, os, sys, httpx
sys.path.insert(0, "src")
from dotenv import load_dotenv
load_dotenv("/home/hoyack/work/phonesys/.env")
from phonesys.ulaw import ulaw_to_slin
from phonesys.stream_stt import LiveStt

async def main():
    r = httpx.post("https://api.elevenlabs.io/v1/text-to-speech/EXAVITQu4vr4xnSDxMaL",
                   params={"output_format": "ulaw_8000"},
                   headers={"xi-api-key": os.environ["ELEVENLABS_API_KEY"]},
                   json={"text": "Testing the live transcription path over a phone line.",
                         "model_id": "eleven_turbo_v2_5"}, timeout=60)
    pcm = ulaw_to_slin(r.content)
    stt = LiveStt(api_key=os.environ["DEEPGRAM_API_KEY"])
    await stt.connect()
    seen = []
    async def read():
        async for ev in stt.events():
            seen.append(ev)
            print(f"  {ev.kind:15} final={ev.is_final} {ev.text!r}")
    task = asyncio.create_task(read())
    for i in range(0, len(pcm), 320):          # 20ms frames, paced
        await stt.send(pcm[i:i+320])
        await asyncio.sleep(0.02)
    await asyncio.sleep(3)
    await stt.close()
    task.cancel()
    print("speech_started seen:", any(e.kind == "speech_started" for e in seen))
    print("final transcript  :", [e.text for e in seen if e.is_final])

asyncio.run(main())
PY
```

Expected: at least one `speech_started` event and a final transcript resembling the spoken text. **If `speech_started` never fires, barge-in cannot work — stop and investigate before continuing.**

- [ ] **Step 6: Commit**

```bash
git add src/phonesys/stream_stt.py tests/test_stream_stt.py
git commit -m "feat: Deepgram live STT with voice activity events"
```

---

### Task 7: Streaming TTS with sentence chunking

**Files:**
- Create: `src/phonesys/stream_tts.py`
- Create: `tests/test_stream_tts.py`

**Interfaces:**
- Consumes: `ulaw_to_slin`.
- Produces: `split_sentences(text: str) -> list[str]`; `SentenceBuffer` with `feed(delta: str) -> list[str]` and `flush() -> str`; `StreamTts(api_key: str)` with `async synthesize_frames(text, voice_id, sentence_index) -> list[tuple[bytes, int]]`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stream_tts.py`:

```python
from phonesys.stream_tts import SentenceBuffer, split_sentences


def test_split_basic_sentences():
    assert split_sentences("One. Two! Three?") == ["One.", "Two!", "Three?"]


def test_split_keeps_trailing_fragment():
    assert split_sentences("Done. And then") == ["Done.", "And then"]


def test_buffer_emits_only_complete_sentences():
    b = SentenceBuffer()
    assert b.feed("Hello") == []
    assert b.feed(" there.") == ["Hello there."]
    assert b.feed(" How are") == []
    assert b.feed(" you?") == ["How are you?"]


def test_buffer_flush_returns_remainder():
    b = SentenceBuffer()
    b.feed("Trailing words")
    assert b.flush() == "Trailing words"
    assert b.flush() == ""


def test_buffer_handles_multiple_sentences_in_one_delta():
    b = SentenceBuffer()
    assert b.feed("A. B. C") == ["A.", "B."]
    assert b.flush() == "C"


def test_frames_are_320_bytes_with_sentence_index():
    from phonesys.stream_tts import _frames_from_ulaw
    frames = _frames_from_ulaw(b"\xff" * 800, sentence_index=3)
    assert all(len(p) == 320 for p, _ in frames)
    assert all(i == 3 for _, i in frames)
    assert len(frames) == 5   # 800 ulaw bytes -> 1600 slin bytes -> 5 frames
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stream_tts.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.stream_tts'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/stream_tts.py`:

```python
"""Sentence-level TTS for streaming playback.

Hermes emits token deltas; speaking each token separately sounds terrible and
costs a request per token. SentenceBuffer accumulates deltas and releases
whole sentences, which is also the granularity at which interrupted speech is
tracked.
"""
from __future__ import annotations

import re

import httpx

from phonesys.audiosocket_codec import AUDIO_FRAME_BYTES
from phonesys.ulaw import ulaw_to_slin

ELEVENLABS_URL = "https://api.elevenlabs.io"
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class TtsStreamError(RuntimeError):
    """ElevenLabs failed for a sentence."""


def split_sentences(text: str) -> list[str]:
    return [p for p in (s.strip() for s in _SENTENCE_END.split(text)) if p]


class SentenceBuffer:
    """Accumulates streamed deltas, releasing complete sentences."""

    def __init__(self) -> None:
        self._buf = ""

    def feed(self, delta: str) -> list[str]:
        self._buf += delta
        parts = _SENTENCE_END.split(self._buf)
        if len(parts) == 1:
            return []
        self._buf = parts[-1]
        return [p.strip() for p in parts[:-1] if p.strip()]

    def flush(self) -> str:
        out, self._buf = self._buf.strip(), ""
        return out


def _frames_from_ulaw(ulaw: bytes, sentence_index: int) -> list[tuple[bytes, int]]:
    pcm = ulaw_to_slin(ulaw)
    return [
        (pcm[i:i + AUDIO_FRAME_BYTES], sentence_index)
        for i in range(0, len(pcm) - AUDIO_FRAME_BYTES + 1, AUDIO_FRAME_BYTES)
    ]


class StreamTts:
    def __init__(self, api_key: str, timeout: float = 30.0,
                 model_id: str = "eleven_turbo_v2_5") -> None:
        self._model_id = model_id
        self._client = httpx.AsyncClient(
            base_url=ELEVENLABS_URL,
            headers={"xi-api-key": api_key},
            timeout=timeout,
        )

    async def synthesize_frames(self, text: str, voice_id: str,
                                sentence_index: int) -> list[tuple[bytes, int]]:
        if not text.strip():
            return []
        resp = await self._client.post(
            f"/v1/text-to-speech/{voice_id}/stream",
            params={"output_format": "ulaw_8000"},
            json={"text": text, "model_id": self._model_id},
        )
        if resp.status_code >= 400:
            raise TtsStreamError(f"elevenlabs failed: HTTP {resp.status_code}")
        return _frames_from_ulaw(resp.content, sentence_index)

    async def aclose(self) -> None:
        await self._client.aclose()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stream_tts.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/stream_tts.py tests/test_stream_tts.py
git commit -m "feat: sentence-chunked streaming TTS"
```

---

### Task 8: Barge-in state machine

The core of the feature. Driven by an injected event source so barge-in is tested deterministically with no network.

**Files:**
- Create: `src/phonesys/stream_call.py`
- Create: `tests/test_stream_call.py`

**Interfaces:**
- Consumes: `SttEvent`, `SentenceBuffer`, `AudioSocketSession`, `HermesClient`, `StreamTts`, `ExtensionConfig`.
- Produces: `StreamingCallHandler(ari, stt_factory, tts, hermes_factory, audiosocket_server, cfg)` with `async handle(channel_id, extension, ext_cfg) -> None`; `HeardTracker` with `record_sent(index)`, `heard_text(sentences) -> str`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_stream_call.py`:

```python
from phonesys.stream_call import HeardTracker


def test_nothing_sent_means_nothing_heard():
    t = HeardTracker()
    assert t.heard_text(["One.", "Two."]) == ""


def test_fully_sent_sentence_counts_as_heard():
    t = HeardTracker()
    t.expect(0, frames=3)
    for _ in range(3):
        t.record_sent(0)
    assert t.heard_text(["One.", "Two."]) == "One."


def test_partially_sent_sentence_is_not_counted():
    """Round DOWN: claiming a half-spoken sentence was heard makes the agent
    reference words the caller never got."""
    t = HeardTracker()
    t.expect(0, frames=3)
    t.expect(1, frames=3)
    for _ in range(3):
        t.record_sent(0)
    t.record_sent(1)          # only 1 of 3 frames of sentence 1
    assert t.heard_text(["One.", "Two."]) == "One."


def test_multiple_complete_sentences_are_joined():
    t = HeardTracker()
    t.expect(0, frames=2)
    t.expect(1, frames=2)
    for _ in range(2):
        t.record_sent(0)
    for _ in range(2):
        t.record_sent(1)
    assert t.heard_text(["One.", "Two.", "Three."]) == "One. Two."
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stream_call.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.stream_call'`

- [ ] **Step 3: Write the implementation**

Create `src/phonesys/stream_call.py`:

```python
"""Streaming call handler with barge-in.

The only module that knows about interruption. Caller audio streams to
Deepgram in every state, which is what makes interruption detectable while the
agent is speaking.
"""
from __future__ import annotations

import asyncio
import logging
import uuid as uuid_mod

from phonesys.config import ExtensionConfig
from phonesys.stream_tts import SentenceBuffer

logger = logging.getLogger(__name__)


class HeardTracker:
    """Counts frames actually written, per sentence.

    A sentence counts as heard only when every one of its frames was written.
    Rounding down matters: crediting a half-spoken sentence makes the agent
    reference words the caller never received.
    """

    def __init__(self) -> None:
        self._expected: dict[int, int] = {}
        self._sent: dict[int, int] = {}

    def expect(self, index: int, frames: int) -> None:
        self._expected[index] = frames

    def record_sent(self, index: int) -> None:
        self._sent[index] = self._sent.get(index, 0) + 1

    def heard_text(self, sentences: list[str]) -> str:
        heard: list[str] = []
        for i, text in enumerate(sentences):
            expected = self._expected.get(i)
            if expected is None or self._sent.get(i, 0) < expected:
                break
            heard.append(text)
        return " ".join(heard)


class StreamingCallHandler:
    def __init__(self, ari, stt_factory, tts, hermes_factory, audiosocket_server,
                 audiosocket_host: str, audiosocket_port: int) -> None:
        self._ari = ari
        self._stt_factory = stt_factory
        self._tts = tts
        self._hermes_factory = hermes_factory
        self._server = audiosocket_server
        self._as_host = audiosocket_host
        self._as_port = audiosocket_port

    async def handle(self, channel_id: str, extension: str, ext: ExtensionConfig) -> None:
        hermes = self._hermes_factory(ext)
        stt = self._stt_factory()
        session_id = f"call-{channel_id}"
        call_uuid = str(uuid_mod.uuid4())
        bridge_id = None
        as_channel = None
        interrupt = asyncio.Event()

        try:
            await self._ari.answer(channel_id)
            await hermes.create_session(session_id)

            bridge_id = await self._ari.create_bridge()
            await self._ari.add_to_bridge(bridge_id, channel_id)
            as_channel = await self._ari.originate_audiosocket(
                self._as_host, self._as_port, call_uuid, self._ari._cfg.app
            )
            await self._ari.add_to_bridge(bridge_id, as_channel)
            audio = await self._server.wait_for_session(call_uuid, timeout=10)

            await stt.connect()

            async def pump_audio() -> None:
                async for frame in audio.frames():
                    await stt.send(frame)

            speech_frames = 0
            confirm_frames = max(1, ext.barge_in_confirm_ms // 20)
            pending_final: list[str] = []

            async def pump_stt() -> None:
                nonlocal speech_frames
                async for ev in stt.events():
                    if ev.kind == "speech_started":
                        speech_frames = 0
                    elif ev.kind == "transcript":
                        if not ev.is_final:
                            speech_frames += 1
                            if speech_frames >= confirm_frames:
                                interrupt.set()
                        else:
                            pending_final.append(ev.text)
                            interrupt.set()

            tasks = [asyncio.create_task(pump_audio()), asyncio.create_task(pump_stt())]

            await self._speak(audio, ext, [ext.greeting], hermes, session_id, interrupt)

            deadline = asyncio.get_running_loop().time() + ext.max_call_seconds
            while asyncio.get_running_loop().time() < deadline:
                await interrupt.wait()
                interrupt.clear()
                if not pending_final:
                    continue
                utterance = " ".join(pending_final).strip()
                pending_final.clear()
                if not utterance:
                    continue
                logger.info("call %s heard: %r", channel_id, utterance)
                await self._reply(audio, ext, hermes, session_id, utterance, interrupt)

            for t in tasks:
                t.cancel()
        except Exception:
            logger.exception("streaming call %s failed", channel_id)
        finally:
            try:
                await stt.close()
            except Exception:
                pass
            if bridge_id:
                await self._ari.destroy_bridge(bridge_id)
            if as_channel:
                await self._ari.hangup(as_channel)
            await self._ari.hangup(channel_id)
            await hermes.aclose()

    async def _speak(self, audio, ext, sentences, hermes, session_id, interrupt) -> None:
        tracker = HeardTracker()
        for i, text in enumerate(sentences):
            frames = await self._tts.synthesize_frames(text, ext.voice_id, i)
            tracker.expect(i, len(frames))
            interrupt.clear()
            await audio.send_frames(frames, on_sent=tracker.record_sent)

    async def _reply(self, audio, ext, hermes, session_id, utterance, interrupt) -> None:
        buf = SentenceBuffer()
        tracker = HeardTracker()
        spoken: list[str] = []
        index = 0
        interrupt.clear()

        async def speak_sentence(text: str) -> bool:
            nonlocal index
            frames = await self._tts.synthesize_frames(text, ext.voice_id, index)
            tracker.expect(index, len(frames))
            spoken.append(text)
            sender = asyncio.create_task(
                audio.send_frames(frames, on_sent=tracker.record_sent)
            )
            waiter = asyncio.create_task(interrupt.wait())
            done, _ = await asyncio.wait({sender, waiter},
                                         return_when=asyncio.FIRST_COMPLETED)
            if waiter in done:
                audio.stop_sending()
                sender.cancel()
                return False
            waiter.cancel()
            index += 1
            return True

        try:
            async for delta in hermes.chat_stream(session_id, utterance):
                for sentence in buf.feed(delta):
                    if not await speak_sentence(sentence):
                        raise _Interrupted
            tail = buf.flush()
            if tail:
                await speak_sentence(tail)
        except _Interrupted:
            heard = tracker.heard_text(spoken)
            logger.info("interrupted; caller heard: %r", heard)
            if heard:
                await hermes.append_assistant(session_id, heard)


class _Interrupted(Exception):
    pass
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stream_call.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/phonesys/stream_call.py tests/test_stream_call.py
git commit -m "feat: barge-in state machine with heard-tracking"
```

---

### Task 9: Config, dispatch, and dialplan for 7001

**Files:**
- Modify: `src/phonesys/config.py`
- Modify: `src/phonesys/main.py`
- Modify: `config.yaml`
- Modify: `tests/test_config.py`
- Modify (in container): `/etc/asterisk/extensions_custom.conf`

**Interfaces:**
- Consumes: everything above.
- Produces: `ExtensionConfig.mode: str`, `.barge_in_confirm_ms: int`, `.audiosocket_host: str`, `.audiosocket_port: int`; `main.py` dispatching on `mode`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_config.py`:

```python
STREAMING_YAML = CONFIG_YAML + """
  "7001":
    mode: streaming
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes."
    voice_id: voice-abc
    barge_in_confirm_ms: 300
    audiosocket_host: host.docker.internal
    audiosocket_port: 9010
"""


def test_mode_defaults_to_turn_and_streaming_is_read(tmp_path, monkeypatch):
    monkeypatch.setenv("ARI_USER", "phonesys")
    monkeypatch.setenv("ARI_PASSWORD", "secret")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "hkey")
    cfg = load_config(_write(tmp_path, STREAMING_YAML))
    assert cfg.extensions["7000"].mode == "turn"
    s = cfg.extensions["7001"]
    assert s.mode == "streaming"
    assert s.barge_in_confirm_ms == 300
    assert s.audiosocket_port == 9010
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: FAIL — `AttributeError: 'ExtensionConfig' object has no attribute 'mode'`

- [ ] **Step 3: Extend the config dataclass**

In `src/phonesys/config.py`, add to `ExtensionConfig`:

```python
    mode: str = "turn"
    barge_in_confirm_ms: int = 300
    audiosocket_host: str = "host.docker.internal"
    audiosocket_port: int = 9010
```

and in `load_config`, inside the `ExtensionConfig(...)` call:

```python
            mode=ext.get("mode", "turn"),
            barge_in_confirm_ms=int(ext.get("barge_in_confirm_ms", 300)),
            audiosocket_host=ext.get("audiosocket_host", "host.docker.internal"),
            audiosocket_port=int(ext.get("audiosocket_port", 9010)),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 4 passed

- [ ] **Step 5: Add 7001 to config.yaml**

```yaml
  "7001":
    mode: streaming
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi, this is Hermes. Go ahead, you can interrupt me any time."
    voice_id: EXAVITQu4vr4xnSDxMaL
    barge_in_confirm_ms: 300
    audiosocket_host: host.docker.internal
    audiosocket_port: 9010
    max_silence_seconds: 1
    max_turn_seconds: 30
    max_call_seconds: 600
```

Add `mode: turn` to the existing `"7000"` block.

- [ ] **Step 6: Dispatch on mode in main.py**

Start the AudioSocket server, build both handlers, and route by `mode`:

```python
    from phonesys.audiosocket import AudioSocketServer
    from phonesys.stream_call import StreamingCallHandler
    from phonesys.stream_stt import LiveStt
    from phonesys.stream_tts import StreamTts

    as_server = AudioSocketServer(host="0.0.0.0", port=9010)
    await as_server.start()

    streaming = StreamingCallHandler(
        ari,
        lambda: LiveStt(api_key=os.environ["DEEPGRAM_API_KEY"]),
        StreamTts(api_key=os.environ["ELEVENLABS_API_KEY"]),
        lambda ext: HermesClient(base_url=ext.hermes_base_url, api_key=ext.hermes_api_key),
        as_server,
        "host.docker.internal",
        9010,
    )
```

and where the call is dispatched:

```python
        if ext_cfg.mode == "streaming":
            asyncio.create_task(streaming.handle(channel_id, extension, ext_cfg))
        else:
            asyncio.create_task(handler.handle(channel_id, extension, ext_cfg))
```

- [ ] **Step 7: Add the dialplan entry**

```bash
docker exec freepbx-docker-freepbx-1 sh -c 'cat >> /etc/asterisk/extensions_custom.conf <<"EOF"
exten => 7001,1,NoOp(phonesys: streaming barge-in agent)
 same => n,Answer()
 same => n,Stasis(phonesys,7001)
 same => n,Hangup()
EOF'
docker exec freepbx-docker-freepbx-1 fwconsole reload
docker exec freepbx-docker-freepbx-1 asterisk -rx 'dialplan show 7001@from-internal'
```

Expected: shows `Stasis(phonesys,7001)`.

- [ ] **Step 8: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass, including the untouched turn-based tests.

- [ ] **Step 9: Commit**

```bash
git add src/phonesys/config.py src/phonesys/main.py config.yaml tests/test_config.py
git commit -m "feat: mode dispatch and extension 7001"
```

---

### Task 10: Live verification and documentation

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Confirm 7000 still works**

Start the bridge, dial 7000, hold one exchange. Expected: unchanged turn-based behaviour. **If 7000 regressed, stop** — keeping it working is a hard constraint.

- [ ] **Step 2: Dial 7001 and let it finish a reply**

Expected: greeting, then a normal exchange with noticeably lower latency than 7000 (audio should begin after the first sentence rather than the whole reply).

- [ ] **Step 3: Dial 7001 and interrupt mid-reply**

Ask something with a long answer, then speak over it. Expected: the agent stops within roughly half a second, and the log shows:

```
interrupted; caller heard: '<the sentences that actually played>'
```

- [ ] **Step 4: Confirm the agent knows what was heard**

After interrupting, ask "what did you just tell me?". Expected: it refers only to the sentences that played, not the full generated reply.

- [ ] **Step 5: Check the CDR**

```bash
docker exec freepbx-docker-freepbx-1 mysql -ufreepbxuser \
  -p"$(cat /home/hoyack/freepbx-docker/secrets/freepbxuser_password.txt)" -hdb asteriskcdrdb \
  -e "SELECT calldate, src, dst, duration, disposition FROM cdr ORDER BY calldate DESC LIMIT 5;"
```

Expected: `dst` 7001, `ANSWERED`, duration matching the real conversation — **not exactly 30 s**, which would indicate `rtptimeout`.

- [ ] **Step 6: Document**

Add a "Barge-in (extension 7001)" section to `README.md` covering: the AudioSocket topology, that `formats=slin` is mandatory and why, that outbound audio must be paced, the `barge_in_confirm_ms` tunable, and troubleshooting — specifically that if barge-in appears not to work, pacing is the first suspect, not detection.

- [ ] **Step 7: Commit**

```bash
git add README.md
git commit -m "docs: barge-in streaming pipeline"
```

---

## Self-Review

**Spec coverage:** AudioSocket transport → Tasks 1, 3, 4. Speech-confirmed interruption → Tasks 6, 8. Truncated history → Task 8 (`HeardTracker`) and Task 5 (`append_assistant`). Extension 7001 with `mode` flag → Task 9. Pacing → Task 3 (with an explicit timing test). µ-law conversion → Task 2. Sentence chunking → Task 7. Error handling → Task 8's `try/finally` plus per-adapter error types. Testing → unit tests in every task, live verification in Task 10.

**Placeholder scan:** No TBDs. Every code step carries real code. All protocol constants come from measurement, not memory.

**Type consistency:** `SttEvent.kind` values (`speech_started`/`transcript`/`utterance_end`) match between Tasks 6 and 8. `synthesize_frames(text, voice_id, sentence_index) -> list[tuple[bytes, int]]` matches Tasks 7 and 8. `send_frames(frames, on_sent)` matches Tasks 3 and 8. `HeardTracker.expect/record_sent/heard_text` matches Task 8's tests and implementation. `AUDIO_FRAME_BYTES` (320) is used consistently in Tasks 1, 3, and 7.

**Known risks, stated rather than hidden:**

1. **The state machine's concurrency is only partly unit-testable.** `HeardTracker` is tested directly, but the interaction between `pump_stt`, `send_frames`, and `interrupt` is timing-dependent and is genuinely verified only in Task 10. This is the least-covered part of the plan and the most likely source of live bugs.
2. **Barge-in detection uses interim transcripts as a speech proxy.** `confirm_frames` counts interim results, not milliseconds of audio, so `barge_in_confirm_ms` is approximate — Deepgram's interim cadence is not a fixed 20 ms. If tuning feels imprecise, switch to timestamping `speech_started` and measuring elapsed wall time.
3. **`append_assistant` injects a `[system]` message through the normal chat endpoint**, because `api_server` exposes no way to append an assistant turn directly. It costs an extra model round-trip and the agent may respond to it. If that proves awkward, the alternative is writing to the session DB directly, which couples us to Hermes internals.
4. **One AudioSocket port serves all calls**, with sessions keyed by UUID. Concurrent calls should work but are untested; the plan does not cover load.
