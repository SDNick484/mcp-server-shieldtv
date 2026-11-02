"""The assumption registry's machinery: statuses, queries and doctor's summary.

Shared by the sibling servers along with registry.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.

These test the machinery with a made-up registry. Each sibling's own
test_assumptions.py checks its real one (ids, citations, docs).
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from shieldtv_mcp.registry import Assumption, by_id, contradicted, doctor_lines, doctor_summary, unverified

FAKE = (
    Assumption("X-ONE", "a", "docs", "high"),
    Assumption("X-TWO", "b", "source", "medium", status="hardware-verified", note="seen"),
    Assumption("X-THREE", "c", "guess", "low", status="hardware-contradicted", note="saw otherwise"),
    Assumption("X-FOUR", "d", "docs", "high"),
)


def test_defaults_to_simulator_only_with_no_note():
    a = Assumption("X-NEW", "claim", "docs", "high")
    assert (a.status, a.note) == ("simulator-only", "")


def test_records_are_immutable():
    with pytest.raises(dataclasses.FrozenInstanceError):
        FAKE[0].status = "hardware-verified"  # type: ignore[misc]


def test_by_id_indexes_every_entry():
    assert list(by_id(FAKE)) == ["X-ONE", "X-TWO", "X-THREE", "X-FOUR"]
    assert by_id(FAKE)["X-TWO"].note == "seen"


def test_by_id_refuses_a_repeated_id():
    with pytest.raises(ValueError, match="X-ONE"):
        by_id((*FAKE, Assumption("X-ONE", "again", "docs", "low")))


def test_unverified_is_simulator_only_and_excludes_contradicted():
    # The bug this module was shared to fix: "anything not hardware-verified" counted
    # X-THREE as not yet confirmed, though hardware checked it and the code works around it.
    assert [a.id for a in unverified(FAKE)] == ["X-ONE", "X-FOUR"]


def test_contradicted_is_its_own_list():
    assert [a.id for a in contradicted(FAKE)] == ["X-THREE"]


def test_every_status_lands_in_exactly_one_place():
    verified = [a for a in FAKE if a.status == "hardware-verified"]
    assert len(unverified(FAKE)) + len(contradicted(FAKE)) + len(verified) == len(FAKE)


def test_queries_take_any_iterable():
    assert [a.id for a in unverified(iter(FAKE))] == ["X-ONE", "X-FOUR"]


# --- doctor's summary ------------------------------------------------------------------------------


def test_brief_counts_unverified_and_lists_contradicted():
    lines = doctor_lines(["X-ONE: a", "X-FOUR: d"], ["X-THREE: c"])
    assert lines == [
        "Contradicted on hardware, no longer relied on (1):",
        "   X-THREE: c",
        "Not yet confirmed on hardware: 2 (doctor --verbose lists them)",
    ]


def test_verbose_lists_unverified():
    lines = doctor_lines(["X-ONE: a", "X-FOUR: d"], [], verbose=True)
    assert lines == ["Not yet confirmed on hardware (2):", "   X-ONE: a", "   X-FOUR: d"]


@pytest.mark.parametrize("verbose", [False, True])
def test_nothing_pending_says_zero_without_the_hint(verbose):
    assert doctor_lines([], [], verbose=verbose) == ["Not yet confirmed on hardware: 0"]


# --- validation ------------------------------------------------------------------------------------


@pytest.mark.parametrize("confidence", ["high", "medium", "low"])
def test_every_confidence_is_accepted(confidence):
    assert Assumption("X-NEW", "claim", "docs", confidence).confidence == confidence


@pytest.mark.parametrize("status", ["simulator-only", "hardware-verified", "hardware-contradicted"])
def test_every_status_is_accepted(status):
    note = "seen on hardware" if status != "simulator-only" else ""
    assert Assumption("X-NEW", "claim", "docs", "high", status=status, note=note).status == status


@pytest.mark.parametrize(
    "field,args",
    [
        ("id", ("", "claim", "docs")),
        ("id", ("   ", "claim", "docs")),
        ("claim", ("X-NEW", "", "docs")),
        ("source", ("X-NEW", "claim", "")),
    ],
)
def test_empty_id_claim_or_source_is_refused(field, args):
    with pytest.raises(ValueError, match="id, claim and source must be non-empty"):
        Assumption(*args, "high")


@pytest.mark.parametrize("status", ["hardware-verified", "hardware-contradicted"])
def test_hardware_status_without_note_is_refused(status):
    with pytest.raises(ValueError, match="requires a non-empty note"):
        Assumption("X-NEW", "claim", "docs", "high", status=status, note="")
    with pytest.raises(ValueError, match="requires a non-empty note"):
        Assumption("X-NEW", "claim", "docs", "high", status=status, note="   ")


@pytest.mark.parametrize("confidence", ["High", "certain", ""])
def test_an_unknown_confidence_is_refused(confidence):
    with pytest.raises(ValueError, match=r"X-BAD.*confidence"):
        Assumption("X-BAD", "claim", "docs", confidence)


@pytest.mark.parametrize("status", ["hardware-verfied", "verified", ""])
def test_an_unknown_status_is_refused(status):
    # A typo'd status would otherwise match no query: neither unverified nor contradicted.
    with pytest.raises(ValueError, match=r"X-BAD.*status"):
        Assumption("X-BAD", "claim", "docs", "high", status=status)


# --- doctor --json ---------------------------------------------------------------------------------


def test_summary_has_every_count_and_list():
    assert doctor_summary(FAKE) == {
        "total": 4,
        "unverified_count": 2,
        "contradicted_count": 1,
        "hardware_verified_count": 1,
        "unverified": [
            {"id": "X-ONE", "claim": "a", "confidence": "high", "source": "docs"},
            {"id": "X-FOUR", "claim": "d", "confidence": "high", "source": "docs"},
        ],
        "contradicted": [{"id": "X-THREE", "claim": "c", "note": "saw otherwise"}],
        "hardware_verified": [{"id": "X-TWO", "claim": "b", "note": "seen"}],
    }


def test_summary_counts_match_its_lists():
    s = doctor_summary(FAKE)
    for key in ("unverified", "contradicted", "hardware_verified"):
        assert s[f"{key}_count"] == len(s[key])
    assert s["unverified_count"] + s["contradicted_count"] + s["hardware_verified_count"] == s["total"]


def test_summary_of_an_empty_registry():
    s = doctor_summary(())
    assert s["total"] == s["unverified_count"] == s["contradicted_count"] == s["hardware_verified_count"] == 0
    assert s["unverified"] == s["contradicted"] == s["hardware_verified"] == []


def test_summary_takes_a_one_shot_iterable():
    # It walks the input three times (once per list), so it must not consume a generator on the first.
    assert doctor_summary(iter(FAKE)) == doctor_summary(FAKE)


def test_summary_round_trips_through_json():
    assert json.loads(json.dumps(doctor_summary(FAKE))) == doctor_summary(FAKE)
