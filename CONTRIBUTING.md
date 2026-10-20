# Contributing to audible-deals

Thanks for your interest in contributing! This document covers the basics.

## Development setup

You need Python 3.11 or newer and [uv](https://docs.astral.sh/uv/). The project is developed against `uv.lock`, and CI installs from it (`uv sync --locked`), so everyone tests the same dependency versions.

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
uv sync --extra dev                 # pytest, pytest-xdist, ruff, mypy, pre-commit, keyring, textual
uv run pre-commit install           # ruff check --fix and ruff format on every commit
```

(`pip install -e ".[dev]"` also works if you prefer pip, but it ignores the lock file.)

Add `--extra docs` to also build the documentation site. To try another Python, use `uv sync --extra dev --python 3.12`; 3.11 to 3.14 are supported and tested.

If you change dependencies in `pyproject.toml`, run `uv lock` and commit `uv.lock`; CI fails with `--locked` when the two disagree.

## Running tests and checks

```bash
uv run pytest -q -n auto          # tests, in parallel (drop -n auto to debug one)
uv run ruff check .               # lint
uv run ruff format --check .      # formatting
uv run mypy                       # type check (src/audible_deals)
uv run --extra docs mkdocs build --strict    # when you touch docs/ or mkdocs.yml
```

All of these must pass before submitting a PR; CI runs the same commands. The pre-commit hook covers ruff only: mypy and the tests are yours to run.

CI runs the tests on Linux with Python 3.11, 3.12, 3.13 and 3.14, and on macOS and Windows with 3.13. Please keep tests portable:

- Tests run in parallel and share nothing: each one gets its own data directory (`tmp_config`, or the autouse `DEALS_HOME` fixture) and its own `tmp_path`. A test must not depend on another test's leftovers, on the working directory, or on the length of its temporary path (the Rich console wraps at 80 columns, so assert on unwrapped text or widen the console as `tests/test_cache_cmd.py` does).
- Compare POSIX permission bits with `assert_mode()` from `tests/conftest.py` (it does nothing on Windows) and mark a test that is genuinely POSIX-only with `skip_on_windows("reason")`. Give a child Python process `bare_env()` when you need a minimal environment (Windows cannot start Python without `SYSTEMROOT`), and name the encoding (`encoding="utf-8"`) whenever a test reads or writes a file.

`tests/test_golden.py` pins the option names of every command and the JSON key
structure of the machine-readable output. If you change either on purpose,
regenerate the expected files and explain why in your commit message:

```bash
UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py
```

## Making changes

1. Fork the repo and create a branch from `main`.
2. Make your changes. Add or update tests for any new or changed behavior.
3. Run the checks above and make sure everything passes.
4. Commit using [conventional commits](https://www.conventionalcommits.org/) (e.g. `feat:`, `fix:`, `docs:`, `refactor:`).
5. If users would notice the change, add a line to `[Unreleased]` in `CHANGELOG.md` (put anything that changes existing behavior under **Upgrade notes**).
6. Open a pull request against `main`.

## Code style

- Keep dependencies minimal. If you need a new dependency, mention it in the PR and explain why.
- Use explicit error handling — no bare `except`, raise `click.ClickException` for user-facing errors.
- Code is type-checked with mypy (`uv run mypy`, also in CI). Prefer fixing the types; if a `# type: ignore[code]` is truly needed, add a short reason next to it.
- Never overwrite a user-edited data file (`config.json`, `profiles.json`, `monitors.json`, ...) that fails to parse: call `storage.ensure_updatable()` before a read-modify-write so the user gets an error instead of lost data.
- Follow the existing code patterns and naming conventions.

## Adding a filter

Everything about a filter's command-line flag, type, bounds, help text, default and where it is available (`find`, `search`, `last`, `for-me`, `series`, profiles, config, monitors) is declared once, as a `FilterSpec` in `src/audible_deals/filter_specs.py`. The click options of every command, the config schema and its bounds, profile and monitor validation, `deals last --clear-filter`, the `--dry-run` active-filter list and the saved-session checks are all generated from that list.

To add a simple filter, for example `--min-year`:

1. Add a `FilterSpec` to `FILTERS`: its `key`, `flag`, `kind` (`FLOAT`, `INT`, `PERCENT`, `RATING`, `TEXT`, `MULTI_TEXT`, `BOOL_PAIR`, `DATE`), `help`, `default`, bounds, and the `scopes` it belongs to. Use `variants` only where one command genuinely differs (a different default or help text). Set `refine` to `Refine.LOCAL` if `deals last` can re-apply it to cached results, and `context_field` to the `FilterContext` field that carries it.
2. Add the typed fields the registry mirrors: `Settings` (always), `ResultRecipe` and `RecipePatch` (when `Refine.LOCAL`), and `FilterContext` (when `context_field` is set).
3. Write the predicate in `filtering.py` and call it from `filter_products`.
4. Add the flag to the filter table in `docs/commands.md`, regenerate the golden option names (`UPDATE_GOLDEN=1 uv run pytest tests/test_golden.py`), and add tests for the predicate and one command-level test.

`tests/test_filter_specs.py` fails with the name of the missing piece if `Settings`, the recipe classes, `FilterContext`, the config schema, a command's options or the docs do not match the registry.

## What makes a good PR

- **Focused.** One logical change per PR.
- **Tested.** New behavior has tests. Bug fixes include a regression test when feasible.
- **Documented.** If your change affects CLI usage, update the README and the pages under `docs/` (`docs/json.md` when a `--json` shape changes).

## Reporting bugs

Open an issue using the **Bug Report** template. Include the command you ran, what you expected, and what happened.

## Requesting features

Open an issue using the **Feature Request** template. Describe the problem you're trying to solve, not just the solution you have in mind.

## Questions?

Open a discussion or issue — happy to help.

## Storage

Where files live is decided at call time by `src/audible_deals/paths.py` (`DEALS_HOME`, then the legacy `~/.config/audible-deals`, then XDG/APPDATA). Never compute a path at import time and never patch path constants in tests: the autouse fixture in `tests/conftest.py` points `DEALS_HOME` (and `HOME`) at the test's own temporary directory and closes the database connections afterwards, so a test cannot touch real data. If a test needs a different data directory, set `DEALS_HOME` with `monkeypatch.setenv`.

Records live in SQLite (`src/audible_deals/db.py`): `state.db` for what the tool must not lose and `cache.db` for what it can rebuild. To change the schema, append a tuple of statements to `_STATE_MIGRATIONS` (or `_CACHE_MIGRATIONS`); the index plus one is the `PRAGMA user_version` it brings the database to. Never edit an earlier entry. Writes go through `db.transaction(...)`. Tests seed history with `tests.conftest.seed_history` rather than writing files, and a test that needs the old JSON files (to exercise the import) writes them into the data directory before the first database use, as `tests/test_json_import.py` does. For speed, tests get a byte copy of a schema-only database built once per session; a test about creating or upgrading the databases opts out with `@pytest.mark.fresh_schema` (see `tests/test_db.py`).

## Catalog requests and the cache

Everything that fetches catalog data goes through `DealsClient` (`src/audible_deals/client.py`); concurrent page reading lives in `pagination.py` (`prefetch_in_order`, `read_serially`) with the worker-pool lifecycle in `DealsClient._pool`. A command that may show cached prices builds a `ScanCache` (`scan_cache.py`) and passes it down (`execute_catalog_scan(..., cache=)`, `get_library_asins(cache=)`, `get_series_products_many(cache=)`); commands that need live prices (`track run`, `notify`, `watch`, wishlist checks, monitors) pass nothing, which is the default. Anything that reads the shared `cache.db` connection from a worker thread must hold `db.exclusive()` (the `cache_store` functions do). If the parser starts reading a new raw API field, add its response group to `CATALOG_RESPONSE_GROUPS`; `tests/test_product_fields.py` fails until you do. `benchmarks/benchmark_scan.py` compares a cold scan with a cached one.

## Releasing

The version lives in one place: `__version__` in `src/audible_deals/__init__.py`. `pyproject.toml` reads it (hatch's dynamic version), `deals --version` prints it, and the release workflow rewrites that one line. Do not edit it in a pull request, and do not add the version anywhere else (`tests/test_version.py` checks).

To release, run the **Release** workflow from the Actions tab with the version (for example `0.12.0`). It runs the tests, writes the version into `__init__.py`, builds the wheel and sdist, checks that the wheel's `deals --version` says the version you asked for, commits the bump, tags `v<version>`, builds the Linux, macOS and Windows binaries from `uv.lock` with the `tui` extra (each one smoke-tested before it is packaged), and creates the GitHub release with the archives, their checksums and the Python distributions. It needs the `RELEASE_PAT` secret to push the bump and the tag. Before releasing, make sure `CHANGELOG.md` has a section for the version: move the `[Unreleased]` entries under a new `## [X.Y.Z] - DATE` heading.

To build and check a binary yourself, in a throwaway environment (PyInstaller is a build tool and deliberately not a dependency of the project):

```bash
uv sync --locked --extra tui
uv pip install "pyinstaller==6.22.3"
DEALS_ARTIFACT=deals-linux-x64 uv run --no-sync pyinstaller deals.spec --noconfirm
.github/scripts/smoke-bundle.sh dist/deals-linux-x64
```

## Publishing to PyPI

audible-deals is not on PyPI yet. The release workflow already has a `pypi` job that publishes the wheel and sdist with [trusted publishing](https://docs.pypi.org/trusted-publishers/) (an OpenID Connect token that PyPI exchanges for a short-lived upload token, so no API token is stored anywhere). It is switched off and its action is not pinned, so releasing does not publish anything. The repository owner turns it on once:

1. **Register the publisher on PyPI.** Sign in at pypi.org, open [Publishing](https://pypi.org/manage/account/publishing/) and add a *pending publisher* (the project does not exist yet): project name `audible-deals`, owner `chauduyphanvu`, repository `audible-deals`, workflow `release.yml`, environment `pypi`. (Using [TestPyPI](https://test.pypi.org/manage/account/publishing/) first, with the same values, is a good dry run.)
2. **Create the GitHub environment.** In the repository settings, add an environment named `pypi`. Optionally require yourself as a reviewer so every upload waits for a click.
3. **Pin the action.** In `.github/workflows/release.yml`, replace `PIN_TO_RELEASE_COMMIT_SHA` in the `pypa/gh-action-pypi-publish` line with the full 40-character commit SHA of a release tag you have checked yourself, and put that tag in the comment, in the style of the other actions (`@<sha>  # v1.x.y`). For example: `gh release list -R pypa/gh-action-pypi-publish --limit 3`, then `gh api repos/pypa/gh-action-pypi-publish/commits/<tag> --jq .sha`. `tests/test_workflows.py` requires every other action to be pinned the same way.
4. **Switch the job on.** Set the repository variable `PUBLISH_TO_PYPI` to `true` (Settings, Secrets and variables, Actions, Variables).
5. **Release.** The job runs after the GitHub release is created, because a PyPI upload cannot be undone or repeated for the same version. If only that job fails, fix the cause and use "Re-run failed jobs"; the distributions are kept as a workflow artifact. After the first upload the pending publisher becomes a normal one.

Once the package is on PyPI, switch the install instructions from the `git+https://github.com/chauduyphanvu/audible-deals` form to the plain package name in `README.md`, `docs/index.md`, `docs/installation.md` and `docs/commands.md`, update the optional-extra hints in `secret_store.py`, and relax `tests/test_docs.py`, which exists to keep the docs from recommending the unpublished package.
