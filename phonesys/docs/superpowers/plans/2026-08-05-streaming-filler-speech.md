# Filler Speech (extension 7001) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cover the 4–10 s of dead air on extension 7001 between the caller
finishing a sentence and the first word of the reply, with short escalating
stall phrases that the caller can talk over.

**Architecture:** A `FillerBank` pre-synthesizes phrases to µ-law frames at
process startup (a TTS round-trip costs 400–600 ms, most of the gap being
covered). During `_reply`, a background task speaks a phrase after
`filler_delay_ms` and another every `filler_repeat_ms`, escalating tiers. The
task is stopped and drained before the first real sentence, because
`AudioSocketSession` supports exactly one writer.

**Tech Stack:** Python 3.14, asyncio, pytest (`.venv/bin/pytest`), ElevenLabs
via the existing `StreamTts`, no new dependencies.

## Global Constraints

- **Extension 7000 must keep working.** No task modifies `call.py`,
  `pipeline.py`, `recordings.py`, `stt.py`, or `tts.py`.
- **Filler never enters Hermes's history.** Filler frames use a no-op
  `on_sent`; filler text is never appended to `spoken`. See Task 4 Step 1.
- **Filler failure must never fail a call.** An empty bank, a failed
  synthesis, or a raising filler task all degrade to today's behaviour.
- **Exactly one writer to the AudioSocket.** `send_frames` sets
  `_sending = True` on entry and `stop_sending()` is session-wide, so two
  concurrent `send_frames` calls interleave frames *and* stop each other.
- Run the full suite with `.venv/bin/pytest -q` before every commit.
- Filler frames carry sentence index `FILLER_SENTENCE_INDEX = -1`.

## Deviation from the spec — read before Task 5

The spec says the stale reply is abandoned when `interrupt` fires. Implementing
it that way is unsafe: `interrupt` is set by *any* confirmed interim burst
(`SpeechBurst`), so line noise or a cough during the thinking window would
abandon a reply the caller never heard a word of, leaving them in total
silence with no answer coming. That is strictly worse than today.

This plan therefore splits the two effects, which preserves the behaviour the
user chose while making it safe:

| Trigger | Effect | Latency |
|---|---|---|
| `interrupt` (interim burst confirmed) | Filler audio stops immediately | ~300 ms |
| `utterance_ready` (a complete new utterance banked) | Reply stream abandoned | ~1 s (Deepgram `UtteranceEnd`) |

The caller still hears the filler stop within ~300 ms, and a genuine new
question still discards the stale answer. A cough only silences the filler.

---

## File Structure

| File | Responsibility |
|---|---|
| `src/phonesys/filler.py` | **new** — `FillerBank`: phrase tiers, startup synthesis, rotation |
| `src/phonesys/config.py` | **modify** — three new `ExtensionConfig` fields + parsing |
| `config.yaml` | **modify** — filler settings under `"7001"` only |
| `src/phonesys/main.py` | **modify** — build and `prepare()` the bank, pass to the handler |
| `src/phonesys/stream_call.py` | **modify** — filler task in `_reply`, abandon-on-new-utterance |
| `tests/test_filler.py` | **new** — `FillerBank` unit tests |
| `tests/test_config.py` | **modify** — parsing and defaults |
| `tests/test_main.py` | **modify** — startup wiring |
| `tests/test_stream_call.py` | **modify** — filler behaviour in `_reply` |

---

### Task 1: `FillerBank`

**Files:**
- Create: `src/phonesys/filler.py`
- Test: `tests/test_filler.py`

**Interfaces:**
- Consumes: `StreamTts.synthesize_frames(text, voice_id, sentence_index) -> list[tuple[bytes, int]]` (raises `TtsStreamError`)
- Produces:
  - `FILLER_SENTENCE_INDEX: int = -1`
  - `FillerBank(tiers: Sequence[Sequence[str]])`
  - `async FillerBank.prepare(tts, voice_id) -> None`
  - `FillerBank.take(tier: int) -> list[tuple[bytes, int]] | None`
  - `FillerBank.is_empty() -> bool`
  - `FillerBank.last_tier -> int` (property)

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_filler.py
import pytest

from phonesys.filler import FILLER_SENTENCE_INDEX, FillerBank
from phonesys.stream_tts import TtsStreamError


class FakeTts:
    """Returns one frame per word so tests can tell phrases apart by length."""

    def __init__(self, fails: set[str] | None = None) -> None:
        self.seen: list[str] = []
        self._fails = fails or set()

    async def synthesize_frames(self, text, voice_id, sentence_index):
        self.seen.append(text)
        if text in self._fails:
            raise TtsStreamError(f"synthesis failed for {text!r}")
        return [(b"\x00" * 320, sentence_index) for _ in text.split()]


TIERS = [["one alpha", "one beta"], ["two alpha"], ["three alpha"]]


async def test_prepare_synthesizes_every_phrase_once():
    tts = FakeTts()
    bank = FillerBank(TIERS)
    await bank.prepare(tts, "voice-1")
    assert tts.seen == ["one alpha", "one beta", "two alpha", "three alpha"]


async def test_frames_are_tagged_with_the_filler_sentinel():
    """Filler must be distinguishable from reply audio: a real sentence index
    would let it be credited to the caller's history."""
    bank = FillerBank(TIERS)
    await bank.prepare(FakeTts(), "voice-1")
    frames = bank.take(0)
    assert frames and all(i == FILLER_SENTENCE_INDEX for _, i in frames)


async def test_take_rotates_within_a_tier():
    """Two consecutive turns must not open with the same words."""
    bank = FillerBank(TIERS)
    await bank.prepare(FakeTts(), "voice-1")
    first, second, third = bank.take(0), bank.take(0), bank.take(0)
    assert first != second
    assert third == first  # wraps around


async def test_take_clamps_above_the_highest_tier():
    bank = FillerBank(TIERS)
    await bank.prepare(FakeTts(), "voice-1")
    assert bank.take(99) == bank.take(2)


async def test_a_tier_whose_phrases_all_failed_falls_back_to_a_lower_tier():
    """A tier that produced no audio must not play as silence."""
    tts = FakeTts(fails={"three alpha"})
    bank = FillerBank(TIERS)
    await bank.prepare(tts, "voice-1")
    assert bank.last_tier == 1
    assert bank.take(2) == bank.take(1)


async def test_one_failed_phrase_does_not_lose_its_siblings():
    tts = FakeTts(fails={"one alpha"})
    bank = FillerBank(TIERS)
    await bank.prepare(tts, "voice-1")
    assert bank.take(0) == bank.take(0)  # only "one beta" remains


async def test_total_synthesis_failure_leaves_the_bank_empty():
    """ElevenLabs down at startup must disable filler, not break the call."""
    tts = FakeTts(fails={"one alpha", "one beta", "two alpha", "three alpha"})
    bank = FillerBank(TIERS)
    await bank.prepare(tts, "voice-1")
    assert bank.is_empty()
    assert bank.take(0) is None


async def test_no_configured_phrases_is_empty_without_calling_tts():
    tts = FakeTts()
    bank = FillerBank([])
    await bank.prepare(tts, "voice-1")
    assert bank.is_empty()
    assert bank.take(0) is None
    assert tts.seen == []


async def test_a_phrase_that_yields_no_frames_is_dropped():
    """_frames_from_ulaw drops a trailing sub-20ms tail, so a very short
    phrase can synthesize to zero frames. Queuing it would be silence."""

    class EmptyTts:
        async def synthesize_frames(self, text, voice_id, sentence_index):
            return []

    bank = FillerBank([["hm"]])
    await bank.prepare(EmptyTts(), "voice-1")
    assert bank.is_empty()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_filler.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'phonesys.filler'`

- [ ] **Step 3: Write the implementation**

```python
# src/phonesys/filler.py
"""Pre-synthesized stall phrases spoken while Hermes is generating a reply.

Synthesized once at process startup rather than on demand: an ElevenLabs
round-trip costs 400-600ms, which is most of the gap the filler exists to
cover, so synthesizing at the moment of need would defeat the feature.

Nothing here can fail a call. If synthesis fails the bank is simply empty and
the streaming handler behaves exactly as it did before filler existed.
"""
from __future__ import annotations

import logging
from typing import Sequence

logger = logging.getLogger(__name__)

# Filler is not part of any reply, so its frames carry a sentinel index that
# no HeardTracker entry can ever match. See the invariant in the design doc:
# crediting filler would tell Hermes it said words it never generated.
FILLER_SENTENCE_INDEX = -1

Frames = list[tuple[bytes, int]]


class FillerBank:
    """Phrases grouped into escalating tiers, pre-synthesized to frames.

    Tier 0 is spoken first, then tier 1, and so on, holding at the highest
    tier that actually has audio. Within a tier the bank rotates, so two
    consecutive turns do not open with the same words.
    """

    def __init__(self, tiers: Sequence[Sequence[str]]) -> None:
        self._phrases: list[list[str]] = [list(tier) for tier in tiers]
        self._audio: list[list[Frames]] = []
        self._cursor: list[int] = []

    async def prepare(self, tts, voice_id: str) -> None:
        """Synthesize every phrase. Failures are dropped, never raised."""
        self._audio = []
        self._cursor = []
        for tier in self._phrases:
            ready: list[Frames] = []
            for text in tier:
                try:
                    frames = await tts.synthesize_frames(
                        text, voice_id, FILLER_SENTENCE_INDEX
                    )
                except Exception:
                    logger.warning(
                        "filler phrase %r failed to synthesize; dropping it", text
                    )
                    continue
                # A phrase too short to fill one 20ms frame yields nothing;
                # queuing it would play as silence.
                if frames:
                    ready.append(frames)
            self._audio.append(ready)
            self._cursor.append(0)
        if self.is_empty():
            logger.warning("filler bank is empty; filler speech is disabled")

    def is_empty(self) -> bool:
        return not any(self._audio)

    @property
    def last_tier(self) -> int:
        """Highest tier that actually has audio.

        Not simply len(tiers) - 1: if the top tier failed to synthesize,
        escalation must stop at the highest tier that can still be spoken.
        """
        for index in range(len(self._audio) - 1, -1, -1):
            if self._audio[index]:
                return index
        return 0

    def take(self, tier: int) -> Frames | None:
        """Next phrase for `tier`, or None if there is nothing to say."""
        if self.is_empty():
            return None
        index = max(0, min(tier, len(self._audio) - 1))
        # Walk down to the nearest tier holding audio: a tier whose phrases
        # all failed must fall back, not play as silence.
        while index >= 0 and not self._audio[index]:
            index -= 1
        if index < 0:
            return None
        options = self._audio[index]
        frames = options[self._cursor[index] % len(options)]
        self._cursor[index] += 1
        return frames
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_filler.py -q`
Expected: PASS (9 tests)

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass, no existing test affected

- [ ] **Step 6: Commit**

```bash
git add src/phonesys/filler.py tests/test_filler.py
git commit -m "feat: add FillerBank for pre-synthesized stall phrases"
```

---

### Task 2: Configuration

**Files:**
- Modify: `src/phonesys/config.py`
- Modify: `config.yaml`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `ExtensionConfig.filler_delay_ms: int`,
  `ExtensionConfig.filler_repeat_ms: int`,
  `ExtensionConfig.filler_phrases: tuple[tuple[str, ...], ...]`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_config.py`:

```python
def test_filler_defaults_to_disabled(tmp_path, monkeypatch):
    """An extension that does not opt in must be untouched -- including 7000."""
    cfg = _write_and_load(tmp_path, monkeypatch, extra="")
    ext = cfg.extensions["7000"]
    assert ext.filler_delay_ms == 0
    assert ext.filler_repeat_ms == 3000
    assert ext.filler_phrases == ()


def test_filler_settings_are_parsed(tmp_path, monkeypatch):
    cfg = _write_and_load(tmp_path, monkeypatch, extra="""
    filler_delay_ms: 800
    filler_repeat_ms: 2500
    filler_phrases:
      - ["Let me think.", "One moment."]
      - ["Still working."]
""")
    ext = cfg.extensions["7000"]
    assert ext.filler_delay_ms == 800
    assert ext.filler_repeat_ms == 2500
    assert ext.filler_phrases == (
        ("Let me think.", "One moment."),
        ("Still working.",),
    )


def test_filler_phrases_are_hashable(tmp_path, monkeypatch):
    """ExtensionConfig is a frozen dataclass; lists would make it unhashable
    and break any use as a dict key or in a set."""
    cfg = _write_and_load(tmp_path, monkeypatch, extra="""
    filler_phrases:
      - ["Let me think."]
""")
    hash(cfg.extensions["7000"])
```

Add this helper near the top of `tests/test_config.py` if an equivalent does
not already exist — check the file first and reuse the existing fixture style
rather than duplicating it:

```python
def _write_and_load(tmp_path, monkeypatch, extra: str):
    monkeypatch.setenv("ARI_USER", "u")
    monkeypatch.setenv("ARI_PASSWORD", "p")
    monkeypatch.setenv("HERMES_PHONEBOT_API_KEY", "k")
    (tmp_path / "config.yaml").write_text("""
ari:
  base_url: http://127.0.0.1:8088/ari
  app: phonesys
extensions:
  "7000":
    profile: phonebot
    hermes_base_url: http://127.0.0.1:8642
    hermes_api_key_env: HERMES_PHONEBOT_API_KEY
    greeting: "Hi"
    voice_id: v
""" + extra)
    from phonesys.config import load_config
    return load_config(tmp_path / "config.yaml")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: FAIL — `AttributeError: 'ExtensionConfig' object has no attribute 'filler_delay_ms'`

- [ ] **Step 3: Add the fields**

In `src/phonesys/config.py`, add to `ExtensionConfig` after
`audiosocket_port`:

```python
    # Filler speech (streaming mode only). filler_delay_ms == 0 disables it,
    # which is the default: an extension that does not opt in behaves exactly
    # as it did before filler existed.
    filler_delay_ms: int = 0
    filler_repeat_ms: int = 3000
    # Tuples, not lists: ExtensionConfig is frozen and must stay hashable.
    filler_phrases: tuple[tuple[str, ...], ...] = ()
```

And in `load_config`, add to the `ExtensionConfig(...)` call:

```python
            filler_delay_ms=int(ext.get("filler_delay_ms", 0)),
            filler_repeat_ms=int(ext.get("filler_repeat_ms", 3000)),
            filler_phrases=tuple(
                tuple(str(phrase) for phrase in tier)
                for tier in (ext.get("filler_phrases") or [])
            ),
```

- [ ] **Step 4: Add the settings to `config.yaml`**

Under the `"7001"` extension only, after `max_call_seconds`:

```yaml
    # Stall phrases covering the 4-10s Hermes takes to answer. Escalating
    # tiers: the first is spoken after filler_delay_ms, the next one
    # filler_repeat_ms later, holding at the last tier. Edit freely -- these
    # are read at startup and cost nothing but a TTS call each.
    filler_delay_ms: 800
    filler_repeat_ms: 3000
    filler_phrases:
      - ["Let me think about that.", "Give me a second.", "Good question."]
      - ["Still working on that.", "Bear with me."]
      - ["Almost there.", "Nearly done."]
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_config.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/phonesys/config.py config.yaml tests/test_config.py
git commit -m "feat: add filler speech configuration to ExtensionConfig"
```

---

### Task 3: Startup wiring

**Files:**
- Modify: `src/phonesys/main.py:85-136` (`_start_streaming`)
- Test: `tests/test_main.py`

**Interfaces:**
- Consumes: `FillerBank`, `FillerBank.prepare` (Task 1); `ExtensionConfig.filler_*` (Task 2)
- Produces: `StreamingCallHandler(..., filler_bank=<FillerBank>)` keyword argument

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_main.py`:

```python
async def test_filler_bank_is_prepared_at_startup_and_passed_to_the_handler(monkeypatch):
    """Synthesis must happen once at startup, not per call: an ElevenLabs
    round-trip is most of the gap filler exists to cover."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    prepared: list[tuple] = []

    class SpyBank:
        def __init__(self, tiers):
            self.tiers = tiers

        async def prepare(self, tts, voice_id):
            prepared.append((tts, voice_id))

        def is_empty(self):
            return False

    monkeypatch.setattr(main_mod, "FillerBank", SpyBank)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: FakeAriClient())

    await main_mod.main()

    assert len(prepared) == 1, "the bank must be prepared exactly once"
    assert isinstance(instances[0].init_kwargs["filler_bank"], SpyBank)


async def test_a_failing_filler_bank_does_not_stop_streaming(monkeypatch):
    """Filler is a nicety. ElevenLabs being down at startup must not take
    extension 7001 down with it -- the same isolation as a bind failure."""
    cfg = _cfg()
    monkeypatch.setattr(main_mod, "load_config", lambda path: cfg)
    monkeypatch.setattr(main_mod, "AudioSocketServer", FakeAudioSocketServer)

    class ExplodingBank:
        def __init__(self, tiers):
            pass

        async def prepare(self, tts, voice_id):
            raise RuntimeError("elevenlabs is down")

        def is_empty(self):
            return True

    monkeypatch.setattr(main_mod, "FillerBank", ExplodingBank)

    factory, instances = _capturing_factory(RecordingHandler)
    monkeypatch.setattr(main_mod, "StreamingCallHandler", factory)

    fake_ari = FakeAriClient()
    fake_ari.events_to_yield = [_stasis_start("chan-7001", "7001")]
    monkeypatch.setattr(main_mod, "AriClient", lambda ari_cfg: fake_ari)

    await main_mod.main()
    await asyncio.sleep(0)

    # The streaming call was still dispatched, not rejected.
    assert instances and instances[0].calls == [("chan-7001", "7001", cfg.extensions["7001"])]
    assert fake_ari.hungup == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_main.py -q`
Expected: FAIL — `AttributeError: module 'phonesys.main' has no attribute 'FillerBank'`

- [ ] **Step 3: Implement the wiring**

In `src/phonesys/main.py`, add the import:

```python
from phonesys.filler import FillerBank
```

In `_start_streaming`, replace the `return StreamingCallHandler(...)` block
with:

```python
    # Hoisted out of the constructor call below so the same client synthesizes
    # the filler phrases and the replies.
    stream_tts = StreamTts(api_key=os.environ["ELEVENLABS_API_KEY"])

    # Prepared here, once, rather than per call. A failure must not take 7001
    # down: an empty bank simply means no filler, exactly like before it
    # existed. Same isolation rule as the bind failure above.
    filler_bank = FillerBank(ext.filler_phrases)
    if ext.filler_delay_ms > 0 and ext.filler_phrases:
        try:
            await filler_bank.prepare(stream_tts, ext.voice_id)
        except Exception:
            logger.exception(
                "filler bank failed to prepare; continuing without filler speech"
            )
    else:
        logger.info("filler speech not configured for extension in streaming mode")

    # Passed by keyword, not position: this constructor takes seven arguments,
    # four of which are opaque callables/clients, so a reordering would be
    # accepted silently by both Python and any test that only counts them.
    return StreamingCallHandler(
        ari=ari,
        stt_factory=lambda: LiveStt(api_key=os.environ["DEEPGRAM_API_KEY"]),
        tts=stream_tts,
        hermes_factory=lambda ext: HermesClient(
            base_url=ext.hermes_base_url, api_key=ext.hermes_api_key
        ),
        audiosocket_server=as_server,
        ari_app=cfg.ari.app,
        filler_bank=filler_bank,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_main.py -q`
Expected: PASS. `test_streaming_handler_gets_each_dependency_in_the_right_slot`
must still pass — it binds against the real signature, so Task 4's constructor
change is what makes it accept `filler_bank`. If it fails here, do Task 4 Step 3
first, then re-run.

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 6: Commit**

```bash
git add src/phonesys/main.py tests/test_main.py
git commit -m "feat: prepare the filler bank at startup and pass it to the streaming handler"
```

---

### Task 4: Speak filler during the wait

**Files:**
- Modify: `src/phonesys/stream_call.py` (constructor, `_reply`)
- Test: `tests/test_stream_call.py`

**Interfaces:**
- Consumes: `FillerBank.take`, `FillerBank.is_empty`, `FillerBank.last_tier` (Task 1); `ExtensionConfig.filler_delay_ms`, `filler_repeat_ms` (Task 2)
- Produces: `StreamingCallHandler(..., filler_bank: FillerBank | None = None)`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_stream_call.py`. Extend the existing `_handler` helper
to accept `filler_bank=None` and forward it as a keyword argument.

```python
class FakeBank:
    """Returns one distinguishable frame per tier."""

    def __init__(self, tiers=3, empty=False):
        self._tiers, self._empty = tiers, empty
        self.taken: list[int] = []

    def is_empty(self):
        return self._empty

    @property
    def last_tier(self):
        return self._tiers - 1

    def take(self, tier):
        if self._empty:
            return None
        self.taken.append(tier)
        return [(bytes([tier]) * 320, -1)]


async def test_no_filler_when_the_reply_is_fast():
    """Under the threshold the caller must hear nothing extra -- this is the
    common case for short replies."""
    bank = FakeBank()
    ext = _ext(filler_delay_ms=200, filler_repeat_ms=200)
    hermes = FakeHermes(deltas=["Quick answer."])
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken == [], "filler played despite a fast reply"


async def test_filler_plays_when_the_reply_is_slow():
    bank = FakeBank()
    ext = _ext(filler_delay_ms=20, filler_repeat_ms=20)
    hermes = FakeHermes(deltas=["Slow answer."], first_delta_delay=0.20)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken, "no filler played during a slow reply"


async def test_filler_escalates_tiers_and_holds_at_the_last():
    bank = FakeBank(tiers=2)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken[:2] == [0, 1]
    assert set(bank.taken[2:]) <= {1}, "escalation ran past the highest tier"


async def test_filler_is_never_credited_to_the_caller_history():
    """THE invariant: Hermes did not generate 'Let me think about that.'
    Crediting it corrupts the transcript HeardTracker exists to keep honest."""
    bank = FakeBank()
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Real reply."], first_delta_delay=0.10)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    for text in hermes.appended:
        assert "filler" not in text.lower()
    assert all(t == "Real reply." for t in hermes.appended) or not hermes.appended


async def test_filler_is_drained_before_the_first_real_sentence():
    """send_frames sets _sending=True on entry and stop_sending() is
    session-wide, so two concurrent writers interleave frames AND stop each
    other. There must never be two send_frames in flight."""
    bank = FakeBank()
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.15)
    audio = FakeAudio(track_concurrency=True)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext, audio=audio)
    assert audio.max_concurrent_senders <= 1


async def test_an_empty_bank_behaves_exactly_as_before():
    bank = FakeBank(empty=True)
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.10)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken == []


async def test_filler_delay_of_zero_creates_no_filler_task():
    bank = FakeBank()
    ext = _ext(filler_delay_ms=0)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.10)
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext)
    assert bank.taken == []


async def test_interrupt_stops_filler_audio_immediately():
    bank = FakeBank()
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Answer."], first_delta_delay=0.40)
    audio = FakeAudio()
    h = _handler(hermes=hermes, filler_bank=bank)
    await _run_one_turn(h, ext, audio=audio, interrupt_after=0.05)
    assert audio.stop_sending_calls >= 1
```

`FakeAudio`, `FakeHermes`, `_ext` and `_handler` already exist in this file.
Extend them rather than adding parallel fakes:
- `FakeHermes` gains `deltas` and `first_delta_delay` so `chat_stream` sleeps
  before yielding its first delta.
- `FakeAudio` gains `track_concurrency` (incrementing a counter on
  `send_frames` entry, decrementing on exit, recording the maximum) and
  `stop_sending_calls`.
- `_ext` already accepts `**over`, so the new config fields work once Task 2
  lands.
- Add a `_run_one_turn(handler, ext, audio=None, interrupt_after=None)` helper
  that drives a single utterance through `handle()` and returns when the reply
  is done, following the shape of the existing turn-driving tests in this file.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stream_call.py -q`
Expected: FAIL — `TypeError: StreamingCallHandler.__init__() got an unexpected keyword argument 'filler_bank'`

- [ ] **Step 3: Add the constructor parameter**

In `StreamingCallHandler.__init__`, add after `ari_app: str`:

```python
        filler_bank: FillerBank | None = None,
```

and in the body:

```python
        # Optional: None, or an empty bank, means no filler and exactly the
        # behaviour this handler had before filler existed.
        self._filler_bank = filler_bank
```

Add the import at the top of `stream_call.py`:

```python
from phonesys.filler import FillerBank
```

- [ ] **Step 4: Add the filler machinery to `_reply`**

Add this module-level function to `stream_call.py`, next to `_drain_sender`:

```python
def _discard_frame(_sentence_index: int) -> None:
    """on_sent for filler frames.

    Filler is not part of the reply, so it must never reach the reply's
    HeardTracker: crediting it would make append_assistant tell Hermes it
    said words it never generated.
    """
```

At the top of `_reply`, after `index = 0`:

```python
        bank = self._filler_bank
        filler_stop = asyncio.Event()
        filler_task: asyncio.Task | None = None
        interrupt_watch: asyncio.Task | None = None

        async def run_filler() -> None:
            """Speak escalating stall phrases until the reply is ready.

            Sleeps wait on filler_stop rather than plain asyncio.sleep: a
            bare sleep would keep the task alive for a full repeat interval
            after it was told to stop, and _drain_sender would time out.
            """
            try:
                await _wait_any((filler_stop,), timeout=ext.filler_delay_ms / 1000.0)
                tier = 0
                while not filler_stop.is_set():
                    frames = bank.take(tier)
                    if frames is None:
                        return
                    await audio.send_frames(frames, on_sent=_discard_frame)
                    if filler_stop.is_set():
                        return
                    tier = min(tier + 1, bank.last_tier)
                    await _wait_any((filler_stop,),
                                    timeout=ext.filler_repeat_ms / 1000.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # A broken filler must never fail a call.
                logger.exception("filler failed; continuing without it")

        async def silence_filler_on_interrupt() -> None:
            """Stop filler audio the moment the caller talks over it.

            Only the AUDIO stops here. Abandoning the reply is driven by a
            complete new utterance instead -- see _next_delta -- because
            `interrupt` also fires on line noise, and abandoning on that
            would leave the caller with silence and no answer coming.
            """
            await interrupt.wait()
            filler_stop.set()
            audio.stop_sending()

        async def stop_filler() -> None:
            """Guarantee the filler is finished before anything else writes.

            _drain_sender rather than a bare cancel: cancelling a sender
            mid-write abandons a half-written frame.
            """
            nonlocal filler_task, interrupt_watch
            if interrupt_watch is not None:
                interrupt_watch.cancel()
                interrupt_watch = None
            if filler_task is None:
                return
            task, filler_task = filler_task, None
            filler_stop.set()
            audio.stop_sending()
            await _drain_sender(task)

        if (bank is not None and not bank.is_empty()
                and ext.filler_delay_ms > 0):
            filler_task = asyncio.create_task(run_filler())
            interrupt_watch = asyncio.create_task(silence_filler_on_interrupt())
```

Change `speak_sentence` so its first statement is:

```python
        async def speak_sentence(text: str) -> None:
            nonlocal index
            # Before ANY reply audio: exactly one writer on the socket.
            await stop_filler()
            if ended.is_set():
                raise _CallEnded
```

Wrap the existing `try:` / `except _Interrupted:` … block of `_reply` so the
filler is always cleaned up. Add a `finally` to that existing statement:

```python
        finally:
            # Every exit path -- interrupted, time limit, TTS failure, call
            # ended, or success -- must leave no filler task running.
            await stop_filler()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stream_call.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/phonesys/stream_call.py tests/test_stream_call.py
git commit -m "feat: speak escalating filler phrases while Hermes generates a reply"
```

---

### Task 5: Abandon a stale reply when the caller asks something new

**Files:**
- Modify: `src/phonesys/stream_call.py` (`_reply`, plus a new module function)
- Test: `tests/test_stream_call.py`

**Interfaces:**
- Consumes: `_Interrupted` (existing), `utterance_ready: asyncio.Event` (must now be passed into `_reply`)
- Produces: `_next_delta(stream, abandon: asyncio.Event) -> str | None`

Read the "Deviation from the spec" section at the top of this plan before
starting. The trigger here is `utterance_ready`, **not** `interrupt`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_a_new_utterance_during_the_wait_abandons_the_stale_reply():
    """The caller asked something else. Answering the previous question would
    talk over them with a stale answer -- the exact failure barge-in exists
    to prevent."""
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Stale answer."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=FakeBank())
    audio = FakeAudio()
    await _run_one_turn(h, ext, audio=audio, new_utterance_after=0.05)
    assert "Stale answer." not in audio.spoken_text
    assert hermes.stream_closed, "the SSE stream must be closed, not leaked"


async def test_line_noise_during_the_wait_does_not_abandon_the_reply():
    """interrupt fires on any confirmed interim burst, including noise. If
    that abandoned the reply the caller would get silence and no answer."""
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Real answer."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=FakeBank())
    audio = FakeAudio()
    await _run_one_turn(h, ext, audio=audio, interrupt_after=0.05)
    assert "Real answer." in audio.spoken_text


async def test_nothing_is_credited_when_the_reply_is_abandoned_before_a_word():
    """No sentence was spoken, so there is nothing the caller heard. An empty
    credit must not be posted to Hermes."""
    ext = _ext(filler_delay_ms=10, filler_repeat_ms=10)
    hermes = FakeHermes(deltas=["Stale."], first_delta_delay=0.30)
    h = _handler(hermes=hermes, filler_bank=FakeBank())
    await _run_one_turn(h, ext, new_utterance_after=0.05)
    assert hermes.appended == []
```

Extend `FakeHermes` with a `stream_closed` flag set in the generator's
`finally`, and `FakeAudio` with `spoken_text` accumulating the text of frames
written. Extend `_run_one_turn` with a `new_utterance_after` parameter that
sets `utterance_ready` after the given delay.

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/pytest tests/test_stream_call.py -q`
Expected: FAIL — the stale answer is still spoken

- [ ] **Step 3: Add the racing delta reader**

Add to `stream_call.py`, next to `_drain_sender`:

```python
async def _next_delta(stream, abandon: asyncio.Event) -> str | None:
    """Next reply delta, None at end of stream, or raise _Interrupted.

    Without this race, _reply blocks in `async for delta in stream` and a
    caller who asks something new during the 4-10s wait is not noticed until
    a whole sentence has already been spoken at them.

    `abandon` is deliberately utterance_ready and NOT interrupt: interrupt
    fires on any confirmed interim burst, so line noise would discard a reply
    the caller never heard a word of and leave them with no answer coming.
    """
    nxt = asyncio.create_task(anext(stream))
    stop = asyncio.create_task(abandon.wait())
    try:
        done, _ = await asyncio.wait(
            {nxt, stop}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        stop.cancel()
    # Prefer the delta when both are ready: a delta already in hand is not
    # worth discarding, and the next call round will see `abandon` again.
    if nxt in done:
        try:
            return nxt.result()
        except StopAsyncIteration:
            return None
    nxt.cancel()
    raise _Interrupted
```

- [ ] **Step 4: Pass `utterance_ready` into `_reply` and use the race**

In `handle()`, change the `_reply` call to pass `utterance_ready`:

```python
                        await self._reply(audio, ext, hermes, session_id,
                                          utterance, interrupt, utterance_ready,
                                          ended, deadline)
```

Change the `_reply` signature to match:

```python
    async def _reply(self, audio: AudioSocketSession, ext: ExtensionConfig,
                     hermes: HermesClient, session_id: str, utterance: str,
                     interrupt: asyncio.Event, utterance_ready: asyncio.Event,
                     ended: asyncio.Event, deadline: float) -> None:
```

Replace the `async for delta in stream:` loop body with:

```python
            async with contextlib.aclosing(
                hermes.chat_stream(session_id, utterance)
            ) as stream:
                while True:
                    delta = await _next_delta(stream, utterance_ready)
                    if delta is None:
                        break
                    # Also checked here, not only in speak_sentence: a reply
                    # that streams tokens without ever completing a sentence
                    # would otherwise run past the deadline unchecked.
                    if loop.time() >= deadline:
                        raise _TimeLimit
                    for sentence in buf.feed(delta):
                        await speak_sentence(sentence)
```

`handle()` already clears `utterance_ready` before calling `_reply`, so a
stale set flag cannot abandon a reply the instant it starts. Verify this by
reading the `if utterance_ready.is_set() or stalled:` branch — the
`utterance_ready.clear()` on its first line is what makes this safe.

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/pytest tests/test_stream_call.py -q`
Expected: PASS

- [ ] **Step 6: Run the full suite**

Run: `.venv/bin/pytest -q`
Expected: all pass

- [ ] **Step 7: Commit**

```bash
git add src/phonesys/stream_call.py tests/test_stream_call.py
git commit -m "feat: abandon a stale reply when the caller asks something new"
```

---

### Task 6: Live verification

**Files:** none — this is a human step, and it is the only thing that can
confirm the feature actually works. Every prior task is unit-tested against
fakes; none of them prove a caller hears anything.

- [ ] **Step 1: Restart the bridge**

```bash
setsid nohup .venv/bin/python -m phonesys.main > /tmp/phonesys-filler.log 2>&1 < /dev/null &
sleep 5 && tail -3 /tmp/phonesys-filler.log
```

Expected: `audiosocket listening on 0.0.0.0:9010` and
`listening for calls on ARI app phonesys`, with no filler warnings. A
`filler bank is empty` warning means synthesis failed — investigate before
calling.

- [ ] **Step 2: Regression-check 7000**

Dial 7000. Speak, let it reply, pause several seconds, speak again. Expected:
no filler (7000 is not configured for it), no change from today, and the call
does not drop during the pause.

- [ ] **Step 3: Check the fast path on 7001**

Dial 7001 and ask something short ("what time is it?"). Expected: **no filler
at all** if the reply starts within 800 ms. A stall phrase on a fast reply
means `filler_delay_ms` is too low.

- [ ] **Step 4: Check the slow path on 7001**

Ask something that takes Hermes several seconds ("tell me a story about a
lighthouse"). Expected: a stall phrase at ~0.8 s, another ~3 s later from the
next tier up, then the real reply. The phrases must not overlap the reply.

- [ ] **Step 5: Talk over a filler phrase**

Ask a slow question, then talk over the stall phrase with a *different*
question. Expected: the phrase stops within ~300 ms, and the answer you get is
to the new question, not the old one.

- [ ] **Step 6: Confirm history is clean**

Ask "what did you just tell me?" Expected: the agent references only its real
replies and never repeats a stall phrase back as something it said.

- [ ] **Step 7: Commit any tuning**

If `filler_delay_ms` or the phrases needed adjusting by ear:

```bash
git add config.yaml
git commit -m "chore: tune filler timing from live calls"
```

---

## Self-Review

**Spec coverage:**

| Spec requirement | Task |
|---|---|
| `FillerBank` with `prepare`/`take`/`is_empty`/`last_tier` | 1 |
| Startup synthesis, not on demand | 1, 3 |
| Failure contained, empty bank disables filler | 1, 3 |
| Rotation within a tier | 1 |
| `last_tier` = highest tier with audio | 1 |
| Three config fields, `config.yaml` for 7001 only | 2 |
| Escalating tiers, holding at the last | 4 |
| Invariant: filler never in Hermes history | 4 |
| One writer to the socket; drain before real audio | 4 |
| Abandon the stale reply on new speech | 5 |
| All nine spec tests | 1 (7–9), 4 (1–5, 8), 5 (6) |

**Placeholder scan:** none — every step has runnable code or an exact command.

**Type consistency:** `take(tier: int) -> list[tuple[bytes, int]] | None`,
`last_tier` a property, and `FILLER_SENTENCE_INDEX = -1` are used identically
in Tasks 1, 3 and 4. `filler_bank` is the keyword in Tasks 3 and 4.
`_next_delta(stream, abandon)` is defined and used only in Task 5.

**Known deviation:** the abandon trigger is `utterance_ready`, not
`interrupt` — see the section at the top of this plan. The spec's
"Behaviour" and "`stream_call.py`" sections should be updated to match before
this plan is executed.
