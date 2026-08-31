import asyncio

import pytest
from phonesys.recordings import RecordingWaiter


async def test_resolves_when_event_arrives():
    w = RecordingWaiter()
    fut = w.expect("turn-1")
    w.resolve("turn-1", ok=True)
    assert await asyncio.wait_for(fut, timeout=1) is True


async def test_failed_recording_resolves_false():
    w = RecordingWaiter()
    fut = w.expect("turn-1")
    w.resolve("turn-1", ok=False)
    assert await asyncio.wait_for(fut, timeout=1) is False


async def test_resolving_unknown_name_is_harmless():
    # Events for calls this process does not own must not raise.
    RecordingWaiter().resolve("never-registered")


async def test_double_resolve_is_harmless():
    w = RecordingWaiter()
    fut = w.expect("turn-1")
    w.resolve("turn-1")
    w.resolve("turn-1")
    assert await asyncio.wait_for(fut, timeout=1) is True


async def test_discard_removes_pending_waiter():
    w = RecordingWaiter()
    w.expect("turn-1")
    w.discard("turn-1")
    # A late event after discard must not raise.
    w.resolve("turn-1")
    assert "turn-1" not in w._waiters
