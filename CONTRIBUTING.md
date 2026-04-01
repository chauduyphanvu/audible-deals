# Contributing to audible-deals

Thanks for your interest in contributing! This document covers the basics.

## Development setup

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e ".[dev]"
```

## Running tests

```bash
pytest tests/ -v
```

All tests must pass before submitting a PR.

## Making changes

1. Fork the repo and create a branch from `main`.
2. Make your changes. Add or update tests for any new or changed behavior.
3. Run `pytest tests/ -v` and make sure everything passes.
4. Commit using [conventional commits](https://www.conventionalcommits.org/) (e.g. `feat:`, `fix:`, `docs:`, `refactor:`).
5. Open a pull request against `main`.

## Code style

- Keep dependencies minimal. If you need a new dependency, mention it in the PR and explain why.
- Use explicit error handling — no bare `except`, raise `click.ClickException` for user-facing errors.
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
