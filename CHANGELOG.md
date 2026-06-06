# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [0.7.0] - 2026-06-05

### Added
- `deals find --subcategories` — scan each subcategory of the genre separately for deeper coverage; dry-run shows the multiplied API-call math
- `deals notify --cooldown DAYS` — suppress repeat notifications for unchanged prices across cron runs; re-fires when the price drops further (state in `notify_state.json`)
- `deals recap --json` and `deals recap --webhook` (generic/slack/discord/teams/ntfy) — machine-readable and webhook output for cron pipelines; `--json` and `--webhook` are mutually exclusive
- `--exit-code` flag on `notify` and `watch` — exit 0 when any item hits target, 1 when none (rejected with `watch --every`)
- Interactive mode verbs: `c # #` compares two results side-by-side, `h #` shows price history — no extra API calls
- `deals library --stats` — aggregate library statistics: totals, average rating/length, top genres, authors, and narrators
- `profile save` now persists `--skip-plus`, `--only-plus`, and `--exclude-keyword`

### Fixed
- Zero-length items (missing runtime) are excluded from `find`/`search`/`last` results — they polluted the default `price-per-hour` sort; `series` keeps them so pre-orders remain visible
- `--version` fallback in frozen builds reported a stale version
- install.sh now suggests installing from source when the binary download fails

## [0.6.0] - 2026-05-27

### Added
- `vs hist` column in results table — percent above/below the historical median for each ASIN with ≥3 recorded prices
- Retry-with-backoff on transient API failures (`_api_get`): up to 3 attempts (2 retries), jittered, each attempt-failure logged at WARNING (incl. final "giving up")
- Range syntax for `--last`: `1-5`, `1,3,5`, `1-3,7,9` (works on `compare`, `wishlist add/remove`; single-ref commands reject expansion)
- Dynamic shell completion for `--profile`, `--genre`, `--exclude-genre`, and `deals profile show/delete NAME`
- `deals recap --atl` — lists wishlist items currently at their all-time low
- `deals notify --webhook-template PATH` — render webhook body from a user-supplied template, one block per hit
- `DEALS_LOG_FILE` env var — append DEBUG logs to a rotating file (5 MB × 3 backups) regardless of `-v`

### Fixed
- ATL detection uses chronologically-latest price (not the last numeric one); now requires ≥2 numeric history entries; comparison unified between `find`/`search` table and `recap --atl`
- `notify` stdout JSON and `generic` webhook keep the stable 5-key schema (`asin`, `title`, `price`, `target`, `url`); per-hit `currency`/`discount_pct` are passed to templates via an internal `extras` map instead of leaking into the public payload
- `recap --atl` with truly empty results now prints "Nothing to report."
- Test isolation: `tmp_config` callers fixed to patch state-module file paths so pytest can no longer write to `~/.config/audible-deals/`

## [0.1.0] - 2026-04-01

### Added
- `deals find` — browse and filter deals by price and genre
- `deals search` — keyword search with filters
- `deals detail` / `deals compare` — single and side-by-side audiobook info
- `deals categories` — list and drill into Audible genres
- `deals wishlist` / `deals watch` — watchlist with price tracking
- `deals history` — per-ASIN price history with sparkline charts
- Saved search profiles (`deals profile save/list/delete`, `deals find --profile`)
- Interactive browsing mode (`-i` flag) — view details, open in browser, add to wishlist
- `deals recap` command for price-drop summaries
- `deals notify` command with webhook support for deal alerts
- Locale-aware display with correct currency symbols and Audible URLs for all 9 marketplaces
- Filters: `--narrator`, `--min-hours`, `--language`, `--all-languages`, `--first-in-series`, `--skip-owned`
- Pre-built binaries for macOS (ARM64), Linux (x64), and Windows (x64)
- One-liner install script for macOS and Linux
- Export to JSON and CSV
- Shell completions (bash/zsh/fish)
- CI/CD with GitHub Actions (test matrix: Python 3.11/3.12/3.13, automated releases)

[0.7.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.7.0
[0.6.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.6.0
[0.1.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.1.0
