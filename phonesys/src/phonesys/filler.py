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
                except Exception as exc:
                    logger.warning(
                        "filler phrase %r failed to synthesize; dropping it: "
                        "%s: %s", text, type(exc).__name__, exc,
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
