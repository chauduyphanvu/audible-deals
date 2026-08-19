# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

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

[0.10.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.10.0
[0.9.1]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.9.1
[0.9.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.9.0
[0.8.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.8.0
[0.7.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.7.0
[0.6.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.6.0
[0.1.0]: https://github.com/chauduyphanvu/audible-deals/releases/tag/v0.1.0
