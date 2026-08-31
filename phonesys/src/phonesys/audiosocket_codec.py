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
