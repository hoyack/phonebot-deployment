"""Small, dependency-free detector for telephone DTMF in 8 kHz PCM audio.

This is a fallback for callers whose carrier/media bridge delivers keypad
tones as ordinary audio instead of RFC 4733 events. Detection is active only
while a protected extension is collecting a PIN.
"""
from __future__ import annotations

import math
import struct

SAMPLE_RATE = 8000
WINDOW_SAMPLES = 160  # 20 ms, matching AudioSocket's slin frame cadence.
LOW_FREQUENCIES = (697, 770, 852, 941)
HIGH_FREQUENCIES = (1209, 1336, 1477, 1633)
DIGITS = (
    ("1", "2", "3", "A"),
    ("4", "5", "6", "B"),
    ("7", "8", "9", "C"),
    ("*", "0", "#", "D"),
)


def _goertzel_power(samples: list[float], frequency: int) -> float:
    coefficient = 2.0 * math.cos(2.0 * math.pi * frequency / SAMPLE_RATE)
    previous = previous_previous = 0.0
    for sample in samples:
        current = sample + coefficient * previous - previous_previous
        previous_previous, previous = previous, current
    return (
        previous_previous * previous_previous
        + previous * previous
        - coefficient * previous * previous_previous
    )


def _classify(samples: tuple[int, ...]) -> str | None:
    mean = sum(samples) / len(samples)
    centered = [sample - mean for sample in samples]
    energy = sum(sample * sample for sample in centered)
    if energy / len(centered) < 400.0**2:
        return None

    low = [_goertzel_power(centered, frequency) for frequency in LOW_FREQUENCIES]
    high = [_goertzel_power(centered, frequency) for frequency in HIGH_FREQUENCIES]
    low_index = max(range(len(low)), key=low.__getitem__)
    high_index = max(range(len(high)), key=high.__getitem__)
    low_peak = low[low_index]
    high_peak = high[high_index]
    low_runner_up = sorted(low)[-2]
    high_runner_up = sorted(high)[-2]

    # Reject speech/noise unless there is one clear peak in each DTMF group.
    if low_peak < 2.5 * max(low_runner_up, 1.0):
        return None
    if high_peak < 2.5 * max(high_runner_up, 1.0):
        return None
    if not 0.1 <= high_peak / max(low_peak, 1.0) <= 10.0:
        return None
    if (low_peak + high_peak) / (len(centered) * energy) < 0.24:
        return None
    return DIGITS[low_index][high_index]


class InbandDtmfDetector:
    """Emit one digit per sustained tone, after a short confirming gap."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self._candidate: str | None = None
        self._candidate_frames = 0
        self._locked: str | None = None
        self._quiet_frames = 0

    def feed(self, pcm: bytes) -> list[str]:
        self._buffer.extend(pcm)
        emitted: list[str] = []
        window_bytes = WINDOW_SAMPLES * 2
        while len(self._buffer) >= window_bytes:
            raw = bytes(self._buffer[:window_bytes])
            del self._buffer[:window_bytes]
            samples = struct.unpack(f"<{WINDOW_SAMPLES}h", raw)
            digit = _classify(samples)
            if digit is None:
                self._candidate = None
                self._candidate_frames = 0
                self._quiet_frames += 1
                if self._quiet_frames >= 2:
                    self._locked = None
                continue

            self._quiet_frames = 0
            if digit == self._candidate:
                self._candidate_frames += 1
            else:
                self._candidate = digit
                self._candidate_frames = 1
            if self._candidate_frames >= 2 and self._locked != digit:
                emitted.append(digit)
                self._locked = digit
        return emitted
