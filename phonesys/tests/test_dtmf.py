import asyncio
import math
import struct

from phonesys.waiters import DtmfCollector


async def test_collects_exact_digit_count_for_active_channel():
    collector = DtmfCollector()
    task = asyncio.create_task(collector.collect("caller-1", 6, timeout=1))
    await asyncio.sleep(0)
    for digit in "481726":
        collector.feed("caller-1", digit)
    assert await task == "481726"


async def test_ignores_digits_from_other_channels():
    collector = DtmfCollector()
    task = asyncio.create_task(collector.collect("caller-1", 2, timeout=1))
    await asyncio.sleep(0)
    collector.feed("caller-2", "9")
    collector.feed("caller-1", "4")
    collector.feed("caller-1", "8")
    assert await task == "48"


async def test_timeout_returns_none_and_discards_waiter():
    collector = DtmfCollector()
    assert await collector.collect("caller-1", 6, timeout=0.01) is None
    collector.feed("caller-1", "4")  # late digits are safely ignored


def _tone_frames(digits: str):
    frequencies = {
        "1": (697, 1209), "2": (697, 1336), "3": (697, 1477),
        "4": (770, 1209), "5": (770, 1336), "6": (770, 1477),
        "7": (852, 1209), "8": (852, 1336), "9": (852, 1477),
        "*": (941, 1209), "0": (941, 1336), "#": (941, 1477),
    }
    sample_offset = 0
    for digit in digits:
        low, high = frequencies[digit]
        for _ in range(8):
            samples = [
                int(7000 * (math.sin(2 * math.pi * low * (sample_offset + i) / 8000)
                            + math.sin(2 * math.pi * high * (sample_offset + i) / 8000)) / 2)
                for i in range(160)
            ]
            sample_offset += 160
            yield struct.pack("<160h", *samples)
        for _ in range(4):
            sample_offset += 160
            yield bytes(320)


async def test_collects_inband_audio_digits_and_allows_repeated_digits():
    collector = DtmfCollector()
    task = asyncio.create_task(collector.collect("caller-1", 6, timeout=1))
    await asyncio.sleep(0)
    for frame in _tone_frames("112233"):
        collector.feed_audio("caller-1", frame)
    assert await task == "112233"


async def test_inband_audio_is_ignored_outside_active_collection():
    collector = DtmfCollector()
    for frame in _tone_frames("123456"):
        collector.feed_audio("caller-1", frame)
    task = asyncio.create_task(collector.collect("caller-1", 1, timeout=0.01))
    assert await task is None


async def test_inband_detector_rejects_silence_and_single_frequency_audio():
    collector = DtmfCollector()
    task = asyncio.create_task(collector.collect("caller-1", 1, timeout=0.02))
    await asyncio.sleep(0)
    for _ in range(8):
        collector.feed_audio("caller-1", bytes(320))
    for frame_number in range(8):
        samples = [
            int(7000 * math.sin(2 * math.pi * 440 * (frame_number * 160 + i) / 8000))
            for i in range(160)
        ]
        collector.feed_audio("caller-1", struct.pack("<160h", *samples))
    assert await task is None


async def test_inband_detector_rejects_a_single_short_tone_frame():
    collector = DtmfCollector()
    task = asyncio.create_task(collector.collect("caller-1", 1, timeout=0.02))
    await asyncio.sleep(0)
    collector.feed_audio("caller-1", next(_tone_frames("5")))
    assert await task is None
