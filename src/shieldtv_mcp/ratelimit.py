"""Token-bucket rate limiting, for guarding devices against runaway tool loops.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Why rate limits at all: a model in a loop ("volume up... still quiet... volume
up...") can send a lot of commands in a few seconds, and a start/stop loop can
wear out a TV's power relay or keep a MiSTer reloading cores. A per-call cap in
the tool's schema bounds one call; a token bucket bounds the *rate* across
calls.

Only the mechanism is shared. Each sibling keeps its own numbers (how many
presses, how many activity changes) in its own module, because those are about
its device, and builds one bucket per device and per kind of action
(``KeyedTokenBucket`` when the devices are only known at run time).

Refuse or wait? ``take`` refuses at once, which is right for a tool call: the
model hears why and decides. ``acquire`` waits (up to a timeout), which suits
code that paces itself, such as a macro stepping through presses.
"""

from __future__ import annotations

import math
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import anyio

# Float slack. Refilling for exactly the computed wait can land a hair short of
# n tokens (0.9999999999999999). acquire counts anything within _EPSILON of n
# as n, or it would ask for a sleep too small to move a clock and spin; and it
# lets a wait overrun its timeout by _TOLERANCE, so a wait of exactly the
# timeout isn't refused over rounding.
_EPSILON = 1e-9
_TOLERANCE = 1e-6


@dataclass
class TokenBucket:
    """Up to ``capacity`` tokens, refilled at ``rate`` per second; each action takes some.

    ``take`` is all-or-nothing on purpose: a call that needs more tokens than
    are there takes none and is refused whole, with how long to wait. Half of a
    "volume up x5" is worse than none, because the model can't tell which half
    happened.

    ``clock`` and ``sleep`` are injectable so tests can move time instead of
    sleeping (a fake ``sleep`` advances the fake clock).
    """

    capacity: float
    rate: float  # tokens per second
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], Awaitable[None]] = field(default=anyio.sleep, repr=False)
    tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        if not (math.isfinite(self.capacity) and math.isfinite(self.rate)) or self.capacity <= 0 or self.rate <= 0:
            raise ValueError("capacity and rate must be positive")
        self.tokens = self.capacity
        self._last = self.clock()

    def _refill(self) -> None:
        now = self.clock()
        self.tokens = min(self.capacity, self.tokens + (now - self._last) * self.rate)
        self._last = now

    def take(self, n: float = 1.0) -> float:
        """Take n tokens and return 0.0, or take nothing and return the seconds until n are available.

        More than ``capacity`` can never be available, so that returns infinity.
        A negative n raises ``ValueError``: it would add tokens, past capacity.
        """
        if math.isnan(n) or n < 0:
            raise ValueError("cannot take a negative number of tokens")
        self._refill()
        if n > self.capacity:
            return float("inf")
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        return (n - self.tokens) / self.rate

    def refund(self, n: float = 1.0) -> None:
        """Give back n tokens (never past ``capacity``), for an action that failed before reaching the device.

        A command that never left the host (bad argument, connection refused)
        shouldn't count against the device's budget. One that may have reached
        it (a timeout waiting for the reply) should, so don't refund that.
        """
        if not math.isfinite(n) or n < 0:
            raise ValueError("can't refund a negative number of tokens")
        self._refill()
        self.tokens = min(self.capacity, self.tokens + n)

    async def acquire(self, n: float = 1.0, timeout: float | None = None) -> float:
        """Take n tokens, waiting for them if need be; return the seconds waited (exactly 0.0 if none).

        Raises ``TimeoutError`` as soon as it's clear the tokens won't arrive
        within what's left of ``timeout``, without sleeping first: refill is
        predictable, so there's no point waiting to find out. A refused call
        takes nothing. Raises ``ValueError`` if n is more than ``capacity``,
        which no wait can satisfy (``take`` returns infinity for that and
        ``refusal`` words it), or negative.

        Waiters aren't queued: if several tasks wait on one bucket, whichever
        wakes first when tokens arrive gets them, and the others go back to
        sleep. Cancelling a waiting ``acquire`` takes nothing.
        """
        if math.isnan(n) or n < 0:
            raise ValueError("cannot acquire a negative number of tokens")
        if n > self.capacity:
            raise ValueError("cannot acquire more tokens than capacity")
        self._refill()
        start = self._last  # the clock as of each refill, so no sleep means exactly 0.0
        while True:
            if self.tokens + _EPSILON >= n:
                self.tokens = max(0.0, self.tokens - n)
                return self._last - start
            wait = (n - self.tokens) / self.rate
            waited = self._last - start
            if timeout is not None and wait > timeout - waited + _TOLERANCE:
                raise TimeoutError(f"{n} tokens not available within {timeout}s (need {waited + wait:.3g}s)")
            await self.sleep(wait)
            self._refill()

    @asynccontextmanager
    async def reserve(
        self, n: float = 1.0, timeout: float | None = None, refund_on_error: bool = False
    ) -> AsyncIterator[float]:
        """``acquire`` n tokens for the body of an ``async with``; yield the seconds waited.

        With ``refund_on_error``, an exception escaping the body gives the
        tokens back, for bodies that fail before touching the device::

            async with bucket.reserve(2, timeout=5, refund_on_error=True):
                cmd = build_command(args)  # a ValueError here costs nothing
                await device.send(cmd)

        Only ``Exception`` refunds. Cancellation doesn't: it usually comes from
        a timeout around the device call, when the command may already be out.
        """
        waited = await self.acquire(n, timeout)
        try:
            yield waited
        except Exception:
            if refund_on_error:
                self.refund(n)
            raise


class KeyedTokenBucket:
    """One ``TokenBucket`` per key, all with the same settings, made on first use.

    For limits per device when the devices are discovered at run time, e.g.
    ``presses = KeyedTokenBucket(10, 2.0)`` then ``presses.take(device_id)``.
    Buckets are never dropped; keys are expected to be a handful of devices,
    not something unbounded such as client addresses.
    """

    def __init__(
        self,
        capacity: float,
        rate: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = anyio.sleep,
    ) -> None:
        if not (math.isfinite(capacity) and math.isfinite(rate)) or capacity <= 0 or rate <= 0:
            raise ValueError("capacity and rate must be positive")
        self.capacity = capacity
        self.rate = rate
        self.clock = clock
        self.sleep = sleep
        self._buckets: dict[str, TokenBucket] = {}

    def get(self, key: str) -> TokenBucket:
        """The bucket for ``key``, made (full) if this is the key's first use."""
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = self._buckets[key] = TokenBucket(self.capacity, self.rate, self.clock, self.sleep)
        return bucket

    def take(self, key: str, n: float = 1.0) -> float:
        """``TokenBucket.take`` on ``key``'s bucket."""
        return self.get(key).take(n)

    def refund(self, key: str, n: float = 1.0) -> None:
        """``TokenBucket.refund`` on ``key``'s bucket."""
        self.get(key).refund(n)

    async def acquire(self, key: str, n: float = 1.0, timeout: float | None = None) -> float:
        """``TokenBucket.acquire`` on ``key``'s bucket."""
        return await self.get(key).acquire(n, timeout)

    @asynccontextmanager
    async def reserve(
        self, key: str, n: float = 1.0, timeout: float | None = None, refund_on_error: bool = False
    ) -> AsyncIterator[float]:
        """``TokenBucket.reserve`` on ``key``'s bucket."""
        async with self.get(key).reserve(n, timeout=timeout, refund_on_error=refund_on_error) as waited:
            yield waited


def refusal(what: str, wait: float, device: str | None = None) -> str:
    """The error message for a refused action.

    For example: "Rate limit on Den: refusing to launch Contra (try again in
    12s). This guards against runaway loops; ...".

    ``wait`` is what ``TokenBucket.take`` returned. The message says why the
    limit exists, so the model doesn't treat it as a fault to work around.
    """
    on = f" on {device}" if device else ""
    if wait == float("inf"):
        return (
            f"Rate limit{on}: refusing to {what} (it's more than the limit allows at once). "
            "This guards against runaway loops; reduce the count or split the request instead of retrying as-is."
        )
    return (
        f"Rate limit{on}: refusing to {what} (try again in {max(wait, 1):.0f}s). "
        "This guards against runaway loops; if this is deliberate, wait and retry."
    )
