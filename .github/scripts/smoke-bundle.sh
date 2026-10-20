#!/usr/bin/env bash
# Smoke-test a PyInstaller onedir bundle of `deals` the way a user would first
# meet it: it starts, it reports its version, it plans a search without
# touching the network, it opens its SQLite database, and the full-screen
# browser's files were bundled. Runs in a throwaway home on Linux, macOS and
# Windows (Git Bash).
#
# usage: smoke-bundle.sh BUNDLE_DIR [EXPECTED_VERSION]
set -euo pipefail

bundle="${1:?usage: smoke-bundle.sh BUNDLE_DIR [EXPECTED_VERSION]}"
expected="${2:-}"

fail() {
    echo "smoke test failed: $*" >&2
    exit 1
}

exe="$bundle/deals"
[ -f "$exe" ] || exe="$bundle/deals.exe"
[ -f "$exe" ] || fail "no deals executable in $bundle"

scratch="$(mktemp -d)"
trap 'rm -rf "$scratch"' EXIT
if command -v cygpath >/dev/null 2>&1; then
    scratch="$(cygpath -m "$scratch")" # a path the native executable understands
fi
export HOME="$scratch" USERPROFILE="$scratch" DEALS_HOME="$scratch/data"
unset DEALS_LOCALE NO_COLOR FORCE_COLOR 2>/dev/null || true

run() {
    echo "+ deals $*" >&2
    "$exe" "$@"
}

version="$(run --version)"
echo "$version"
if [ -n "$expected" ] && [ "$version" != "deals, version $expected" ]; then
    fail "expected 'deals, version $expected'"
fi

run find --genre sci-fi --dry-run
run cache info # creates state.db: the bundle ships sqlite3

# The `tui` extra (textual and its data files) must be part of every release
# binary; `deals find -i` falls back to the prompt browser without it.
if [ -z "$(find "$bundle" -type d -name textual -print -quit)" ]; then
    fail "textual was not bundled (build with: uv sync --locked --extra tui)"
fi

echo "bundle OK: $exe"
