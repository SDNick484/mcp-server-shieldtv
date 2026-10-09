"""Keep the assumption registry and the code in step (and, below, the docs).

assumptions.py is the source of truth. This fails when:
- the code cites an id that isn't in the registry (a typo, or a removed assumption)
- an assumption is cited nowhere in the code or tests (nothing depends on it, so why keep it?)
"""

from __future__ import annotations

import re
from pathlib import Path

from shieldtv_mcp.assumptions import ASSUMPTIONS, BY_ID

ROOT = Path(__file__).parent.parent
ID = re.compile(r"\bS-[A-Z0-9]+(?:-[A-Z0-9]+)*\b")


def cited_in(paths: list[Path]) -> set[str]:
    return {m for path in paths for m in ID.findall(path.read_text())}


def test_ids_are_unique_and_well_formed():
    ids = [a.id for a in ASSUMPTIONS]
    assert len(ids) == len(set(ids))
    assert all(ID.fullmatch(i) for i in ids)


def test_verified_ones_say_what_was_seen():
    for a in ASSUMPTIONS:
        if a.status != "simulator-only":
            assert a.note or "observed" in a.source or "checked" in a.source, a.id


def test_unverified_ones_say_why():
    for a in ASSUMPTIONS:
        if a.status == "simulator-only":
            assert a.note, f"{a.id}: say what is (and isn't) known"


def test_code_cites_only_real_ids_and_every_id_is_used():
    code = sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "tests").glob("test_*.py"))
    code = [p for p in code if p.name not in ("assumptions.py", "test_assumptions.py")]
    cited = cited_in(code)
    assert cited <= set(BY_ID), cited - set(BY_ID)
    assert set(BY_ID) <= cited, f"assumptions nothing cites: {set(BY_ID) - cited}"
