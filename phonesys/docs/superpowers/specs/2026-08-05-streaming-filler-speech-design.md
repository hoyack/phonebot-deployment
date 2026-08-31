# Filler speech for extension 7001

**Date:** 2026-08-05
**Status:** approved, not yet implemented
**Scope:** extension 7001 (streaming) only. The turn-based path — `call.py`,
`pipeline.py`, `recordings.py` — is not touched.

---

## Problem

On 7001 the caller hears nothing between finishing their sentence and the
first word of the reply. Measured from the live call of 2026-08-05, from the
utterance being recognised to the first TTS byte:

| Caller utterance | Dead air |
|---|---|
| "Okay. Tell me a story." | 6.5 s |
| "Okay. I'm gonna interrupt you now." | 4.1 s |
| "Okay. Tell continue telling me the story." | 9.9 s |
| "What kind of rate loan did they settle it for?" | 4.2 s |

Four to ten seconds of silence on a phone call reads as a dropped call. The
caller has no way to tell the difference between "thinking" and "gone".

This design **masks** that latency. It does not reduce it. Reducing the
underlying 4–10 s is a separate and probably larger win, and is out of scope
here — see Non-goals.

---

## Behaviour

After the caller's utterance is recognised, if no reply sentence is ready
within `filler_delay_ms`, the agent speaks a short stall phrase. If the reply
is still not ready `filler_repeat_ms` later it speaks another, drawn from the
next tier up, holding at the last tier for as long as the wait continues.

```
  [0.0s] caller stops speaking
  [0.8s] AGENT: "Let me think about that."      <- tier 0
  [3.8s] AGENT: "Still working on that."        <- tier 1
  [6.8s] AGENT: "Almost there."                 <- tier 2
  [9.5s] AGENT: <first real reply sentence>
```

Phrases escalate rather than repeat so that a long wait sounds like progress
instead of a stuck loop.

If the caller speaks over a filler phrase, the phrase stops and **the reply
being generated is abandoned** — the caller has said something new, so the
answer in flight is stale.

Those two effects have **different triggers**, which matters for safety:

| Trigger | Effect | Latency |
|---|---|---|
| `interrupt` (a confirmed interim burst) | Filler audio stops | ~300 ms |
| `utterance_ready` (a complete new utterance) | Reply stream abandoned | ~1 s |

Abandoning on `interrupt` alone would be wrong. `SpeechBurst` confirms on any
sustained interim, including line noise or a cough, so a stray sound during
the thinking window would discard a reply the caller never heard a word of and
leave them in silence with no answer coming — strictly worse than no filler at
all. Requiring a complete utterance before discarding the reply keeps the
caller-facing behaviour (audio stops fast, a real new question wins) without
that failure mode.

Under `filler_delay_ms` of thinking the caller hears nothing extra. This is
the common case for short replies and must stay that way.

---

## Components

### `filler.py` — `FillerBank`

Owns the phrases and their synthesized audio.

```python
class FillerBank:
    def __init__(self, tiers: list[list[str]]) -> None: ...
    async def prepare(self, tts: StreamTts, voice_id: str) -> None: ...
    def take(self, tier: int) -> list[tuple[bytes, int]] | None: ...
    def is_empty(self) -> bool: ...

    @property
    def last_tier(self) -> int: ...   # highest tier index that has audio
```

`last_tier` is the highest tier that actually holds synthesized audio, not
simply `len(tiers) - 1`: if the top tier failed to synthesize, escalation must
stop at the highest tier that can still be spoken.

- **`prepare()` runs once at process startup**, not per call and not on
  demand. A TTS round-trip costs 400–600 ms, which is most of the gap the
  filler exists to cover; synthesizing on demand would defeat the feature.
- **Failure is contained.** If `prepare()` cannot synthesize (ElevenLabs down,
  bad key, bad voice), it logs and leaves the bank empty. `take()` then
  returns `None` and 7001 behaves exactly as it does today. Filler is a
  nicety and must never be able to take the extension down — the same
  isolation principle as `_start_streaming` in `main.py`.
- **`take(tier)` rotates within the tier** and the cursor is process-wide, so
  two consecutive turns do not open with the same words.
- Frames are returned in the `(payload, sentence_index)` shape
  `AudioSocketSession.send_frames` expects. The index is a fixed sentinel;
  nothing consumes it, because filler is never credited (see Invariant).

### `config.py` — three new `ExtensionConfig` fields

```python
filler_delay_ms: int = 0          # 0 disables filler entirely
filler_repeat_ms: int = 3000
filler_phrases: tuple[tuple[str, ...], ...] = ()
```

`filler_delay_ms` defaults to `0` (disabled), so an extension that does not
opt in — including 7000, and including 7001 if the phrases are removed — is
unaffected. `config.yaml` gains, under `"7001"` only:

```yaml
    filler_delay_ms: 800
    filler_repeat_ms: 3000
    filler_phrases:
      - ["Let me think about that.", "Give me a second.", "Good question."]
      - ["Still working on that.", "Bear with me."]
      - ["Almost there.", "Nearly done."]
```

Phrases live in config rather than in code so they can be retuned by ear
without a code change, matching how `barge_in_confirm_ms` and the greeting are
already handled.

### `stream_call.py` — `_reply` changes

Two changes, both in `_reply`.

**A filler task runs during the wait.** Started before the reply stream is
iterated, cancelled at the first real sentence:

```python
async def run_filler() -> None:
    await asyncio.sleep(delay)
    tier = 0
    while not filler_stop.is_set():
        frames = bank.take(tier)
        if frames is None:
            return
        await audio.send_frames(frames, on_sent=_discard)
        tier = min(tier + 1, bank.last_tier)
        await asyncio.sleep(repeat)
```

**Delta iteration is raced against `utterance_ready`.** Today `_reply` blocks
on `async for delta in stream`, so a caller who asks something new during the
thinking gap is not noticed until a whole sentence has been spoken at them.
Abandoning a stale reply requires noticing while still waiting, so delta
iteration becomes a race that raises `_Interrupted` when a complete new
utterance lands first. Filler audio is silenced separately and sooner, off the
`interrupt` event, per the table above.

---

## Invariant: filler never enters Hermes's history

Hermes did not generate "Let me think about that." Crediting it would make
`append_assistant` tell Hermes it said words it never produced, corrupting the
exact transcript that `HeardTracker` exists to keep honest.

Therefore:

- filler frames are sent with a no-op `on_sent`, so the reply's `HeardTracker`
  never sees them;
- filler text is never appended to `spoken`;
- on interruption during filler, `credit_heard` computes an empty string and
  the existing `if heard:` guard means nothing is posted.

This has its own test. It is the one thing here that could quietly corrupt
conversation state rather than merely sounding wrong.

---

## Concurrency: exactly one writer to the socket

`AudioSocketSession.send_frames` sets `self._sending = True` on entry and
`stop_sending()` is session-wide. Two concurrent `send_frames` calls would
therefore both interleave their frames on the wire *and* be able to stop each
other.

So before the first real sentence is written, `_reply` must stop the filler
and wait for it to actually finish:

```python
filler_stop.set()          # no further phrases
audio.stop_sending()       # current phrase stops within one frame
await _drain_sender(filler_task)
```

`_drain_sender` is reused rather than cancelling the task outright, for the
reason its docstring already gives: cancelling a sender mid-write abandons a
half-written frame.

---

## Error handling

| Failure | Behaviour |
|---|---|
| TTS fails during `prepare()` at startup | Bank empty, filler silently disabled, 7001 otherwise unchanged |
| A single phrase fails to synthesize | That phrase is dropped; remaining phrases in the tier still usable |
| `filler_delay_ms` is 0 or phrases absent | Filler never starts; no task created |
| Caller hangs up during a filler phrase | `send_frames` already returns on a closed session; the loop exits with the call |
| Reply arrives while a phrase is mid-word | Phrase is stopped and drained, then the real sentence plays. Slight overlap in *time* is avoided; the caller hears a clipped filler, which is preferable to delaying the answer |
| Filler task raises | Logged; the reply proceeds. A broken filler must never fail a call |

---

## Tests

1. No filler when the first sentence arrives before `filler_delay_ms`.
2. Filler plays when the reply is slower than the threshold.
3. Tiers escalate across a long wait and hold at the last tier.
4. Rotation within a tier: two consecutive turns do not use the same phrase.
5. **Filler frames never reach `HeardTracker` and never appear in an
   `append_assistant` payload.**
6. Interrupt during filler stops the audio *and* closes the reply stream, and
   nothing is credited to history.
7. Empty bank (synthesis failed) reproduces today's behaviour exactly.
8. The filler task is always fully drained before the first real sentence is
   written — never two concurrent `send_frames`.
9. `filler_delay_ms: 0` creates no filler task at all.

---

## Non-goals

- **Reducing the actual latency.** Where the 4–10 s goes has not been
  measured; it may be model speed, tool calls, or Hermes overhead. Worth
  investigating separately before assuming filler is the whole answer.
- **Narrating what Hermes is doing.** Using `tool.progress`'s `tool_name` to
  say "checking my notes" was considered and deferred. Note for whoever picks
  it up: that event's `delta` field carries the model's private reasoning and
  must never be spoken — filter on the event name.
- **Filler on 7000.** The turn-based path has no way to speak while waiting;
  it is a blocking request/response loop.
