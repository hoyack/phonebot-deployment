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
