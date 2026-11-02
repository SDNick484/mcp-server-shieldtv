"""The assumption registry's machinery: what an assumption is, and which ones still need hardware.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Every sibling keeps a registry of the device behaviors it relies on, in its own
``assumptions.py``: an ``ASSUMPTIONS`` tuple whose ids the code, tests and docs
cite (``# ASSUMPTION B-ES-LAUNCH``), and which ``doctor`` reports on. Only the
list is about the device. The rest (the record, the statuses, the queries,
doctor's summary) was the same code in eight places, and when two of them fixed
``unverified()`` the other six kept the bug. So that part lives here, and a
sibling's ``assumptions.py`` is its list plus::

    from .registry import Assumption, by_id, contradicted, unverified
    ASSUMPTIONS: tuple[Assumption, ...] = (...)
    BY_ID = by_id(ASSUMPTIONS)

The statuses, and what the queries mean by them:

- ``simulator-only``: read in docs or source, implemented in the simulator,
  not yet seen on a real device. The code relies on it on faith; that's what
  ``unverified`` returns and what doctor counts as "not yet confirmed".
- ``hardware-verified``: seen to hold on a real device (``note`` says which and when).
- ``hardware-contradicted``: seen *not* to hold, and the code was changed so it
  no longer relies on the claim (``note`` says what was seen instead). It has
  been checked, so it is not "not yet confirmed"; ``contradicted`` returns these,
  and doctor lists them on their own because each one changed the code.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

# How sure the claim is, judged from its source (not from hardware; that's Status):
#   high   - read directly in the code that implements it
#   medium - read in source, but depends on settings, timing or versions we can't see
#   low    - our own guess; the simulator implements it but nothing confirms it
Confidence = Literal["high", "medium", "low"]
Status = Literal["simulator-only", "hardware-verified", "hardware-contradicted"]


@dataclass(frozen=True)
class Assumption:
    id: str
    claim: str
    source: str
    confidence: Confidence
    status: Status = "simulator-only"
    note: str = ""

    def __post_init__(self) -> None:
        # The Literal types only bind under mypy. Checked here too, so a typo'd status in a sibling's
        # ASSUMPTIONS ("hardware-verfied") fails at import rather than quietly matching no query.
        if not self.id.strip() or not self.claim.strip() or not self.source.strip():
            raise ValueError(f"assumption {self.id!r}: id, claim and source must be non-empty")
        if self.confidence not in ("high", "medium", "low"):
            raise ValueError(
                f"assumption {self.id!r}: confidence {self.confidence!r} is not one of ('high', 'medium', 'low')"
            )
        if self.status not in ("simulator-only", "hardware-verified", "hardware-contradicted"):
            raise ValueError(
                f"assumption {self.id!r}: status {self.status!r} is not one of"
                " ('simulator-only', 'hardware-verified', 'hardware-contradicted')"
            )
        if self.status in ("hardware-verified", "hardware-contradicted") and not self.note.strip():
            raise ValueError(f"assumption {self.id!r}: status {self.status!r} requires a non-empty note")


def by_id(assumptions: Iterable[Assumption]) -> dict[str, Assumption]:
    """Index by id. A repeated id is an error here, at import, rather than a silently lost entry."""
    index: dict[str, Assumption] = {}
    for a in assumptions:
        if a.id in index:
            raise ValueError(f"assumption id {a.id!r} appears twice")
        index[a.id] = a
    return index


def unverified(assumptions: Iterable[Assumption]) -> list[Assumption]:
    """Not checked on hardware yet (``simulator-only``): the code relies on these on the strength of docs or source.

    Not "everything but hardware-verified": a contradicted assumption *was*
    checked, and the code already works around it.
    """
    return [a for a in assumptions if a.status == "simulator-only"]


def contradicted(assumptions: Iterable[Assumption]) -> list[Assumption]:
    """Checked on hardware and found wrong (``hardware-contradicted``); the code no longer relies on them."""
    return [a for a in assumptions if a.status == "hardware-contradicted"]


def doctor_lines(unverified: Sequence[str], contradicted: Sequence[str], verbose: bool = False) -> list[str]:
    """doctor's text summary of the registry, given one line per assumption (e.g. ``"B-ES-LAUNCH: ..."``).

    Contradicted ones are listed in full: they're rare, and each one changed the
    code. Unverified ones are a count unless ``verbose``, because that list only
    shrinks as hardware checks are recorded and says little from one run to the
    next. doctor's ``--json`` output should carry both lists in full regardless.

    Takes strings rather than ``Assumption`` objects so each doctor keeps its own
    line format (some add the confidence) and its own report shape.
    """
    lines: list[str] = []
    if contradicted:
        lines.append(f"Contradicted on hardware, no longer relied on ({len(contradicted)}):")
        lines += [f"   {c}" for c in contradicted]
    if verbose and unverified:
        lines.append(f"Not yet confirmed on hardware ({len(unverified)}):")
        lines += [f"   {u}" for u in unverified]
    else:
        more = " (doctor --verbose lists them)" if unverified else ""
        lines.append(f"Not yet confirmed on hardware: {len(unverified)}{more}")
    return lines


def doctor_summary(assumptions: Iterable[Assumption]) -> dict[str, Any]:
    """doctor ``--json``'s view of the registry: every count, and every list in full.

    The machine-readable counterpart of ``doctor_lines``, so a script (or CI) can
    read the registry without parsing text, e.g. fail when ``contradicted_count``
    rises. The keys are a contract with whatever reads the JSON; add, don't rename.
    Lists keep the registry's order. Unverified entries carry confidence and source
    (what decides which to check on hardware next); checked ones carry the note
    that says what hardware showed.
    """
    items = tuple(assumptions)
    pending = unverified(items)
    wrong = contradicted(items)
    verified = [a for a in items if a.status == "hardware-verified"]
    return {
        "total": len(items),
        "unverified_count": len(pending),
        "contradicted_count": len(wrong),
        "hardware_verified_count": len(verified),
        "unverified": [{"id": a.id, "claim": a.claim, "confidence": a.confidence, "source": a.source} for a in pending],
        "contradicted": [{"id": a.id, "claim": a.claim, "note": a.note} for a in wrong],
        "hardware_verified": [{"id": a.id, "claim": a.claim, "note": a.note} for a in verified],
    }
