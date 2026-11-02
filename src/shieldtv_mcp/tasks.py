"""Starting and stopping background tasks reliably.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Coroutine, Iterable
from typing import Any, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

# Strong references to the tasks spawn_task started, each dropped when its task finishes. The event
# loop itself only keeps a *weak* reference to a task, so a task nobody else holds can be garbage
# collected mid-await and silently vanish (the asyncio docs' warning on create_task). This set is
# what keeps a fire-and-forget task alive.
_BACKGROUND_TASKS: set[asyncio.Task[Any]] = set()


def _consume_abandoned_exception(task: asyncio.Task[Any]) -> None:
    if task.done() and not task.cancelled():
        task.exception()


async def stop_task(task: asyncio.Task[Any] | None, timeout: float, what: str) -> None:
    """Cancel a background task and wait until it has finished, re-cancelling as needed, for at most `timeout`.

    Why not just `task.cancel(); await task`:
    - One cancel isn't always enough. On Python 3.11, asyncio.wait_for can return the awaited result and
      drop a cancellation that arrives in the same loop iteration (CPython PR #28149; fixed in 3.12). The
      task carries on, and awaiting it hangs shutdown forever. So: cancel, wait briefly, cancel again until
      it's done.
    - Bounded: a task that still won't exit is abandoned with a warning rather than hanging the server.
    - `asyncio.wait` never raises the task's own CancelledError into us, so a cancellation of *our*
      caller still propagates (suppressing CancelledError around `await task` swallowed both).

    The one-task case of `stop_tasks`, which does the work.
    """
    await stop_tasks((task,), timeout, what)


async def stop_tasks(tasks: Iterable[asyncio.Task[Any] | None], timeout: float, what: str = "background tasks") -> None:
    """Stop several background tasks at once, the way `stop_task` stops one, within `timeout` *in total*.

    Calling `stop_task` on each in turn would make the worst case additive: three stubborn workers with
    a 5 s timeout each hold shutdown for 15 s. Here every pending task is cancelled (and re-cancelled,
    for the dropped-cancel case `stop_task` describes) on each round, and one `asyncio.wait` covers them
    all, so they wind down in parallel and the whole call returns by `timeout`.

    `None`s are skipped (a worker that was never started). Tasks that had already finished aren't
    cancelled, but an exception they ended with is still logged, as for those stopped here: retrieving
    it is also what spares asyncio's "Task exception was never retrieved" at garbage collection.
    """
    given = [t for t in tasks if t is not None]
    if not given:
        return
    single = len(given) == 1

    pending: set[asyncio.Task[Any]] = set()
    for task in given:
        if task.done():
            if not task.cancelled() and task.exception() is not None:
                log.debug("%s ended with %r", what, task.exception())
        else:
            pending.add(task)

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while pending:
        left = deadline - loop.time()
        if left <= 0:
            if single:
                log.warning("%s didn't stop within %.0fs; abandoning it", what, timeout)
            else:
                names = ", ".join(sorted(t.get_name() for t in pending))
                log.warning(
                    "%s didn't stop within %.0fs; abandoning %d of %d (%s)",
                    what,
                    timeout,
                    len(pending),
                    len(given),
                    names,
                )
            for task in pending:
                task.add_done_callback(_consume_abandoned_exception)
            return
        for task in pending:
            task.cancel()
        done, pending = await asyncio.wait(pending, timeout=min(0.25, left))
        for task in done:
            if not task.cancelled() and task.exception() is not None:
                log.debug("%s ended with %r", what, task.exception())


def spawn_task(
    coro: Coroutine[Any, Any, T],
    *,
    name: str | None = None,
    on_error: Callable[[BaseException], None] | None = None,
) -> asyncio.Task[T]:
    """`asyncio.create_task(coro, name=name)` for a fire-and-forget task: kept alive, and its failure reported.

    Two traps of a bare `create_task` whose result nobody awaits:
    - The loop holds tasks weakly, so a task with no other reference can be garbage collected before it
      finishes. spawn_task holds one until the task is done.
    - An exception that ends the task goes nowhere until the task is collected, and then only as
      "Task exception was never retrieved" on stderr, with no hint of which worker died. spawn_task
      retrieves it as soon as the task ends and logs it (at ERROR, with the traceback and the task's
      name), or hands it to `on_error` instead, e.g. to mark the device offline or restart the worker.

    Cancellation isn't a failure, so it's neither logged nor passed on. The returned task can still be
    awaited or passed to `stop_task(s)` as usual.
    """
    task = asyncio.create_task(coro, name=name)
    _BACKGROUND_TASKS.add(task)

    def done(t: asyncio.Task[T]) -> None:
        _BACKGROUND_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is None:
            return
        if on_error is not None:
            try:
                on_error(exc)
            except Exception:
                log.exception("on_error for background task %s failed handling %r", t.get_name(), exc)
            return
        log.error("Background task %s failed", t.get_name(), exc_info=exc)

    task.add_done_callback(done)
    return task


async def stop_background_tasks(timeout: float = 5.0, what: str = "background tasks") -> None:
    """Cancel and wait for all currently running tasks started by ``spawn_task``."""
    await stop_tasks(tuple(_BACKGROUND_TASKS), timeout, what)
