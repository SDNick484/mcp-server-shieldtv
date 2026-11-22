# Pin the commit an install gets, and stamp it beside the venv, so the server can report what it
# runs (remote.py adds it to /healthz and <MCP_HTTP_PATH>/healthz).
#
# Shared by the sibling servers, byte for byte: change it in mcp-server-shared, not in a copy, then
# sync (`python scripts/sync_shared.py --update`). Sourced by deploy/alpine/install.sh; defines
# functions only. POSIX sh (busybox ash on Alpine), safe under `set -eu`.
#
#   . "$HERE/commit.sh"
#   REPO=SDNick484/mcp-server-<name>
#   pinned=$(commit_source "$SRC" "$REPO")     # stops the install if main can't be resolved
#   SRC=$(printf '%s\n' "$pinned" | sed -n 1p)
#   COMMIT=$(printf '%s\n' "$pinned" | sed -n 2p)
#   ... pip install "$SRC" ...
#   write_commit "$PREFIX" "$COMMIT" "$REPO"     # only after pip succeeded
#
# The commit is worked out before pip, so the stamp and the install are the same commit:
#   git+https://github.com/<REPO>[.git]   main's SHA from `git ls-remote`, installed as ...@<sha>
#   a checkout directory                  its HEAD (none if it isn't a git checkout)
#   any other pip spec                    unchanged, and no commit

# 0 if $1 is a full commit SHA: 40 lowercase hex digits. (Explicit lists, not ranges: a range in a
# bracket expression follows the locale's collation, and may let uppercase through.)
_commit_is_sha() {
	[ "${#1}" -eq 40 ] || return 1
	case $1 in *[!0123456789abcdef]*) return 1 ;; esac
}

# 0 if $1 is owner/name, as remote.py checks it.
_commit_is_repo() {
	case $1 in */*/* | */) return 1 ;; esac
	_owner=${1%%/*}
	_name=${1#*/}
	[ "$_owner" != "$1" ] && [ -n "$_owner" ] && [ -n "$_name" ] || return 1
	case $_owner in *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-]*) return 1 ;; esac
	case $_name in *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-]*) return 1 ;; esac
}

# commit_source SRC REPO: print the spec to install (line 1) and its commit, or an empty line
# (line 2). Returns 1, with a message on stderr, if SRC is REPO's GitHub URL and main can't be
# resolved: installing it unpinned would leave nothing to stamp.
commit_source() {
	_src=$1
	_repo=$2
	case $_src in
	"git+https://github.com/$_repo" | "git+https://github.com/$_repo.git")
		_out=$(git ls-remote "https://github.com/$_repo" refs/heads/main) || {
			echo "commit.sh: git ls-remote https://github.com/$_repo failed; can't pin main" >&2
			return 1
		}
		_sha=$(printf '%s\n' "$_out" | sed -n '1s/[[:space:]].*//p')
		if ! _commit_is_sha "$_sha"; then
			echo "commit.sh: no commit for main in https://github.com/$_repo (got '$_out')" >&2
			return 1
		fi
		printf '%s\n%s\n' "git+https://github.com/$_repo@$_sha" "$_sha"
		;;
	*)
		_sha=
		if [ -d "$_src" ]; then
			# git as root refuses a checkout another user owns ("dubious ownership"), hence
			# safe.directory, which git compares with the repo's absolute path: so resolve it first.
			_abs=$(cd "$_src" && pwd -P)
			_top=$(git -c safe.directory="$_abs" -C "$_abs" rev-parse --show-toplevel 2>/dev/null) || _top=
			# Only the top of a checkout: a plain directory inside some other repo isn't its HEAD.
			if [ "$_top" = "$_abs" ]; then
				_sha=$(git -c safe.directory="$_abs" -C "$_abs" rev-parse HEAD 2>/dev/null) || _sha=
				_commit_is_sha "$_sha" || _sha=
			fi
		fi
		printf '%s\n%s\n' "$_src" "$_sha"
		;;
	esac
}

# write_commit PREFIX SHA REPO: write $PREFIX/COMMIT (SHA, then REPO if it checks out). Without a
# full SHA, remove it instead: a COMMIT from an earlier install must not outlive the code it named.
write_commit() {
	if _commit_is_sha "$2"; then
		if _commit_is_repo "$3"; then
			printf '%s\n%s\n' "$2" "$3" >"$1/COMMIT.tmp"
		else
			printf '%s\n' "$2" >"$1/COMMIT.tmp"
		fi
		# Written beside and moved into place, so a reader never sees a half-written file
		chmod 0644 "$1/COMMIT.tmp"
		mv -f "$1/COMMIT.tmp" "$1/COMMIT"
	else
		rm -f "$1/COMMIT"
	fi
}
