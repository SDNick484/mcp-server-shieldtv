"""The command line: subcommand defaults and argument spelling (no Shield needed)."""

from __future__ import annotations

import pytest

from shieldtv_mcp import cli


@pytest.mark.parametrize(
    ("argv", "cmd", "http"),
    [
        ([], "serve", False),
        (["--http"], "serve", True),  # used to fail: "unrecognized arguments: --http"
        (["serve", "--http", "--port", "9000"], "serve", True),
        (["--http", "--bind", "0.0.0.0", "--insecure-no-auth"], "serve", True),
    ],
)
def test_serve_is_the_default(argv, cmd, http):
    args = cli.build_parser().parse_args(cli.normalize(argv))
    assert args.cmd == cmd and args.http is http


def test_other_commands_parse_as_before():
    args = cli.build_parser().parse_args(cli.normalize(["pair", "--host", "192.168.1.50"]))
    assert (args.cmd, args.host) == ("pair", "192.168.1.50")
    args = cli.build_parser().parse_args(cli.normalize(["discover", "--timeout", "2", "--debug"]))
    assert (args.cmd, args.timeout, args.debug) == ("discover", 2.0, True)


def test_version_and_help_are_not_rewritten():
    assert cli.normalize(["--version"]) == ["--version"]
    assert cli.normalize(["-h"]) == ["-h"]


def test_serve_dry_run_flag():
    args = cli.build_parser().parse_args(cli.normalize(["--dry-run", "--http"]))
    assert args.dry_run and args.http
