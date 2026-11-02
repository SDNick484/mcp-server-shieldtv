"""Normalized matching and closest-candidate suggestions.

Shared by the sibling servers along with lookup.py; only the package name in
the import below differs between repos. Change it in mcp-server-shared, not in
a copy, then sync.
"""

from __future__ import annotations

from shieldtv_mcp.lookup import norm, suggest, unknown_with_suggestions

COMMANDS = ("VolumeUp", "VolumeDown", "Mute", "PowerOff", "InputHdmi1", "InputHdmi2")


def test_norm_strips_case_spaces_and_punctuation():
    assert norm("Volume Up") == norm("volume_up") == norm("VOLUME-UP") == norm("VolumeUp") == "volumeup"


def test_suggest_finds_substring_and_fuzzy_matches_without_duplicates():
    assert suggest("vol", COMMANDS) == ["VolumeUp", "VolumeDown"]
    assert suggest("VolumUp", COMMANDS)[0] == "VolumeUp"
    assert suggest("hdmi", COMMANDS) == ["InputHdmi1", "InputHdmi2"]
    assert suggest("", COMMANDS) == []
    assert suggest("---", COMMANDS) == []
    assert suggest("vol", COMMANDS, limit=0) == []
    assert suggest("vol", ["VolumeUp", "volume_up", "VolumeDown"], limit=1) == ["VolumeUp"]


def test_unknown_with_suggestions_includes_matches_or_fallback():
    msg = unknown_with_suggestions(
        "activity Watch TV on Den", "VolUp", COMMANDS, fallback="Use list_commands to see what it accepts."
    )
    assert msg == "activity Watch TV on Den has no 'VolUp'. Closest matches: 'VolumeUp', 'VolumeDown'."

    miss = unknown_with_suggestions(
        "activity Watch TV on Den", "EjectDisc", COMMANDS, fallback="Use list_commands to see what it accepts."
    )
    assert miss == "activity Watch TV on Den has no 'EjectDisc'. Use list_commands to see what it accepts."

    bare = unknown_with_suggestions("device Shield", "EjectDisc", ())
    assert bare == "device Shield has no 'EjectDisc'."
