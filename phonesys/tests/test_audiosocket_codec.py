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
