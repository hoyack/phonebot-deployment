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


TIERS = [["one", "alpha beta"], ["two alpha"], ["three alpha"]]


async def test_prepare_synthesizes_every_phrase_once():
    tts = FakeTts()
    bank = FillerBank(TIERS)
    await bank.prepare(tts, "voice-1")
    assert tts.seen == ["one", "alpha beta", "two alpha", "three alpha"]


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
    tts = FakeTts(fails={"one"})
    bank = FillerBank(TIERS)
    await bank.prepare(tts, "voice-1")
    assert bank.take(0) == bank.take(0)  # only "alpha beta" remains


async def test_total_synthesis_failure_leaves_the_bank_empty():
    """ElevenLabs down at startup must disable filler, not break the call."""
    tts = FakeTts(fails={"one", "alpha beta", "two alpha", "three alpha"})
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
