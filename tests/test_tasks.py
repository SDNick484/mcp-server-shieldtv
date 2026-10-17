"""stop_task: stopping a background task that doesn't stop on the first cancel.

Shared by the sibling servers along with tasks.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.

Based on mcp-server-sofabaton's MQTT shutdown tests, without the MQTT: a
Stubborn task drops a cancellation the way asyncio.wait_for can on Python 3.11.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from shieldtv_mcp.tasks import stop_task

pytestmark = pytest.mark.anyio


class Stubborn:
    """A background task's body that can drop cancellations.

    On Python 3.11, if the result asyncio.wait_for is waiting for and a
    cancellation land in the same loop iteration, wait_for returns the result
    and the cancel is lost (CPython PR #28149; fixed in 3.12). Sofabaton's CI
    hit it about one run in three: the task carried on into its loop, and a
    plain `task.cancel(); await task` waited for it forever. `drops` is how many
    cancellations to drop, or None to drop every one (a task that never exits).
    """

    def __init__(self, drops: int | None = 1) -> None:
        self.drops = drops
        self.dropped = 0

    async def run(self) -> None:
        while True:
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:
                if self.drops is not None and self.dropped >= self.drops:
                    raise
                self.dropped += 1  # the result "won": the cancel is lost

    def give_up(self) -> None:
        """Let the next cancel through, so the test's loop can close cleanly."""
        self.drops = self.dropped


@pytest.fixture
async def start():
    """``worker, task = await start(drops)``: a Stubborn task, already mid-sleep (where a cancel gets dropped).

    Afterwards every one is let go and stopped. That's what makes a broken
    stop_task fail these tests rather than hang them: a task left dropping
    cancels forever would keep the event loop from ever closing.
    """
    started: list[tuple[Stubborn, asyncio.Task[None]]] = []

    async def start_(drops: int | None) -> tuple[Stubborn, asyncio.Task[None]]:
        worker = Stubborn(drops)
        task = asyncio.create_task(worker.run())
        started.append((worker, task))
        await asyncio.sleep(0.05)
        return worker, task

    yield start_
    for worker, task in started:
        worker.give_up()
        task.cancel()
    if started:
        await asyncio.wait([task for _, task in started], timeout=1)


async def stop_in_its_own_task(task: asyncio.Task[None], timeout: float = 5.0) -> asyncio.Task[None]:
    """Run stop_task in a separate task, waiting at most 3 s for it.

    Not `asyncio.wait_for(stop_task(...))`: a timeout works by cancelling the
    awaiting task, and a stop that swallowed its caller's cancellation would
    pass that check while still hung. Watching it from outside can't be fooled.
    """
    stopping = asyncio.create_task(stop_task(task, timeout, "the test task"))
    done, _ = await asyncio.wait({stopping}, timeout=3)
    assert stopping in done, "stop_task hung"
    return stopping


async def test_none_is_nothing_to_stop():
    await stop_task(None, 1.0, "nothing")


async def test_a_cooperative_task_stops_at_once(start):
    _, task = await start(drops=0)
    loop = asyncio.get_running_loop()
    t = loop.time()
    await stop_in_its_own_task(task)
    assert task.cancelled() and loop.time() - t < 0.5


async def test_a_dropped_cancellation_is_retried(start):
    # The bug this exists for: with one cancel and an await, stop never returns.
    worker, task = await start(drops=1)
    await stop_in_its_own_task(task)
    assert worker.dropped == 1 and task.done()


async def test_the_callers_cancellation_still_propagates(start):
    # Suppressing CancelledError around `await task` used to swallow the
    # caller's cancellation as well as the task's.
    _, task = await start(drops=None)  # never exits, so stop_task is still waiting
    stopping = asyncio.create_task(stop_task(task, 5.0, "the test task"))
    await asyncio.sleep(0.1)
    stopping.cancel()  # e.g. the server being shut down harder
    await asyncio.wait({stopping}, timeout=1)
    assert stopping.cancelled(), "stop_task swallowed its caller's cancellation"


async def test_bounded_even_if_the_task_never_exits(start, caplog):
    caplog.set_level(logging.WARNING)
    _, task = await start(drops=None)
    loop = asyncio.get_running_loop()
    t = loop.time()
    await stop_in_its_own_task(task, timeout=0.5)
    assert loop.time() - t < 1.5 and not task.done()
    assert "the test task didn't stop within" in caplog.text


async def test_a_task_that_already_failed_is_logged_not_raised(caplog):
    caplog.set_level(logging.DEBUG)

    async def crash() -> None:
        raise RuntimeError("boom")

    task = asyncio.create_task(crash())
    await asyncio.wait({task})
    await stop_task(task, 1.0, "the crashed task")
    assert "the crashed task ended with RuntimeError('boom')" in caplog.text
