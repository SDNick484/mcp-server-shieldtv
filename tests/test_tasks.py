"""stop_task(s) and spawn_task: starting background tasks, and stopping ones that don't stop on the first cancel.

Shared by the sibling servers along with tasks.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.

Based on mcp-server-sofabaton's MQTT shutdown tests, without the MQTT: a
Stubborn task drops a cancellation the way asyncio.wait_for can on Python 3.11.
"""

from __future__ import annotations

import asyncio
import gc
import logging

import pytest

from shieldtv_mcp import tasks as tasks_module
from shieldtv_mcp.tasks import (
    _BACKGROUND_TASKS,
    _consume_abandoned_exception,
    spawn_task,
    stop_background_tasks,
    stop_task,
    stop_tasks,
)

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


async def stop_in_its_own_task(
    task: asyncio.Task[None], timeout: float = 5.0, grace: float = 0.25
) -> asyncio.Task[None]:
    """Run stop_task in a separate task, waiting at most 3 s for it.

    Not `asyncio.wait_for(stop_task(...))`: a timeout works by cancelling the
    awaiting task, and a stop that swallowed its caller's cancellation would
    pass that check while still hung. Watching it from outside can't be fooled.

    `grace` defaults to 0.25 s here, not stop_task's 1 s, so the dropped-cancel
    rounds these tests exercise come quickly; the default is tested on its own.
    """
    stopping = asyncio.create_task(stop_task(task, timeout, "the test task", grace=grace))
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


async def test_a_cleanup_that_takes_a_while_is_not_cut_off(start):
    """The reason for `grace`: a second cancel lands wherever the task is awaiting, including in the
    `finally:` where it closes its connection. With the default grace, a 0.6 s close completes."""
    state: dict[str, bool] = {}

    async def worker() -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            try:
                await asyncio.sleep(0.6)  # an MQTT DISCONNECT + socket close, say
                state["closed_cleanly"] = True
            except asyncio.CancelledError:
                state["cleanup_interrupted"] = True
                raise

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    loop = asyncio.get_running_loop()
    t = loop.time()
    await stop_task(task, 5.0, "the test task")  # default grace
    assert state == {"closed_cleanly": True} and task.cancelled()
    assert 0.5 < loop.time() - t < 1.0  # ended when the cleanup did, not at the next cancel


async def test_a_short_grace_does_cut_the_cleanup_off(start):
    """The flip side, so the default's value is visible: with grace shorter than the cleanup, the
    cleanup is interrupted (this is what every stop_task call did before `grace` existed)."""
    state: dict[str, bool] = {}

    async def worker() -> None:
        try:
            await asyncio.sleep(3600)
        finally:
            try:
                await asyncio.sleep(0.6)
                state["closed_cleanly"] = True
            except asyncio.CancelledError:
                state["cleanup_interrupted"] = True
                raise

    task = asyncio.create_task(worker())
    await asyncio.sleep(0.05)
    await stop_task(task, 5.0, "the test task", grace=0.1)
    assert state == {"cleanup_interrupted": True}


async def test_a_dropped_cancel_is_still_retried_with_the_default_grace(start):
    worker, task = await start(drops=1)
    loop = asyncio.get_running_loop()
    t = loop.time()
    await stop_in_its_own_task(task, grace=tasks_module.DEFAULT_GRACE)
    assert worker.dropped == 1 and task.done()
    assert 0.9 < loop.time() - t < 1.6  # the second cancel went out after about a second, not at once


async def test_grace_longer_than_the_timeout_still_returns_on_time(start, caplog):
    caplog.set_level(logging.WARNING)
    _, task = await start(drops=None)
    loop = asyncio.get_running_loop()
    t = loop.time()
    await stop_in_its_own_task(task, timeout=0.5, grace=10.0)
    assert loop.time() - t < 1.0 and not task.done()
    assert "didn't stop within" in caplog.text


async def test_negative_grace_is_refused():
    with pytest.raises(ValueError, match="grace"):
        await stop_tasks([asyncio.create_task(asyncio.sleep(0))], 1.0, grace=-1)


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


# --- stop_tasks: several at once, within one timeout -----------------------------------------------


async def stop_all_in_its_own_task(
    tasks: list[asyncio.Task[None] | None], timeout: float, grace: float = 0.25
) -> float:
    """Run stop_tasks in a separate task (see stop_in_its_own_task) and return how long it took."""
    loop = asyncio.get_running_loop()
    t = loop.time()
    stopping = asyncio.create_task(stop_tasks(tasks, timeout, "the test tasks", grace=grace))
    done, _ = await asyncio.wait({stopping}, timeout=3)
    assert stopping in done, "stop_tasks hung"
    stopping.result()
    return loop.time() - t


async def test_nothing_to_stop_is_fine():
    await stop_tasks([], 1.0)
    await stop_tasks([None, None], 1.0)


async def test_stubborn_tasks_are_stopped_in_parallel(start):
    # Each drops two cancels, so each needs about three rounds (~0.5 s) to stop. One after
    # another that's ~1.5 s for three; in parallel it's the same ~0.5 s, inside the timeout.
    started = [await start(drops=2) for _ in range(3)]
    took = await stop_all_in_its_own_task([task for _, task in started], timeout=1.0)
    assert all(task.done() for _, task in started)
    assert all(worker.dropped == 2 for worker, _ in started)
    assert took <= 1.0


async def test_the_timeout_is_shared_not_per_task(start, caplog):
    caplog.set_level(logging.WARNING)
    started = [await start(drops=None) for _ in range(3)]  # none of them ever exits
    took = await stop_all_in_its_own_task([task for _, task in started], timeout=0.5)
    assert took < 0.9, "the timeouts added up"  # 3 x 0.5 s if they ran one after another
    assert not any(task.done() for _, task in started)
    assert "the test tasks didn't stop within 0s; abandoning 3 of 3" in caplog.text


async def test_only_the_stragglers_are_abandoned(start, caplog):
    caplog.set_level(logging.WARNING)
    _, quick = await start(drops=0)
    _, stuck = await start(drops=None)
    stuck.set_name("stuck-worker")
    await stop_all_in_its_own_task([quick, None, stuck], timeout=0.5)
    assert quick.cancelled() and not stuck.done()
    assert "abandoning 1 of 2 (stuck-worker)" in caplog.text


async def test_finished_tasks_are_skipped_but_their_failures_logged(start, caplog):
    caplog.set_level(logging.DEBUG)

    async def crash() -> None:
        raise RuntimeError("boom")

    crashed = asyncio.create_task(crash(), name="crasher")
    await asyncio.wait({crashed})
    _, running = await start(drops=0)
    await stop_all_in_its_own_task([crashed, running], timeout=1.0)
    assert running.cancelled()
    assert "the test tasks ended with RuntimeError('boom')" in caplog.text


async def test_tasks_that_fail_while_stopping_are_logged(caplog):
    caplog.set_level(logging.DEBUG)

    async def fail_on_cancel() -> None:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError as err:
            raise RuntimeError("cleanup exploded") from err

    task = asyncio.create_task(fail_on_cancel(), name="bad-cleanup")
    await asyncio.sleep(0.05)
    await stop_all_in_its_own_task([task], timeout=1.0)
    assert "the test tasks ended with RuntimeError('cleanup exploded')" in caplog.text


# --- spawn_task: fire and forget, without the forgetting -------------------------------------------


async def test_spawn_task_names_the_task_and_returns_its_result():
    async def work() -> int:
        return 42

    task = spawn_task(work(), name="worker")
    assert task.get_name() == "worker"
    assert await task == 42
    default_named = spawn_task(work())
    assert await default_named == 42


async def test_spawn_task_forwards_a_failure_to_on_error(caplog):
    caplog.set_level(logging.DEBUG)
    errors: list[BaseException] = []

    async def fail() -> None:
        raise ValueError("device went away")

    task = spawn_task(fail(), name="poller", on_error=errors.append)
    await asyncio.wait({task})
    await asyncio.sleep(0)  # done callbacks run on the next loop iteration
    assert [repr(e) for e in errors] == ["ValueError('device went away')"]
    assert "poller" not in caplog.text  # handled, so not also logged


async def test_spawn_task_logs_a_failure_with_no_on_error(caplog):
    caplog.set_level(logging.ERROR)

    async def fail() -> None:
        raise ValueError("device went away")

    task = spawn_task(fail(), name="poller")
    await asyncio.wait({task})
    await asyncio.sleep(0)
    assert "Background task poller failed" in caplog.text
    assert "device went away" in caplog.text  # the traceback came along


async def test_spawn_task_logs_an_on_error_that_fails_itself(caplog):
    caplog.set_level(logging.ERROR)

    def broken_handler(exc: BaseException) -> None:
        raise RuntimeError("handler bug")

    async def fail() -> None:
        raise ValueError("device went away")

    task = spawn_task(fail(), name="poller", on_error=broken_handler)
    await asyncio.wait({task})
    await asyncio.sleep(0)
    assert "on_error for background task poller failed" in caplog.text


async def test_spawn_task_does_not_report_a_cancellation(caplog):
    caplog.set_level(logging.DEBUG)
    errors: list[BaseException] = []
    task = spawn_task(asyncio.sleep(10), name="sleeper", on_error=errors.append)
    await asyncio.sleep(0)
    await stop_task(task, 1.0, "the sleeper")
    await asyncio.sleep(0)
    assert task.cancelled() and errors == [] and "sleeper" not in caplog.text


async def test_spawn_task_keeps_an_unreferenced_task_alive_and_cleans_up():
    # The loop holds tasks weakly: with no reference but ours dropped, a bare create_task's
    # task can be collected while it waits, and never finish. spawn_task holds one in
    # _BACKGROUND_TASKS while it runs, and discards it when it finishes.
    go = asyncio.Event()
    finished = asyncio.Event()

    async def wait_then_finish() -> None:
        await go.wait()
        finished.set()

    spawn_task(wait_then_finish(), name="orphan")  # result deliberately dropped
    await asyncio.sleep(0)
    gc.collect()
    assert any(t.get_name() == "orphan" for t in _BACKGROUND_TASKS)
    go.set()
    await asyncio.wait_for(finished.wait(), timeout=1)
    await asyncio.sleep(0)  # let the done callback discard the task
    assert not any(t.get_name() == "orphan" for t in _BACKGROUND_TASKS)


async def test_consume_abandoned_exception():
    async def fails_later():
        await asyncio.sleep(0.01)
        raise RuntimeError("late crash")

    task = asyncio.create_task(fails_later())
    _consume_abandoned_exception(task)
    await asyncio.sleep(0.05)
    _consume_abandoned_exception(task)

    cancelled_task = asyncio.create_task(asyncio.sleep(10))
    cancelled_task.cancel()
    await asyncio.sleep(0.01)
    _consume_abandoned_exception(cancelled_task)


async def test_stop_background_tasks():
    ran = False

    async def worker():
        nonlocal ran
        try:
            await asyncio.sleep(10)
        finally:
            ran = True

    t1 = spawn_task(worker(), name="bg1")
    t2 = spawn_task(worker(), name="bg2")
    await asyncio.sleep(0)
    assert t1 in _BACKGROUND_TASKS and t2 in _BACKGROUND_TASKS
    await stop_background_tasks(timeout=1.0)
    assert t1.cancelled() and t2.cancelled()
    assert ran is True
