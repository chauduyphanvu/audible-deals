# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Catalog responses are cached for two hours in `cache.db`: re-running or refining a `find`, `search`, `for-me` or `series` scan makes no API calls, says how old the prices are, and `--refresh` (new on `find`, `search` and `series`, and extended on `for-me`) fetches live data. `deals config set cache-ttl MINUTES` changes the lifetime (`0` turns it off). Cached prices are never recorded in the price history; `track run`, `notify`, `watch`, `wishlist` and monitors always use live data. Progress and `--dry-run` count cached pages as free (`cached_calls` is a new key in the dry-run JSON; `api_calls` now counts only requests that would reach Audible).
- On an interactive terminal `find` and `search` show a live "best so far" table under the progress bar while pages arrive; output that is redirected, `--json`, `-q`/`-o` and `-i` are unchanged. Ctrl-C during a `find` or `search` scan now shows what was gathered ("Scan interrupted after X/Y pages"), saves the session so `@N` works, records history for the live-fetched items and exits with status 130 (before, it aborted and discarded the scan).
- `--skip-owned` reuses the list of owned titles for 24 hours (`deals library` and `--refresh` update it), and category names are cached for 7 days.
- `DEALS_HOME` selects the data directory, read on every run (tests and scheduled jobs use it too). Without it, an existing `~/.config/audible-deals` is kept, otherwise `$XDG_CONFIG_HOME/audible-deals`, otherwise `~/.config/audible-deals` (`%APPDATA%\audible-deals` on Windows when that does not exist). `deals doctor` shows the directory and why it was chosen. `deals track install` now writes the directory into the scheduled job (launchd plist, systemd unit, crontab line, schtasks command) and prints it.
- `deals cache info [--json]` shows where data lives and what each database holds; `deals cache clear` empties the disposable cache without touching your records.
- `deals doctor` checks `state.db` (`PRAGMA quick_check`, schema version), reports the outcome of the one-time JSON import, and checks `cache.db`.
- `benchmarks/benchmark_history.py` compares history, recap and lookup costs on thousands of tracked ASINs for any source tree.
- `--locale` and `-v/--verbose` are now accepted after the command as well as before it, on every command and subcommand (`deals find --locale uk`, `deals wishlist add ASIN -v`). If both positions are used, the one after the command wins.
- Development: every filter's flag, type, bounds, help, default and availability is declared once in `src/audible_deals/filter_specs.py`; the command options, config schema, profile and monitor validation, `last --clear-filter` names and `--dry-run` filter list are generated from it, and `tests/test_filter_specs.py` fails when a hand-written mirror (`Settings`, the result recipe, `FilterContext`, the docs) drifts. CONTRIBUTING.md has an "Adding a filter" section.
- Development: mypy is part of the `dev` extra and the CI lint job, ruff checks a wider rule set (`E`, `F`, `W`, `I`, `B`, `UP`), CI installs from `uv.lock`, and `tests/test_golden.py` pins every command's option names and the key structure of the JSON outputs.

### Changed
- Internal: one paginator (`pagination.py`) and one worker-pool lifecycle now serve the catalog, library, wishlist and series readers, and genre matching lives in `genres.py`. Requests, ordering and call counts are unchanged.
- Records moved from many JSON files into two SQLite databases in the data directory: `state.db` (price history, the last result session, seen and dismissed ASINs, refresh eligibility, monitor baselines, alert cooldowns, tracking state and run history) and `cache.db` (category lists and the `for-me` taste profile, safe to delete). `config.json`, `profiles.json`, `monitors.json`, `wishlist.json` and `auth.json` stay JSON. On first use the old files are imported in one transaction and renamed to `*.migrated` (`history/` becomes `history.migrated/`); nothing is deleted, a failed import changes nothing and is retried, and concurrent runs import exactly once. Unusable entries are skipped and reported by `deals doctor`, and the cache files are rebuilt rather than imported (one extra category request after upgrading). A `state.db` that cannot be read is moved to `state.db.corrupt-<timestamp>` with a one-line warning and recreated, and one written by a newer release is refused untouched. See "Local data" in the command reference.
- Price-history reads are queries instead of "load every file": `deals recap` on 5,000 tracked titles with 30 days of history takes under a third of the time and under half the memory, recording prices is about twenty times faster, and `deals history ASIN` opens a single database instead of a file per title. Ties in recap ordering and the `--atl-all` ranking are now broken by ASIN rather than by directory order.
- Price history is purged per marketplace without the old "has no entries, skipping purge" warning for every ASIN tracked only in another marketplace.
- `deals track install` and `uninstall` update only the install record, atomically, instead of rewriting the whole tracking state without the run lock.
- Every state file is read and written as UTF-8 regardless of the platform's locale encoding (files written in the old locale encoding still load), including the track log, `import-auth` sources and the callback file.
- `deals profile save` and `deals monitor add` now take every filter `find` takes. Profiles gain `--max-effective-price`, `--hist-below`, `--min-price-drop`, `--require-history`, `--released-after`/`--released-before` and `--exclude-seen`; monitors gain those (except `--exclude-seen`, which cannot apply to a snapshot) plus `--max-price-per-hour`, `--exclude-genre`, `--series`, `--publisher` and `--all-languages`. A monitor built from a profile keeps all of the profile's filters, and `--max-effective-price` uses the credit price configured when the monitor runs.
- `--released-after` and `--released-before` are checked when the command line is parsed, on `find`, `search`, `last`, `profile save` and `monitor add` (an invalid date is a usage error naming the option; compact `20240101` is still accepted and normalized to `2024-01-01`).
- `find` and `search` gain `--no-exclude-seen` and `--no-require-history`, so a value saved in a profile can be turned off from the command line.
- `deals config set max-effective-price N` sets a global default for `--max-effective-price` (`deals for-me` honors it too). Release dates and the repeatable exclude filters stay profile-only.
- Every option of `deals last` and `deals profile save` now has help text, and `last`, `profile save` and `monitor add` complete genre names.
- The package now requires `click>=8.2`.
- A zero-byte or whitespace-only data file (`config.json`, `profiles.json`, ...) is now treated as empty without a warning; there is nothing in it to lose.
- `-v`/`-vv` now also apply to warnings emitted while loading saved configuration at startup.

### Fixed
- A `config.json`, `profiles.json`, `monitors.json` or `dismissed_asins.json` that could not be parsed (for example after a typo while hand-editing) was silently replaced with an empty file the next time a command saved it, deleting every saved profile, monitor, or default. Commands that would write to such a file now stop with a one-line error naming the file and the line and column of the problem, and leave it untouched. Commands that only read it print a one-line warning (the traceback appears only with `-vv`).
- Interactive mode printed the literal text `[dim]...[/dim]` around its "Invalid input" hint.
- `install.sh` told users on unsupported platforms to run `pip install audible-deals`, which fails because the package is not on PyPI. It, and the README and installation guide, now show `uv tool install git+https://github.com/chauduyphanvu/audible-deals` (or the `pipx` equivalent).

## [0.11.1] - 2026-08-31

### Fixed
- `deals login` now explains that callback URL input is hidden for security and that users should press Enter after pasting, instead of appearing unresponsive.

## [0.11.0] - 2026-08-22

### Changed
- `deals for-you` is now `deals for-me`. The old spelling remains as a deprecated alias for one minor release and prints a warning.
- Bare `deals` now shows a local, state-aware dashboard; top-level help is organized by workflow; and bare wishlist, profile, config, monitor, and track groups show their current state.
- `deals login` now defaults to browser sign-in, opens the browser when possible, and accepts the callback URL directly. `--credentials` retains terminal credential entry and `--via-file` remains available for remote terminals.

## [0.10.0] - 2026-08-19

### Added
- `deals monitor` creates frozen, locale-specific profile (`find`) or direct-query (`search`) saved searches that `deals track run` checks alongside the wishlist. First runs establish a silent baseline; later runs report new matches and price drops through the configured webhook. Scheduled monitors use a deterministic 60-page-call shared budget.

### Fixed
- Background-run locking is now crash-safe across processes and platforms, including safe handoff from legacy PID locks; failure-state writes remain protected by the same lock.
- Price history is isolated by marketplace and serialized across concurrent writers, preventing one locale from overwriting another. Legacy unscoped history is archived instead of assigned to an arbitrary marketplace, and stale-history purges revalidate entries before deletion.
- Scheduled tracking rejects intervals that cron or Windows Task Scheduler cannot represent exactly; Windows schedules now preserve supported minute and daily cadences.
- `deals notify --exit-code` now returns 1 for an empty wishlist, consistently indicating that no item hit its target.

## [0.9.1] - 2026-08-17

### Fixed
- Windows: CLI crashed with `UnicodeEncodeError` when the console used a legacy code page (e.g. cp1252) and could not encode the Unicode glyphs Rich renders. The standard streams are now reconfigured to UTF-8 at startup.
- `deals last` no longer records cached prices as current observations, preventing stale results from corrupting price history.
- `find` and `search` dry runs no longer construct an authenticated client or fetch category data; multi-query estimates now include every query.
- Export formats are validated before API calls, and export write failures occur before price-history or result-cache updates. JSON and CSV files now always use UTF-8.
- Saved-profile numeric ranges and resolved `--skip-plus`/`--only-plus` conflicts are validated consistently; profile-supplied genres now work with `search`.
- Audible request and filesystem failures now render concise CLI errors instead of tracebacks while preserving normal broken-pipe behavior.
- Version fallbacks, config help and aliases, and the `--min-price-drop` help text are current and consistent.

## [0.9.0] - 2026-06-22

### Added
- `deals for-you` filter parity with `find`: `--sort`, `--narrator`, `--exclude-author`, `--exclude-narrator`, `--skip-plus`/`--only-plus`
- Price signals in `for-you` ranking — candidates at an all-time low or below their historical median rank higher, with "all-time low" / "below median" match reasons
- Interactive mode verbs: `s <key>` re-sorts the results in place (the last-results cache follows, so `--last` refs keep matching the screen); `n #[,#-#]` marks items not-interested so `--exclude-seen` hides them from future scans
- `deals track status --history` — table of the last 10 background runs (ring buffer in `track_state.json` replaces the single `last_run` entry); `deals doctor` reports FAIL when 3+ consecutive runs failed
- `--webhook-header 'Name: Value'` (repeatable) on `notify`, `recap`, and `track install` — custom headers for self-hosted receivers (Home Assistant, n8n, …); persisted to config by `track install`; `Content-Type` cannot be overridden
- Webhook POSTs retry up to 3 attempts with jittered backoff before failing
- `deals wishlist list --json` and `-o FILE` (`.json`/`.csv`) — machine-readable wishlist export
- install.sh verifies downloads against SHA-256 sidecars (now uploaded by the release workflow) and falls back to a pinned version when the GitHub API is unreachable

### Changed
- `get_products_batch` fetches 50-ASIN batches concurrently (up to 4 in flight) — faster `track run`, `watch`, and `notify` with large wishlists

### Fixed
A correctness/robustness audit of the whole CLI resolved 32 confirmed bugs, each with a regression test:
- Webhook POSTs no longer follow HTTP redirects — closes an SSRF bypass and stops `--webhook-header` secrets (e.g. `Authorization`) from leaking to a redirect target; the SSRF guard also now blocks CGNAT shared space (100.64.0.0/10)
- `deals completions <shell>` emits a real completion script on the fallback path (when `deals` isn't yet on `PATH`) instead of writing CLI help text into your shell config, and fails loudly if generation fails
- `deals notify --exit-code` returns 0 when an item hits target but is suppressed by `--cooldown` (matches the documented contract); `recap` validates `--json`/`--webhook` and the webhook URL before acquiring the run lock
- `deals track` background runs only treat genuine auth failures (401/403/auth messages) as needing re-login, instead of pinging on every error; failure state is written under the run lock so a failing run can no longer clobber a concurrent run's freshly saved state
- `deals track uninstall` removes systemd units even when the user D-Bus session is unavailable; `track install` rounds sub-hour cron intervals up to a divisor of 60 so a run never fires faster than requested
- Non-numeric or missing prices in the Audible response no longer crash catalog/library fetches, price-history display, or wishlist target matching; a corrupt categories cache is handled gracefully
- Category genres keep their correct names (id/name stay aligned) in taste profiles and `library --stats`; wishlist entries missing an ASIN or title are skipped
- `deals config set` enforces the same numeric ranges as the equivalent flags; `profile save --sort` and the global `--locale` are now validated; `wishlist add/sync/author` reject negative `--max-price`; interactive target prices reject non-numeric and non-positive input
- `$0.00` wishlist targets render as `$0.00` instead of as "no target"

## [0.8.0] - 2026-06-10

### Added
- Credit-aware buy advice: `deals config set credit-price N` adds a **Buy** verdict (cash / credit / plus) to results tables, `watch`, `detail`, and `compare`; new `--max-effective-price` filter on `find`/`search`/`last` (effective price = the cheaper of cash and one credit); `notify` payloads gain `verdict` and `effective_price` keys. Output is unchanged when the key is unset
- `deals track` — autonomous background price tracking: `install` registers a launchd agent (macOS), systemd user timer or crontab entry (Linux), or Scheduled Task (Windows) running `track run` on an interval (default 6h, min 10m); each run refreshes wishlist + author watches plus recently tracked ASINs (30-day window, 200 cap), records history, and sends cooldown-gated webhook alerts via new `webhook`/`webhook-format` config keys; `track status`/`track log` and new `deals doctor` rows surface schedule and last-run health; auth failures trigger a one-time "re-auth needed" webhook ping
- `deals for-you` — personalized deals from a local taste profile built from the owned library (top authors, narrators, genres, in-progress series; cached 24h in `taste_cache.json`); scans series gaps, author searches, and genre bestsellers, ranks by fit (series-next > author > narrator > genre, ties by value score); a **Match** column explains each result and tags wishlisted items; supports `--dry-run`, `--refresh`, and the standard filter/export/interactive pipeline

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

[0.11.1]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.11.1
[0.11.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.11.0
[0.10.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.10.0
[0.9.1]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.9.1
[0.9.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.9.0
[0.8.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.8.0
[0.7.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.7.0
[0.6.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.6.0
[0.1.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.1.0
