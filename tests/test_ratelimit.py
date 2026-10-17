"""TokenBucket and refusal: the shared rate-limit mechanism.

Shared by the sibling servers along with ratelimit.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.

Time is a list the test moves forward, so nothing sleeps.
"""

from __future__ import annotations

import pytest

from shieldtv_mcp.ratelimit import TokenBucket, refusal


def bucket(capacity: float, rate: float) -> tuple[TokenBucket, list[float]]:
    now = [0.0]
    return TokenBucket(capacity, rate, clock=lambda: now[0]), now


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


@pytest.mark.parametrize(("capacity", "rate"), [(0, 1.0), (1, 0), (-1, 1.0)])
def test_nonsense_settings_are_rejected(capacity, rate):
    with pytest.raises(ValueError):
        TokenBucket(capacity, rate)


def test_refusal_message():
    msg = refusal("launch Contra", 11.6, device="Den")
    assert msg.startswith("Rate limit on Den: refusing to launch Contra (try again in 12s).")
    assert "runaway loops" in msg
    assert refusal("press VolumeUp x30", float("inf")).startswith(
        "Rate limit: refusing to press VolumeUp x30 (it's more than the limit allows at once)."
    )
    assert "try again in 1s" in refusal("x", 0.2)  # never "0s"
