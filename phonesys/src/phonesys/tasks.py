"""Registry for fire-and-forget asyncio tasks.

The event loop holds only a WEAK reference to a running task. Discarding the
return value of `create_task`/`ensure_future` therefore does not mean "let it
run unattended" -- it means the task may be garbage-collected mid-flight and
silently cancelled. For this process that is the difference between a call
being served and a call vanishing, and between StreamingCallHandler's
deliberately detached teardown running and an Asterisk bridge plus two
channels being leaked for the life of the process.

Every task nobody awaits is therefore parked in this module-level set until
it completes, and removed by its own done-callback.
"""
from __future__ import annotations

import asyncio
from typing import Any, Coroutine

BACKGROUND_TASKS: set[asyncio.Task] = set()


def spawn(coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task:
    """Start `coro` and keep a strong reference to it until it finishes."""
    task = asyncio.ensure_future(coro)
    if name is not None:
        task.set_name(name)
    BACKGROUND_TASKS.add(task)
    # discard, not remove: the callback also fires for a task cancelled during
    # loop shutdown, and a KeyError there would be logged as an unhandled
    # exception in a callback.
    task.add_done_callback(BACKGROUND_TASKS.discard)
    return task
