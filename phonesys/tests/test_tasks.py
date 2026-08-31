"""Tests for the background task registry.

The bug this guards against is invisible in normal runs and catastrophic when
it bites: asyncio holds only a WEAK reference to a running task, so a task
whose handle is discarded can be collected and cancelled mid-flight. For this
process that means a call being served one moment and gone the next, or a
detached teardown never releasing an Asterisk bridge.
"""
from __future__ import annotations

import asyncio
import gc

from phonesys.tasks import BACKGROUND_TASKS, spawn


async def test_spawned_task_survives_a_forced_gc_while_in_flight():
    finished = asyncio.Event()

    async def slow_work() -> str:
        # Suspends, so the task is genuinely mid-flight (and its only strong
        # reference is whatever spawn() kept) when gc.collect() runs.
        await asyncio.sleep(0.05)
        finished.set()
        return "done"

    task = spawn(slow_work(), name="slow")
    del slow_work                     # no lingering reference via the closure

    await asyncio.sleep(0)            # let it start
    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)

    assert task in BACKGROUND_TASKS, "task was dropped from the registry"
    assert not task.cancelled()
    assert await asyncio.wait_for(task, timeout=2.0) == "done"
    assert finished.is_set()


async def test_a_task_nobody_holds_a_reference_to_still_completes():
    """The real call site: spawn(...) with the return value discarded."""
    ran = asyncio.Event()

    async def work() -> None:
        await asyncio.sleep(0.05)
        ran.set()

    spawn(work())                      # return value deliberately dropped
    for _ in range(3):
        gc.collect()
        await asyncio.sleep(0)

    await asyncio.wait_for(ran.wait(), timeout=2.0)


async def test_finished_tasks_are_discarded_from_the_registry():
    """Otherwise the set is an unbounded leak over a long-lived process."""
    async def work() -> None:
        return None

    task = spawn(work())
    await task
    await asyncio.sleep(0)             # done-callbacks run on the next tick
    assert task not in BACKGROUND_TASKS


async def test_a_failing_task_is_still_discarded():
    async def boom() -> None:
        raise RuntimeError("nope")

    task = spawn(boom())
    await asyncio.gather(task, return_exceptions=True)
    await asyncio.sleep(0)
    assert task not in BACKGROUND_TASKS
