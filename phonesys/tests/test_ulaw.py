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
