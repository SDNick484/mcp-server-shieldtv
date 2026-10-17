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
its device, and builds one bucket per device and per kind of action.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass
class TokenBucket:
    """Up to ``capacity`` tokens, refilled at ``rate`` per second; each action takes some.

    ``take`` is all-or-nothing on purpose: a call that needs more tokens than
    are there takes none and is refused whole, with how long to wait. Half of a
    "volume up x5" is worse than none, because the model can't tell which half
    happened.

    ``clock`` is injectable so tests can move time instead of sleeping.
    """

    capacity: float
    rate: float  # tokens per second
    clock: Callable[[], float] = time.monotonic
    tokens: float = field(init=False)
    _last: float = field(init=False)

    def __post_init__(self) -> None:
        if self.capacity <= 0 or self.rate <= 0:
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
        """
        self._refill()
        if n > self.capacity:
            return float("inf")
        if self.tokens >= n:
            self.tokens -= n
            return 0.0
        return (n - self.tokens) / self.rate


def refusal(what: str, wait: float, device: str | None = None) -> str:
    """The error message for a refused action.

    For example: "Rate limit on Den: refusing to launch Contra (try again in
    12s). This guards against runaway loops; ...".

    ``wait`` is what ``TokenBucket.take`` returned. The message says why the
    limit exists, so the model doesn't treat it as a fault to work around.
    """
    when = "it's more than the limit allows at once" if wait == float("inf") else f"try again in {max(wait, 1):.0f}s"
    on = f" on {device}" if device else ""
    return (
        f"Rate limit{on}: refusing to {what} ({when}). This guards against runaway loops; "
        "if this is deliberate, wait and retry."
    )
