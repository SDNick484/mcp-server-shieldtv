"""Normalized name matching and closest-candidate suggestions for catalog lookups.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

Why suggest candidates on a miss: when a model asks for ``VolUp`` and the device
calls it ``VolumeUp``, a blunt "no command 'VolUp'; call list_commands" wastes a
full tool-call round-trip. Naming the closest matches in the error lets the
model self-correct on its next turn.
"""

from __future__ import annotations

import difflib
from collections.abc import Iterable


def norm(name: str) -> str:
    """Matching key: case-, space-, dash- and underscore-insensitive ('Volume Up' -> 'volumeup')."""
    return "".join(ch for ch in name.lower() if ch.isalnum())


def suggest(wanted: str, candidates: Iterable[str], *, limit: int = 5) -> list[str]:
    """Up to ``limit`` close matches from ``candidates``, preserving original casing and order of quality.

    Substring/prefix matches on the normalized key come first (so ``vol`` finds
    ``VolumeUp`` and ``VolumeDown``), followed by ``difflib.get_close_matches``
    for typos (so ``VolumUp`` finds ``VolumeUp``).
    """
    if limit <= 0:
        return []
    key = norm(wanted)
    if not key:
        return []
    by_norm: dict[str, str] = {}
    for c in candidates:
        n = norm(c)
        if n and n not in by_norm:
            by_norm[n] = c
    substr = [c for n, c in by_norm.items() if key in n or n in key]
    fuzzy_keys = difflib.get_close_matches(key, list(by_norm.keys()), n=limit, cutoff=0.5)
    fuzzy = [by_norm[k] for k in fuzzy_keys]
    return list(dict.fromkeys([*substr, *fuzzy]))[:limit]


def unknown_with_suggestions(
    what: str,
    wanted: str,
    candidates: Iterable[str],
    *,
    limit: int = 5,
    fallback: str = "",
) -> str:
    """Format an unknown-item error message with closest matches when any exist."""
    matches = suggest(wanted, candidates, limit=limit)
    if matches:
        listed = ", ".join(repr(m) for m in matches)
        return f"{what} has no {wanted!r}. Closest matches: {listed}."
    suffix = f" {fallback}" if fallback else ""
    return f"{what} has no {wanted!r}.{suffix}"
