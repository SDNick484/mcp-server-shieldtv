"""TokenBucket, KeyedTokenBucket and refusal: the shared rate-limit mechanism.

Shared by the sibling servers along with ratelimit.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.

Time is a list the test moves forward, so nothing sleeps: the async tests'
fake sleep records how long it was asked to wait and moves that list instead.
"""

from __future__ import annotations

import pytest

from shieldtv_mcp.ratelimit import KeyedTokenBucket, TokenBucket, refusal


def bucket(capacity: float, rate: float) -> tuple[TokenBucket, list[float]]:
    now = [0.0]
    return TokenBucket(capacity, rate, clock=lambda: now[0]), now


def async_bucket(capacity: float, rate: float) -> tuple[TokenBucket, list[float], list[float]]:
    """A bucket on a fake clock whose fake sleep moves that clock: (bucket, now, sleeps asked for)."""
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds
        assert len(sleeps) < 100, "acquire is spinning"

    return TokenBucket(capacity, rate, clock=lambda: now[0], sleep=sleep), now, sleeps


def test_starts_full_and_empties():
    b, _ = bucket(3, 1.0)
    assert [b.take() for _ in range(3)] == [0.0, 0.0, 0.0]
    assert b.take() == pytest.approx(1.0)  # empty: one token arrives in one second


def test_refills_with_time_but_never_past_capacity():
    b, now = bucket(3, 1.0)
    for _ in range(3):
        b.take()
    now[0] = 2.0
    assert b.take(2) == 0.0
    now[0] = 100.0  # long idle: back to full (3), not 98
    assert b.take(3) == 0.0
    assert b.take() == pytest.approx(1.0)


def test_a_refused_take_takes_nothing():
    b, _ = bucket(5, 1.0)
    b.take(4)
    assert b.take(3) == pytest.approx(2.0)  # needs 3, has 1: wait for 2 more
    assert b.take(1) == 0.0  # the refused call didn't spend the one that was there


def test_more_than_capacity_is_never_possible():
    b, _ = bucket(3, 1.0)
    assert b.take(4) == float("inf")
    assert b.take(3) == 0.0  # and it took nothing


def test_slow_rates_work():
    b, now = bucket(1, 1 / 15)  # one per 15 s, as for activity changes
    b.take()
    assert b.take() == pytest.approx(15.0)
    now[0] = 15.0
    assert b.take() == 0.0


@pytest.mark.parametrize(
    ("capacity", "rate"),
    [(0, 1.0), (1, 0), (-1, 1.0), (float("nan"), 1.0), (float("inf"), 1.0), (1.0, float("nan")), (1.0, float("inf"))],
)
def test_nonsense_settings_are_rejected(capacity, rate):
    with pytest.raises(ValueError):
        TokenBucket(capacity, rate)


def test_refusal_message():
    msg = refusal("launch Contra", 11.6, device="tv-1")
    assert msg.startswith("Rate limit on tv-1: refusing to launch Contra (try again in 12s).")
    assert "runaway loops" in msg
    assert (
        refusal("press VolumeUp x30", float("inf"))
        == "Rate limit: refusing to press VolumeUp x30 (it's more than the limit allows at once). "
        "This guards against runaway loops; reduce the count or split the request instead of retrying as-is."
    )
    assert "try again in 1s" in refusal("x", 0.2)  # never "0s"


def test_taking_a_negative_amount_is_rejected():
    b, _ = bucket(3, 1.0)
    b.take()
    with pytest.raises(ValueError):
        b.take(-5)  # would have added 5 tokens, past capacity
    with pytest.raises(ValueError):
        b.take(float("nan"))
    assert b.tokens == 2


def test_refund_gives_tokens_back_but_never_past_capacity():
    b, _ = bucket(3, 1.0)
    b.take(3)
    b.refund(2)
    assert b.take(2) == 0.0
    assert b.take() == pytest.approx(1.0)
    b.refund(10)  # far more than was taken: back to full (3), not 10
    assert b.tokens == 3
    assert b.take(3) == 0.0
    assert b.take() == pytest.approx(1.0)


def test_refund_on_a_full_bucket_changes_nothing():
    b, _ = bucket(3, 1.0)
    b.refund()
    assert b.tokens == 3


def test_refund_counts_refill_first():
    b, now = bucket(3, 1.0)
    b.take(3)
    now[0] = 1.0  # one token refilled
    b.refund(1)
    assert b.tokens == pytest.approx(2.0)
    now[0] = 2.0  # and refill carries on from there, not from the old timestamp
    assert b.tokens == pytest.approx(2.0)
    assert b.take(3) == 0.0


def test_refunding_a_negative_amount_is_rejected():
    b, _ = bucket(3, 1.0)
    with pytest.raises(ValueError):
        b.refund(-1)
    with pytest.raises(ValueError):
        b.refund(float("nan"))
    with pytest.raises(ValueError):
        b.refund(float("inf"))
    assert b.tokens == 3


@pytest.mark.anyio
async def test_acquire_takes_at_once_when_tokens_are_there():
    b, _, sleeps = async_bucket(3, 1.0)
    assert await b.acquire(2) == 0.0
    assert sleeps == []
    assert b.take() == 0.0 and b.take() == pytest.approx(1.0)  # it took 2 of the 3


@pytest.mark.anyio
async def test_acquire_with_a_real_clock_and_no_wait_is_exactly_zero():
    b = TokenBucket(3, 1.0)  # time.monotonic: two readings would differ by a hair
    assert await b.acquire() == 0.0


@pytest.mark.anyio
async def test_acquire_waits_for_tokens():
    b, now, sleeps = async_bucket(5, 2.0)
    b.take(4)
    waited = await b.acquire(3)  # has 1, needs 3: two more at 2/s is one second
    assert waited == pytest.approx(1.0)
    assert sleeps == [pytest.approx(1.0)]  # one sleep, of exactly the wait
    assert now[0] == pytest.approx(1.0)
    assert b.tokens == pytest.approx(0.0, abs=1e-9)


@pytest.mark.anyio
async def test_acquire_at_slow_rates():
    b, now, sleeps = async_bucket(1, 1 / 15)
    b.take()
    assert await b.acquire() == pytest.approx(15.0)
    assert now[0] == pytest.approx(15.0)
    assert len(sleeps) == 1  # no spinning on a float shortfall after the sleep


@pytest.mark.anyio
async def test_acquire_does_not_spin_on_a_float_shortfall():
    # Refilling 3 tokens at 0.7/s for the computed 3/0.7 s gives 2.9999999999999996, and
    # the 6e-16 s sleep that would ask for is too small to move the clock: without slack,
    # acquire would loop forever (async_bucket's sleep fails the test instead).
    b, _, sleeps = async_bucket(3, 0.7)
    b.take(3)
    assert await b.acquire(3, timeout=3 / 0.7) == pytest.approx(3 / 0.7)
    assert len(sleeps) == 1


@pytest.mark.anyio
async def test_acquire_within_the_timeout():
    b, _, _ = async_bucket(2, 1.0)
    b.take(2)
    assert await b.acquire(2, timeout=2.0) == pytest.approx(2.0)  # exactly the timeout is in time


@pytest.mark.anyio
async def test_acquire_times_out_without_sleeping_or_taking():
    b, now, sleeps = async_bucket(5, 1.0)
    b.take(5)
    with pytest.raises(TimeoutError):
        await b.acquire(3, timeout=2.5)  # needs 3 s
    assert sleeps == []  # refill is predictable: no point sleeping to find out
    now[0] = 3.0
    assert b.take(3) == 0.0  # the timed-out call took nothing


@pytest.mark.anyio
async def test_acquire_times_out_on_what_is_left_of_the_timeout():
    b, now, sleeps = async_bucket(2, 1.0)
    b.take(2)
    fake_sleep = b.sleep

    async def sleep_then_lose_the_race(seconds):
        await fake_sleep(seconds)
        assert b.take(1) == 0.0  # another task grabs the token that just arrived

    b.sleep = sleep_then_lose_the_race
    with pytest.raises(TimeoutError):
        await b.acquire(1, timeout=1.5)  # 1 s spent, 1 s more needed, 0.5 s left
    assert sleeps == [pytest.approx(1.0)]
    assert now[0] == pytest.approx(1.0)


@pytest.mark.anyio
async def test_acquire_zero_timeout_is_a_non_blocking_try():
    b, _, sleeps = async_bucket(1, 1.0)
    assert await b.acquire(timeout=0) == 0.0
    with pytest.raises(TimeoutError):
        await b.acquire(timeout=0)
    assert sleeps == []


@pytest.mark.anyio
async def test_acquire_more_than_capacity_is_an_error_not_a_hang():
    b, _, sleeps = async_bucket(3, 1.0)
    with pytest.raises(ValueError, match="cannot acquire more tokens than capacity"):
        await b.acquire(4)
    with pytest.raises(ValueError, match="cannot acquire more tokens than capacity"):
        await b.acquire(4, timeout=100)  # checked before the timeout, which no wait could meet
    assert sleeps == []
    assert b.take(3) == 0.0  # and it took nothing


@pytest.mark.anyio
async def test_acquire_a_negative_amount_is_rejected():
    b, _, _ = async_bucket(3, 1.0)
    with pytest.raises(ValueError):
        await b.acquire(-1)
    with pytest.raises(ValueError):
        await b.acquire(float("nan"))
    assert b.tokens == 3


@pytest.mark.anyio
async def test_reserve_yields_the_wait_and_keeps_the_tokens():
    b, _, _ = async_bucket(2, 1.0)
    b.take(2)
    async with b.reserve(1, refund_on_error=True) as waited:
        assert waited == pytest.approx(1.0)
    assert b.take() == pytest.approx(1.0)  # success: spent, not refunded


@pytest.mark.anyio
async def test_reserve_without_waiting_yields_zero():
    b, _, _ = async_bucket(2, 1.0)
    async with b.reserve(2) as waited:
        assert waited == 0.0
        assert b.tokens == 0  # taken on entry, not on exit


@pytest.mark.anyio
async def test_reserve_refunds_on_error_when_asked():
    b, _, _ = async_bucket(3, 1.0)
    with pytest.raises(ConnectionRefusedError):
        async with b.reserve(2, refund_on_error=True):
            assert b.tokens == 1
            raise ConnectionRefusedError
    assert b.tokens == 3  # all three back


@pytest.mark.anyio
async def test_reserve_keeps_the_tokens_on_error_by_default():
    b, _, _ = async_bucket(3, 1.0)
    with pytest.raises(ConnectionRefusedError):
        async with b.reserve(2):
            raise ConnectionRefusedError
    assert b.tokens == 1


@pytest.mark.anyio
async def test_reserve_passes_timeouts_through_without_refunding():
    b, _, _ = async_bucket(1, 1.0)
    b.take()
    body_ran = False
    with pytest.raises(TimeoutError):
        async with b.reserve(1, timeout=0.5, refund_on_error=True):
            body_ran = True
    assert not body_ran
    assert b.tokens == 0  # nothing refunded that wasn't taken


def test_keyed_buckets_are_independent():
    now = [0.0]
    k = KeyedTokenBucket(2, 1.0, clock=lambda: now[0])
    assert k.take("tv-1") == 0.0 and k.take("tv-1") == 0.0
    assert k.take("tv-1") == pytest.approx(1.0)
    assert k.take("hub-1", 2) == 0.0  # its own full bucket
    now[0] = 1.0
    assert k.take("tv-1") == 0.0


def test_keyed_get_makes_one_bucket_per_key():
    k = KeyedTokenBucket(3, 0.5)
    first = k.get("tv-1")
    assert k.get("tv-1") is first and k.get("tv-2") is not first
    assert (first.capacity, first.rate) == (3, 0.5)


def test_keyed_refund():
    now = [0.0]
    k = KeyedTokenBucket(2, 1.0, lambda: now[0])
    k.take("tv-1", 2)
    k.refund("tv-1")
    assert k.get("tv-1").tokens == 1
    assert k.get("tv-2").tokens == 2  # untouched
    k.refund("tv-2", 5)
    assert k.get("tv-2").tokens == 2  # capped
    with pytest.raises(ValueError):
        k.refund("tv-1", -1)


@pytest.mark.anyio
async def test_keyed_acquire():
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    k = KeyedTokenBucket(1, 0.5, lambda: now[0], sleep)  # clock and sleep positional too
    assert await k.acquire("tv-1") == 0.0
    assert await k.acquire("tv-2") == 0.0
    assert await k.acquire("tv-1") == pytest.approx(2.0)
    assert sleeps == [pytest.approx(2.0)]
    assert await k.acquire("tv-2") == 0.0  # it refilled while tv-1 waited: one clock, separate buckets
    with pytest.raises(TimeoutError):
        await k.acquire("tv-2", timeout=1.0)
    with pytest.raises(ValueError, match="cannot acquire more tokens than capacity"):
        await k.acquire("tv-1", 2)


@pytest.mark.anyio
async def test_keyed_reserve():
    now = [0.0]
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now[0] += seconds

    k = KeyedTokenBucket(2, 1.0, lambda: now[0], sleep)
    k.take("tv-1", 2)
    async with k.reserve("tv-1", 1, refund_on_error=True) as waited:
        assert waited == pytest.approx(1.0)
    assert k.take("tv-1") == pytest.approx(1.0)  # spent

    with pytest.raises(ConnectionRefusedError):
        async with k.reserve("tv-1", 1, refund_on_error=True):
            raise ConnectionRefusedError
    # refunded
    assert k.get("tv-1").tokens == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("capacity", "rate"),
    [(0, 1.0), (1, 0), (-1, 1.0), (float("nan"), 1.0), (float("inf"), 1.0), (1.0, float("nan")), (1.0, float("inf"))],
)
def test_keyed_nonsense_settings_are_rejected_up_front(capacity, rate):
    with pytest.raises(ValueError):
        KeyedTokenBucket(capacity, rate)
