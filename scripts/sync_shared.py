"""Sync the files the sibling MCP servers share from mcp-server-shared, or check them.

Shared by the sibling servers, byte for byte. Change it in mcp-server-shared,
not in a copy, then sync (`python scripts/sync_shared.py --update`).

mcp-server-shared is the single source of truth for code the siblings each
carry a copy of. Its shared.toml lists those files, grouped into modules. Each
sibling's shared.lock pins the mcp-server-shared commit its copies come from,
names its package, and can exclude modules it doesn't use (a server that only
speaks stdio has no use for remote.py):

    python scripts/sync_shared.py                   write the files as of the locked commit
    python scripts/sync_shared.py --update          move the lock to mcp-server-shared main, then write
    python scripts/sync_shared.py --check           exit 1 if any copy differs from the locked commit
    python scripts/sync_shared.py --check --latest  exit 1 if any copy differs from main instead
    ... --source DIR [--package NAME]               read a local mcp-server-shared checkout, not GitHub

`--source` is for trying a change to mcp-server-shared in a sibling before
pushing it, and for mcp-server-shared itself, whose CI renders the shared
tests for its own package with `--source . --package mcp_shared`.

Exit status: 0 in sync, 1 a copy differs (--check), 2 anything else (bad
arguments, an unreadable lock, the network).

Why it works the way it does:
- Files are fetched by commit SHA, never by branch name. A SHA names one
  immutable snapshot, so every file in a sync comes from the same commit, and
  raw.githubusercontent.com's caching of branch URLs (minutes) can't hand back
  a stale file. `--update` and `--latest` resolve `main` to a SHA first, with
  one GitHub API call.
- Everything is fetched before anything is written, and the lock is written
  last, so a network failure halfway leaves the repo as it was.
- Standard library only (tomllib needs Python 3.11), so it runs before
  `pip install` and needs nothing in CI but a Python.
- GITHUB_TOKEN (or GH_TOKEN), if set, is sent to api.github.com only, to raise
  its rate limit (60 an hour anonymously, per IP: shared CI runners can use
  that up). Raw files are fetched anonymously because raw.githubusercontent.com
  answers 404 to a token that can't see the repo, even a public one, and a
  workflow's own token is scoped to its own repo, not to mcp-server-shared.
- Siblings opt out of a module (`exclude = ["remote"]`) rather than opt in, so
  a newly shared module reaches every sibling by default, and a sibling that
  doesn't want it says so on purpose. Exclusion is by module, not by path, so
  a module's code and its test can't be split by accident. An excluded module's
  files are left alone: the script neither writes nor deletes them.
"""

from __future__ import annotations

import argparse
import contextlib
import difflib
import os
import re
import sys
import tomllib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

PLACEHOLDER = "__PKG__"
MANIFEST = "shared.toml"
LOCK = "shared.lock"
SELF = "scripts/sync_shared.py"  # where this script lives, in mcp-server-shared and in every sibling
# The newest shared.toml format this script understands. 2 added `module` on [[file]] and, with
# it, `exclude` in shared.lock. A format-1 script ignores both, and its --update would rewrite the
# lock without `exclude`, quietly bringing excluded files back; the bump makes it fetch this
# script first instead (see wanted_files).
FORMAT = 2
DEFAULT_REPO = "SDNick484/mcp-server-shared"
TOKEN_HOSTS = ("api.github.com",)  # see the docstring for why raw.githubusercontent.com isn't here
TIMEOUT = 30.0
DIFF_LINES = 40  # per file, in --check output

_SHA = re.compile(r"[0-9a-f]{40}")
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_PACKAGE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

LOCK_HEADER = """\
# Which mcp-server-shared commit this repo's shared files come from, and this
# repo's package name (it replaces __PKG__). Written by
# `python scripts/sync_shared.py --update`; see that script. An optional
# `exclude = ["remote"]` names shared modules this repo doesn't take.
"""


class SyncError(Exception):
    """Something the user has to fix; the message says what."""


# --- the lock --------------------------------------------------------------------
@dataclass(frozen=True)
class Lock:
    repo: str
    commit: str
    package: str
    exclude: tuple[str, ...] = ()  # shared modules this repo doesn't take

    def validate(self) -> Lock:
        if not _REPO.fullmatch(self.repo):
            raise SyncError(f"{LOCK}: repo must look like owner/name (got {self.repo!r})")
        if not _SHA.fullmatch(self.commit):
            raise SyncError(f"{LOCK}: commit must be a full 40-character SHA (got {self.commit!r})")
        if not _PACKAGE.fullmatch(self.package):
            raise SyncError(f"{LOCK}: package must be a Python identifier (got {self.package!r})")
        for name in self.exclude:
            if not _PACKAGE.fullmatch(name):
                raise SyncError(f'{LOCK}: exclude must list module names like "remote" (got {name!r})')
        return self

    def dump(self) -> str:
        text = f'{LOCK_HEADER}repo = "{self.repo}"\ncommit = "{self.commit}"\npackage = "{self.package}"\n'
        if self.exclude:
            text += "exclude = [" + ", ".join(f'"{name}"' for name in self.exclude) + "]\n"
        return text


def read_lock(root: Path) -> Lock:
    path = root / LOCK
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SyncError(f"no {LOCK} in {root}; see mcp-server-shared's README for how to add one") from None
    except tomllib.TOMLDecodeError as exc:
        raise SyncError(f"{LOCK} isn't valid TOML: {exc}") from None
    exclude = data.get("exclude", [])
    if not isinstance(exclude, list) or not all(isinstance(name, str) for name in exclude):
        raise SyncError(f'{LOCK}: exclude must be a list of module names, like ["remote"] (got {exclude!r})')
    try:
        lock = Lock(
            str(data.get("repo", DEFAULT_REPO)),
            str(data["commit"]),
            str(data["package"]),
            tuple(dict.fromkeys(exclude)),  # drop repeats, keep the order written
        )
    except KeyError as exc:
        raise SyncError(f"{LOCK} is missing {exc.args[0]!r}") from None
    return lock.validate()


# --- where the files come from ----------------------------------------------------------
class Source(Protocol):
    @property
    def label(self) -> str: ...  # for messages: "mcp-server-shared at 1a2b3c4 (locked)"

    def read(self, path: str) -> bytes: ...


def _get(url: str, accept: str | None = None) -> bytes:
    """GET a URL; HTTPS_PROXY and SSL_CERT_FILE are honored the usual urllib way."""
    request = urllib.request.Request(url, headers={"User-Agent": "sync_shared.py"})
    if accept:
        request.add_header("Accept", accept)
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if token and urllib.parse.urlsplit(url).hostname in TOKEN_HOSTS:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body: bytes = response.read()
            return body
    except urllib.error.HTTPError as exc:
        hint = " (rate limited? set GITHUB_TOKEN)" if exc.code in (403, 429) else ""
        raise SyncError(f"GET {url}: HTTP {exc.code} {exc.reason}{hint}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise SyncError(f"GET {url}: {reason}") from None


def resolve_main(repo: str) -> str:
    """The SHA mcp-server-shared's main points at right now."""
    sha = _get(f"https://api.github.com/repos/{repo}/commits/main", "application/vnd.github.sha").decode().strip()
    if not _SHA.fullmatch(sha):
        raise SyncError(f"GitHub returned {sha[:60]!r} for {repo} main, not a commit SHA")
    return sha


@dataclass
class GitHub:
    repo: str
    commit: str
    why: str  # "locked" or "main"

    @property
    def label(self) -> str:
        return f"{self.repo.split('/')[-1]} at {self.commit[:7]} ({self.why})"

    def read(self, path: str) -> bytes:
        return _get(f"https://raw.githubusercontent.com/{self.repo}/{self.commit}/{path}")


@dataclass
class LocalDir:
    root: Path

    @property
    def label(self) -> str:
        return f"the local checkout {self.root}"

    def read(self, path: str) -> bytes:
        try:
            return (self.root / path).read_bytes()
        except OSError as exc:
            raise SyncError(f"{self.root / path}: {exc.strerror}") from None


# --- the manifest ------------------------------------------------------------------------
@dataclass(frozen=True)
class Entry:
    source: str
    target: str
    render: bool
    module: str | None  # the group a sibling can exclude; None means every sibling takes it (the script)


def _relative(path: object, field: str) -> str:
    """A manifest path must stay inside the repo it names a file in."""
    if not isinstance(path, str) or not path:
        raise SyncError(f"{MANIFEST}: {field} must be a non-empty string (got {path!r})")
    parts = Path(path).parts
    if Path(path).is_absolute() or ".." in parts or "\\" in path:
        raise SyncError(f"{MANIFEST}: {field} must be a relative path inside the repo (got {path!r})")
    return path


def parse_manifest(raw: bytes) -> tuple[int, list[Entry]]:
    try:
        data = tomllib.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise SyncError(f"{MANIFEST} isn't valid TOML: {exc}") from None
    fmt = data.get("format")
    if not isinstance(fmt, int):
        raise SyncError(f"{MANIFEST}: format must be an integer")
    entries: list[Entry] = []
    for item in data.get("file", []):
        if not isinstance(item, dict):
            raise SyncError(f"{MANIFEST}: each [[file]] must be a table")
        render = item.get("render", False)
        if not isinstance(render, bool):
            raise SyncError(f"{MANIFEST}: render must be true or false (got {render!r})")
        module = item.get("module")
        if module is not None and not (isinstance(module, str) and _PACKAGE.fullmatch(module)):
            raise SyncError(f'{MANIFEST}: module must be a name like "remote" (got {module!r})')
        source, target = _relative(item.get("source"), "source"), _relative(item.get("target"), "target")
        entries.append(Entry(source, target, render, module))
    targets = [e.target for e in entries]
    if len(set(targets)) != len(targets):
        raise SyncError(f"{MANIFEST}: two entries write the same target")
    return fmt, entries


def wanted_files(
    source: Source, package: str, *, writing: bool, root: Path, exclude: tuple[str, ...] = ()
) -> dict[str, bytes]:
    """Every shared file's target path and the exact bytes it should hold, minus excluded modules.
    Nothing is written here, except in one case: a manifest newer than this script (see below)."""
    fmt, entries = parse_manifest(source.read(MANIFEST))
    if fmt > FORMAT:
        # A newer script is needed to read this manifest, and the script is itself shared: fetch
        # just it (its path never changes), so the next run is the new version.
        if not writing:
            raise SyncError(
                f"{MANIFEST} in {source.label} is format {fmt}, newer than this script ({FORMAT}). "
                "Run the same command without --check: it brings in the newer script, then run this again."
            )
        (root / SELF).parent.mkdir(parents=True, exist_ok=True)
        (root / SELF).write_bytes(source.read(SELF))
        raise SyncError(f"Updated {SELF} for {MANIFEST} format {fmt}. Run the same command again.")
    # A typo ("remot") would otherwise exclude nothing, silently. So would a module that was renamed
    # or dropped in mcp-server-shared: then this lock's exclude is out of date and should say so.
    modules = sorted({e.module for e in entries if e.module})
    unknown = [name for name in exclude if name not in modules]
    if unknown:
        raise SyncError(
            f"{LOCK} excludes {', '.join(map(repr, unknown))}, but {MANIFEST} in {source.label} has no such module "
            f"(it has: {', '.join(modules) or 'none'})"
        )
    files: dict[str, bytes] = {}
    for entry in entries:
        if entry.module in exclude:
            continue
        content = source.read(entry.source)
        if entry.render:
            try:
                content = content.decode("utf-8").replace(PLACEHOLDER, package).encode("utf-8")
            except UnicodeDecodeError:
                raise SyncError(f"{entry.source} is marked render = true but isn't UTF-8 text") from None
        files[entry.target.replace(PLACEHOLDER, package)] = content
    return files


# --- comparing and writing ------------------------------------------------------------------
def differences(root: Path, files: dict[str, bytes]) -> list[tuple[str, str]]:
    """(target, "differs" | "missing") for every copy that doesn't match, in manifest order."""
    found = []
    for target, content in files.items():
        path = root / target
        if not path.exists():
            found.append((target, "missing"))
        elif path.read_bytes() != content:
            found.append((target, "differs"))
    return found


def write_files(root: Path, files: dict[str, bytes]) -> list[str]:
    """Write the copies that differ; return their paths. Unchanged files are left alone (and their mtimes)."""
    changed = []
    for target, status in differences(root, files):
        path = root / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(files[target])  # bytes, so no newline translation: copies stay byte-identical
        changed.append(f"{target} ({'added' if status == 'missing' else 'updated'})")
    return changed


def _diff(local: Path, wanted: bytes, label: str) -> list[str]:
    try:
        ours = local.read_text(encoding="utf-8").splitlines()
        theirs = wanted.decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return ["    (binary or unreadable; no diff)"]
    lines = list(difflib.unified_diff(theirs, ours, f"{label}", f"{local.name} (this repo)", lineterm="", n=1))
    shown = [f"    {line}" for line in lines[:DIFF_LINES]]
    if len(lines) > DIFF_LINES:
        shown.append(f"    ... {len(lines) - DIFF_LINES} more diff lines")
    return shown


def _quiet_stdout() -> None:
    """Point stdout at /dev/null, so the interpreter's flush at exit can't raise BrokenPipeError again."""
    with contextlib.suppress(AttributeError, OSError, ValueError):  # stdout isn't a real file (e.g. under pytest)
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())


def report(root: Path, files: dict[str, bytes], label: str, fix: str) -> int:
    found = differences(root, files)
    if not found:
        print(f"shared files: all {len(files)} match {label}")
        return 0
    try:
        _print_report(root, files, found, label, fix)
    except BrokenPipeError:
        # Piped into something that stopped reading (`--check | head`). The listing was cut short,
        # but the answer stands: something differs. Without this, a traceback hid the exit status.
        _quiet_stdout()
    return 1


def _print_report(root: Path, files: dict[str, bytes], found: list[tuple[str, str]], label: str, fix: str) -> None:
    annotate = os.environ.get("GITHUB_ACTIONS") == "true"
    print(f"shared files: {len(found)} of {len(files)} don't match {label}:")
    for target, status in found:
        print(f"  {target}: {status}  -> fix: {fix}")
        if annotate:  # shows on the file in the PR's "Files changed" view
            print(f"::error file={target}::{target} {status} from {label}. Fix: {fix}")
    print(
        f"\nFix them all with `{fix}`. If you meant to change a shared file, make the change in "
        "mcp-server-shared instead, push it to main, then run `python scripts/sync_shared.py --update` here."
    )
    for target, status in found:
        if status == "differs":
            print(f"\n  {target}:")
            print("\n".join(_diff(root / target, files[target], label)))


# --- the command line ------------------------------------------------------------------------
def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="sync_shared.py",
        description="Sync (or check) the files shared by the sibling MCP servers, from mcp-server-shared.",
    )
    parser.add_argument("--update", action="store_true", help="move shared.lock to mcp-server-shared main first")
    parser.add_argument("--check", action="store_true", help="don't write; exit 1 if any copy differs")
    parser.add_argument("--latest", action="store_true", help="with --check: compare against main, not the lock")
    parser.add_argument("--source", type=Path, help="read files from this local mcp-server-shared checkout")
    parser.add_argument("--package", help="package name for __PKG__ (default: from shared.lock)")
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parent.parent,
        help="the repo to sync (default: the one this script is in)",
    )
    args = parser.parse_args(argv)
    if args.latest and not args.check:
        parser.error("--latest only goes with --check (to move to main and write, use --update)")
    if args.update and args.check:
        parser.error("--update writes; use --check --latest to compare against main without writing")
    if args.source and (args.update or args.latest):
        parser.error("--source reads a local checkout, which has no lock or main to move to")
    if args.package is not None and not _PACKAGE.fullmatch(args.package):
        parser.error(f"--package must be a Python identifier (got {args.package!r})")
    return args


def _excluding(exclude: tuple[str, ...]) -> str:
    return f" (excluding {', '.join(exclude)}, per {LOCK})" if exclude else ""


def run(args: argparse.Namespace) -> int:
    root: Path = args.root

    if args.source:
        # mcp-server-shared itself renders its tests with --package and has no lock; a sibling
        # trying out a local change does have one, and its exclude list still applies.
        lock_or_none = read_lock(root) if (root / LOCK).exists() or not args.package else None
        package = args.package or (lock_or_none.package if lock_or_none else "")
        exclude = lock_or_none.exclude if lock_or_none else ()
        local = LocalDir(args.source.resolve())
        files = wanted_files(local, package, writing=not args.check, root=root, exclude=exclude)
        label = local.label + _excluding(exclude)
        if args.check:
            return report(root, files, label, f"python scripts/sync_shared.py --source {args.source}")
        changed = write_files(root, files)
        print(f"synced from {label}: " + (", ".join(changed) if changed else "nothing to change"))
        return 0

    lock = read_lock(root)
    package = args.package or lock.package
    exclude = lock.exclude

    if args.check:
        if args.latest:
            main = resolve_main(lock.repo)
            latest = GitHub(lock.repo, main, "main")
            files = wanted_files(latest, package, writing=False, root=root, exclude=exclude)
            status = report(root, files, latest.label + _excluding(exclude), "python scripts/sync_shared.py --update")
            if status == 0 and main != lock.commit:
                # Nothing shared changed between the lock and main (a README edit, say), so this
                # repo isn't behind in any way that matters; --update would only move the lock.
                print(f"(shared.lock is at {lock.commit[:7]}, main at {main[:7]}, with no shared file changed)")
            return status
        source = GitHub(lock.repo, lock.commit, "locked")
        files = wanted_files(source, package, writing=False, root=root, exclude=exclude)
        return report(root, files, source.label + _excluding(exclude), "python scripts/sync_shared.py")

    if args.update:
        main = resolve_main(lock.repo)
        source = GitHub(lock.repo, main, "main")
        files = wanted_files(source, package, writing=True, root=root, exclude=exclude)
        changed = write_files(root, files)
        new_lock = Lock(lock.repo, main, lock.package, lock.exclude)  # exclude carries over unchanged
        if new_lock != lock:
            (root / LOCK).write_text(new_lock.dump(), encoding="utf-8")  # last: only once every file is in
        moved = f"{lock.commit[:7]} -> {main[:7]}" if main != lock.commit else f"already at {main[:7]}"
        print(f"shared.lock: {moved}{_excluding(exclude)}; " + (", ".join(changed) if changed else "no files changed"))
        return 0

    source = GitHub(lock.repo, lock.commit, "locked")
    files = wanted_files(source, package, writing=True, root=root, exclude=exclude)
    changed = write_files(root, files)
    label = source.label + _excluding(exclude)
    print(f"synced from {label}: " + (", ".join(changed) if changed else "nothing to change"))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        return run(args)
    except SyncError as exc:
        print(f"sync_shared.py: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
