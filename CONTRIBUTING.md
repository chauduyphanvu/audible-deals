# Contributing to audible-deals

Thanks for your interest in contributing! This document covers the basics.

## Development setup

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
uv sync --extra dev
```

(`pip install -e ".[dev]"` also works if you prefer pip.)

## Running tests and checks

```bash
uv run pytest -q                  # tests
uv run ruff check .               # lint
uv run ruff format --check .      # formatting
uv run mypy                       # type check (src/audible_deals)
```

All four must pass before submitting a PR; CI runs the same commands.

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
5. Open a pull request against `main`.

## Code style

- Keep dependencies minimal. If you need a new dependency, mention it in the PR and explain why.
- Use explicit error handling — no bare `except`, raise `click.ClickException` for user-facing errors.
- Code is type-checked with mypy. Prefer fixing the types; if a `# type: ignore[code]` is truly needed, add a short reason next to it.
- Never overwrite a user-edited data file (`config.json`, `profiles.json`, `monitors.json`, ...) that fails to parse: call `storage.ensure_updatable()` before a read-modify-write so the user gets an error instead of lost data.
- Follow the existing code patterns and naming conventions.

## What makes a good PR

- **Focused.** One logical change per PR.
- **Tested.** New behavior has tests. Bug fixes include a regression test when feasible.
- **Documented.** If your change affects CLI usage, update the README.

## Reporting bugs

Open an issue using the **Bug Report** template. Include the command you ran, what you expected, and what happened.

## Requesting features

Open an issue using the **Feature Request** template. Describe the problem you're trying to solve, not just the solution you have in mind.

## Questions?

Open a discussion or issue — happy to help.
