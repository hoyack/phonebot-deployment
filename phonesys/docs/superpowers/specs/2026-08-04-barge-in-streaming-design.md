# Barge-In Streaming Pipeline — Design

**Date:** 2026-08-04
**Status:** Approved for planning
**Supersedes nothing.** Extends the turn-based system from
`2026-08-02-hermes-voice-extension-design.md`, which stays working.

## Goal

Hold a natural phone conversation with a Hermes agent: speak over it and it
stops, the way a person would. Reachable on extension **7001**, with the
existing turn-based agent on **7000** untouched.

## Scope

**Delivers:** continuous duplex audio over AudioSocket, streaming STT with
voice-activity detection, streaming Hermes replies synthesized sentence by
sentence, speech-confirmed interruption, and conversation history truncated to
what the caller actually heard.

**Excludes:** replacing extension 7000 (a later, one-line config change),
multiple concurrent calls per extension beyond what falls out naturally,
word-level truncation, and any change to the PBX or Hermes profile.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Audio transport | **AudioSocket over TCP** | Verified `host.docker.internal` is reachable from the container; `externalMedia` would put RTP back over Docker Desktop's UDP NAT, the exact thing that caused the one-way-audio bug. TCP also removes jitter/loss handling entirely. |
| Interruption trigger | **Speech-confirmed, ~300 ms** | Coughs, keyboard noise and backchannels ("mhm") must not stop the agent. ~350 ms to go quiet reads as natural; humans take about that long. |
| Interrupted history | **Truncate to what was heard** | Otherwise the agent believes it said things the caller never heard and later references them — a confusing, hard-to-debug failure mode. |
| Migration | **New extension 7001, `mode` flag** | 7000 keeps working, allows direct A/B on one line, and rollback is a config change. |
| Truncation granularity | **Sentence** | Word-level needs per-word timing from TTS; sentence granularity is exact with per-sentence frame accounting. |
| Outbound audio | **Paced to real time** | See "Pacing" below. This is the load-bearing decision. |
| TTS format | **`ulaw_8000` → slin via lookup table** | ElevenLabs has no `pcm_8000`. µ-law → linear is a 256-entry table: exact, no resampling, and avoids `audioop`, removed in Python 3.13+ (we run 3.14). |

## Architecture

```
extension 7001 → Stasis(phonesys,7001) → StreamingCallHandler

1. answer caller
2. create mixing bridge, add caller
3. register AudioSocket session under a fresh UUID
4. originate AudioSocket/host.docker.internal:9010/<uuid>, add to bridge
5. Asterisk connects OUT to our TCP server → bidirectional PCM
```

```
caller ──PCM slin16 8k──> AudioSocket ──> Deepgram live WS ──> transcript + VAD
                                                                     │
                                                                     ▼
                                                              Hermes /chat/stream
                                                                     │ per sentence
                                                                     ▼
caller <──PCM slin16 8k── AudioSocket <── ElevenLabs (ulaw_8000) ────┘
```

Both directions run concurrently and continuously. That is the entire
difference from v1, where record → transcribe → think → speak is strictly
sequential. Transcribing while the agent speaks is what makes interruption
detectable at all.

## State machine

One per call. Caller audio streams to Deepgram in **every** state.

```
LISTENING ──utterance end──> THINKING ──first sentence──> SPEAKING
    ▲                                                        │
    └──────────── confirmed speech (~300 ms) ────────────────┘
                   (also fires during THINKING)
```

- **LISTENING** — inbound audio to Deepgram; wait for endpointing.
- **THINKING** — Hermes `/chat/stream` running; caller may interrupt already.
- **SPEAKING** — paced TTS frames outbound; watching for confirmed speech.

On confirmed speech while THINKING or SPEAKING: cancel TTS generation, stop the
writer, drop queued frames, compute what was heard, write the truncated
assistant turn to Hermes, return to LISTENING.

## Pacing (load-bearing)

Outbound audio is written **one 20 ms frame per 20 ms of wall clock**.

Dumping frames as fast as ElevenLabs produces them makes Asterisk buffer
seconds of speech the caller has not heard yet but is now committed to. Two
things break at once: an interrupt cannot stop what is about to play, and there
is no way to know what was heard. Pacing keeps "bytes written" ≈ "audio heard"
to within one frame, which makes barge-in *and* truncation correct.

If barge-in ever appears not to work, suspect pacing before suspecting
detection.

## Truncation accounting

TTS is generated per sentence; each outbound frame carries its sentence index.
A sentence is "heard" once all its frames are written. On interrupt, round
**down** to the last fully-written sentence. Hermes receives that text with an
`[interrupted]` marker appended, so the agent knows the caller heard only part.

## Components

New modules, each with one responsibility and a fake for tests:

| Module | Responsibility |
|---|---|
| `audiosocket.py` | TCP server, frame codec, per-call session, paced writer |
| `stream_stt.py` | Deepgram live WebSocket client → events |
| `stream_tts.py` | ElevenLabs streaming → µ-law → slin frames |
| `stream_call.py` | The state machine; the only place barge-in logic lives |

Modified:

| Module | Change |
|---|---|
| `hermes.py` | add `chat_stream()` over `/api/sessions/{id}/chat/stream` |
| `config.py` | add `mode` (`turn`\|`streaming`), `barge_in_confirm_ms` |
| `main.py` | dispatch on `mode` |
| `ari.py` | add `create_bridge`, `add_to_bridge`, `originate_audiosocket` |

`call.py`, `recordings.py`, and `pipeline.py` are **not modified** — extension
7000 must keep working.

## Configuration

```yaml
extensions:
  "7000":
    mode: turn          # unchanged behaviour
    ...
  "7001":
    mode: streaming
    barge_in_confirm_ms: 300
    audiosocket_host: host.docker.internal
    audiosocket_port: 9010
    ...
```

## Error handling

| Failure | Behavior |
|---|---|
| AudioSocket disconnects | End the call |
| Deepgram WS drops | Reconnect once; then apologize aloud and end |
| Hermes stream errors | Apologize aloud, return to LISTENING |
| ElevenLabs fails mid-sentence | Skip that sentence, log, continue |
| Caller hangs up | Cancel all tasks, close WS and socket (404 is normal) |
| Runaway call | `max_call_seconds` still enforced |

## Testing

- **Frame codec** — encode/decode round-trip, split frames across reads,
  oversized payloads. Pure unit tests.
- **State machine** — driven by an injected event source, no network. Barge-in
  is verified deterministically: "speech at frame 40 → sentences 1–2 heard,
  sentence 3 never sent, history truncated to 2." This is the logic most likely
  to hide subtle bugs and the least pleasant to debug by making phone calls.
- **Pacing** — assert writes are spaced in time, not dumped.
- **µ-law conversion** — table correctness against known values.
- **Live** — dial 7001, interrupt mid-sentence, confirm the agent stops and
  that its next reply reflects only what was heard.

## Verified facts (measured 2026-08-04, not assumed)

All three original unknowns were resolved empirically before planning.

**1. AudioSocket framing** — `[type:1][len:2 big-endian][payload]`, confirmed:

| Type | Meaning | Observed |
|---|---|---|
| `0x01` | UUID | len 16, the raw 16 bytes of the UUID passed to ARI |
| `0x10` | Audio | len 320 = 20 ms of slin 8 kHz mono |

**2. ARI originates AudioSocket channels** — `POST /channels` with
`endpoint=AudioSocket/host.docker.internal:9010/<uuid>` returns 200 and
Asterisk connects out to the bridge. Bridging works:
`POST /bridges?type=mixing` → `POST /bridges/{id}/addChannel` → audio flows.

**3. `formats=slin` is mandatory.** This is the sharpest trap found. Without
it the channel driver negotiates its highest capability — **`slin192`** — and
delivers 7680-byte frames at 375 KB/s (192 kHz). Deepgram would receive 24× the
data at the wrong rate. With `formats=slin` (note the plural; `format` is an
`externalMedia` parameter and is silently ignored here) frames are exactly 320
bytes at 16 KB/s.

**4. Asterisk paces inbound audio at real time** — frames arrive every 20 ms
(measured offsets 0.0000, 0.0200, 0.0400, 0.0598 …), and 10 s of wall clock
yielded 8.62 s of audio. Inbound needs no flow control. Outbound still must be
paced by us, since nothing throttles our writes.

**5. Hermes `/chat/stream` is SSE** with this vocabulary:

```
run.started → message.started → assistant.delta ×N
            → tool.progress → assistant.completed → run.completed → done
```

Reply text is the concatenation of `assistant.delta` `delta` fields.
**`tool.progress` also carries a `delta` field** (e.g. `tool_name: "_thinking"`)
— accumulating every `delta` would speak the model's internal reasoning down
the phone. Filter on the event name, not the presence of `delta`.

**6. Port 9010, not 9000** — 9000 is occupied by a minio container on this host.

## Known constraints inherited

- Asterisk 21.10.2, AudioSocket available since 18.0.0, slin16 @ 8 kHz.
- ARI on `127.0.0.1:8088`; Asterisk reaches the bridge at
  `host.docker.internal` only.
- Extension 7000 and its media-address workaround are unaffected.
