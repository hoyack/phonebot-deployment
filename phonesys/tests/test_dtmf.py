import asyncio

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
