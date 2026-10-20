# Cleanup plan

This plan records the findings from the August 23, 2026 repository audit. It separates work that was safe to complete immediately from changes that need platform-specific testing, an API-compatibility decision, or explicit approval for remote/CI mutations.

## Status

| Item | Status |
|------|--------|
| Catalog pagination | Resolved |
| Dead and duplicate code | Resolved |
| P0 Transactional, path-safe installer | **Resolved** (`install.sh` stages, verifies and swaps with rollback; `tests/test_install_script.py`) |
| P1 Windows tracking logs | **Resolved in code** (`generate_windows_task_command` redirects both streams to the log; quoting tests in `tests/test_track.py`). Still to do: run a real scheduled task on a Windows machine |
| P1 One release-version authority | **Resolved** (`audible_deals.__version__` is the only literal; `install.sh` has no static fallback; see below) |
| P2 Obsolete public-looking API surfaces | **Resolved** (the unused helpers and switches were removed, the partial-series message has one formatter) |
| P3 Remote repository state | **Open: needs the repository owner** (shared GitHub state; nothing was changed from here) |

## Completed in this cleanup pass

### Catalog pagination

The Audible catalog API uses zero-based page indexes, while `DealsClient.search_pages()` and `search_segments()` expose one-based logical page numbers. The client correctly special-cased title-only searches but incorrectly treated keyword, category, browse, and non-title segmented searches as one-indexed, skipping their first API page.

The client now:

- defaults direct `search_catalog()` calls to API page `0`;
- preserves explicitly supplied API page values for direct callers;
- translates logical page `N` to API page `N - 1` in paginated and segmented searches; and
- has regression coverage for keyword, title, category, concurrent-page, and segmented searches.

This resolves the defect reported in GitHub issue #32.

### Proven dead and duplicate code

- Removed 32 redundant test methods, including the copied filtering/sorting/deduplication block in `test_catalog_filtering.py` and exact duplicate reset-key and discount-boundary cases.
- Retained similar helpers whose consolidation would only trade a few explicit test lines for a new abstraction.

## Prioritized remaining work

### P0 — Make the installer transactional and path-safe (resolved)

**Finding:** `install.sh` accepts an arbitrary `LIB_DIR` and executes `rm -rf "$LIB_DIR"` before extracting and validating the replacement. A bad value can target a broad directory, and any extraction failure destroys the working installation.

**Implementation plan:**

1. Resolve and validate `LIB_DIR` before any mutation. Reject empty values, filesystem roots, the user's home directory, and other targets that do not identify a dedicated installation directory.
2. Create a staging directory next to the final destination so the final rename stays on one filesystem.
3. Extract the archive into staging and verify the expected `deals` binary is present before touching the current installation.
4. Replace the installation with a same-parent rename. Keep a temporary backup until the new binary and symlink are installed, and restore it if the swap fails.
5. Extend the exit trap to remove only paths created by the current run.
6. Add shell-level tests using temporary directories for unsafe targets, corrupt archives, missing binaries, successful upgrades, and rollback after a failed swap.

**Acceptance criteria:** No user-supplied broad directory is recursively deleted; failed downloads, checksums, extraction, or validation leave the previous installation usable; a successful upgrade leaves no staging or backup directory.

**Resolution:** `install.sh` validates `LIB_DIR` and the command link before touching anything (an install marker identifies a dedicated directory), stages and verifies the new tree next to the destination, swaps by rename with a backup that is restored on any failure, and cleans up only what the run created. `tests/test_install_script.py` covers unsafe targets, corrupt archives, missing binaries, upgrades and rollback.

### P1 — Preserve scheduled tracking logs on Windows (resolved in code; unverified on Windows)

**Finding:** `_schtasks_install()` accepts `log_path` but never uses it. Linux cron and macOS launchd write the promised tracking log, while Windows scheduled runs discard that output.

**Implementation plan:**

1. Add a small Windows command builder that invokes `cmd.exe /D /S /C` and redirects stdout and stderr to `log_path`.
2. Quote the executable, arguments, and log path according to `cmd.exe` rules, including paths containing spaces and metacharacters.
3. Keep the existing interval validation and `schtasks` schedule construction unchanged.
4. Extend `tests/test_track.py` to assert the complete `/TR` payload for ordinary paths, paths with spaces, and special characters.
5. Exercise a real scheduled task on Windows before release if a Windows runner or machine is available.

**Acceptance criteria:** A scheduled Windows run appends both output streams to the configured log, existing schedules remain representable, and quoting tests demonstrate that command arguments cannot be split or interpreted as extra shell syntax.

**Resolution:** the task command is `cmd.exe /D /S /V:OFF /C "…  >> "LOG" 2>&1"`, built and parsed back by `generate_windows_task_command` / `parse_windows_task_command`, with unsafe characters in the data directory refused; `tests/test_track.py` and `tests/test_scheduler_*.py` assert the payloads. Step 5 (a real scheduled task on Windows) has not been done: the Windows CI job runs the unit tests only.

### P1 — Establish one release-version authority (resolved)

**Finding:** `install.sh` falls back to version `0.9.1` when GitHub's latest-release API is unavailable, while the package and CLI are at `0.11.0`. The release workflow updates only `pyproject.toml`, so the literal silently drifts after every release.

**Preferred implementation:** Remove the static fallback. When `VERSION` is unset and the latest-release lookup fails, stop with an actionable message telling the user to retry or set `VERSION` explicitly. Add shell tests for explicit versions, successful API resolution, and API failure.

**Alternative:** If offline fallback behavior is a product requirement, update the release workflow to rewrite and commit the installer version from the same release input. This changes CI/CD and requires explicit approval.

**Acceptance criteria:** The installer cannot silently install an older release because a duplicated version literal was not updated.

**Resolution:** the preferred option. `install.sh` has no static fallback: with `VERSION` unset and the latest-release lookup failing it stops with "Could not resolve the latest release from GitHub; retry or set VERSION explicitly" (tests for explicit versions, successful resolution and seven failure shapes). Beyond the installer, `audible_deals.__version__` is now the only version literal: `pyproject.toml` reads it, `deals --version` prints it (the hardcoded fallback is gone), and the release workflow rewrites that one file. `tests/test_version.py` guards it.

### P2 — Remove obsolete public-looking API surfaces deliberately (resolved)

These items are unused by production code but remain importable, so removal should follow an explicit compatibility decision rather than being bundled into mechanical cleanup.

1. `delete_price_histories()` has no repository callers but is a non-underscored, importable helper shipped in v0.11.0. Remove it only if direct module imports are outside the supported API; otherwise retain and document it.
2. `ResultPublicationRequest.write_cache` and `record_price_history` are never set to `False`, and production callers provide `session_spec`, leaving the legacy last-results fallback unreachable in normal CLI flows. Decide whether this dataclass is supported externally; if not, remove the switches and simplify `commit_presentation()` with focused publication tests.
3. `DealsClient.get_series_products()` is used only by tests; production uses the bounded batch method `get_series_products_many()`. If the client class is not a supported library API, migrate the remaining tests and remove the single-series adapter. Otherwise, document and retain it.
4. `cli/series.py` and `cli/foryou.py` independently build nearly identical partial-series outcome messages. Extract one narrowly scoped formatter only after its wording and ownership are agreed, then cover both command surfaces with the existing CLI tests.

**Acceptance criteria:** Any removed symbol has no repository callers, documented compatibility is respected, and behavior-level tests cover the surviving path rather than mirroring its implementation.

**Resolution:** `delete_price_histories()`, the `ResultPublicationRequest` switches and `DealsClient.get_series_products()` are gone (removed by the cleanup change, #33; the supported surface is the command line and the documented `--json` shapes), and `series` and `for-me` share `_report_partial_series_outcomes`.

### P3 — Clean up remote repository state (open: needs the repository owner)

These actions mutate shared GitHub state or delete branches and therefore require explicit approval at execution time.

- Close issue #32 after the pagination fix is released or otherwise available to users.
- Re-test and close issue #15 if the current macOS arm64 release asset confirms the one-line installer problem is resolved.
- Close PR #19 as redundant: `main` already uses the same `actions/upload-artifact` v7.0.1 revision, while the old branch would regress unrelated workflow changes if merged as-is.
- Delete `origin/feature/series-command` after confirmation. Its effective patch is already represented on `main` by the squash-merged series work.
- Decide whether `origin/feat/web-ui` is parked or abandoned. It contains a substantial web UI, is more than 100 commits behind `main`, and has no current integration path; archive its intent before deletion if any part is still wanted.
- Triage the remaining Dependabot pull requests individually; age alone is not enough to classify them as abandoned. Consider switching `.github/dependabot.yml` from the `pip` ecosystem to `uv`, which also updates `uv.lock`.

None of this was done from the modernization work: it changes shared GitHub state.

## Intentionally retained

- Legacy data readers and migrations remain active compatibility paths.
- The hidden `for-you` compatibility alias is intentionally retained for its documented deprecation window.
- `deals.spec` remains part of binary packaging.
- Ignored virtual environments, generated site output, caches, and bytecode are local build artifacts rather than abandoned source.

## Recommended sequence

1. ~~Harden and test the installer before making further one-line installation changes.~~ Done.
2. ~~Fix Windows logging~~ Done; validate it on Windows.
3. ~~Resolve the release-version strategy.~~ Done (one version literal, no installer fallback).
4. ~~Make and document the API-compatibility decision, then perform the P2 removals.~~ Done.
5. Perform the GitHub and remote-branch cleanup only after each destructive or shared-state action is explicitly approved. **Open.**

For every implementation batch, run the full pytest suite, Ruff lint and formatting checks, `mkdocs build --strict`, and the relevant platform or shell integration tests before merging.
