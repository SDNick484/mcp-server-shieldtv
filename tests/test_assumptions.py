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


# --- the docs ----------------------------------------------------------------------------
DOCS = ("README.md", "HARDWARE_VALIDATION.md")


def test_docs_list_every_assumption():
    for doc in DOCS:
        cited = cited_in([ROOT / doc])
        assert cited <= set(BY_ID), f"{doc} cites unknown ids: {cited - set(BY_ID)}"
        assert set(BY_ID) <= cited, f"{doc} doesn't mention {set(BY_ID) - cited}"


def test_readme_table_matches_the_registry():
    rows = re.findall(r"^\| `(S-[A-Z0-9-]+)` \| (\w+) \| ([\w-]+) \|$", (ROOT / "README.md").read_text(), re.M)
    assert {r[0]: (r[1], r[2]) for r in rows} == {a.id: (a.confidence, a.status) for a in ASSUMPTIONS}


def test_validation_doc_has_the_steps_doctor_points_at():
    from shieldtv_mcp.doctor import STEP

    text = (ROOT / "HARDWARE_VALIDATION.md").read_text()
    headings = dict(re.findall(r"^## (\d+)\. (.+)$", text, re.M))
    expected = {"config": "Pairing", "tcp": "doctor", "identity": "doctor", "session": "doctor"}
    expected |= {"mdns": "mDNS", "adb": "ADB"}
    for layer, step in STEP.items():
        assert headings[str(step)].startswith(expected[layer]), (layer, step, headings.get(str(step)))


def test_validation_doc_starts_with_pairing():
    text = (ROOT / "HARDWARE_VALIDATION.md").read_text()
    assert re.findall(r"^## (\d+)\. (.+)$", text, re.M)[0] == ("1", "Pairing")


def test_validation_doc_marks_new_features_unverified():
    text = (ROOT / "HARDWARE_VALIDATION.md").read_text()
    table = text.split("## What's new and unconfirmed", 1)[1].split("##", 1)[0]
    rows = [r for r in table.splitlines() if r.startswith("| ") and not r.startswith("| Feature") and "---" not in r]
    assert rows and all("verified against simulator only" in r or "verified on hardware" in r for r in rows)
