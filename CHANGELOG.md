# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/), and this project adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

Upgrading from 0.11.1? Read **Upgrade notes** first: your records are imported into a new database on the first run, a few exit statuses and webhook rules changed, and older releases cannot be run against the upgraded data directory.

### Upgrade notes

Breaking and behavior changes since 0.11.1.

- **Your records move into `state.db`.** Price history, the last result session, seen and dismissed ASINs, refresh eligibility, monitor baselines, alert cooldowns and tracking state moved from many JSON files into `state.db`; category lists and the `for-me` taste profile moved to `cache.db` (safe to delete; the categories are fetched again once after upgrading). `config.json`, `profiles.json`, `monitors.json`, `wishlist.json` and `auth.json` stay JSON. The first run imports the old files in a single transaction and renames them to `*.migrated` (`history/` becomes `history.migrated/`). Nothing is deleted, a failed import changes nothing and is retried, and unusable entries are skipped and reported by `deals doctor`.
  **Downgrade caveat:** do not run an older release against the same data directory afterwards. It would not see the imported records and would start new JSON files that this release ignores. Back up the directory first (`deals cache info` shows where it is) if you might go back. See "Local data" in the command guide.
- **Exit status 10 means "nothing matched".** `--exit-code` on `watch` and `notify` now exits **10** instead of 1 when nothing hit its target, so it can be told apart from a real error (1); `find`, `search` and `last` gain `--exit-code` with the same meaning. A busy run lock in `notify` exits 0 with a note instead of 1, and Ctrl-C exits 130 everywhere (it was 1 with "Aborted!"). Authentication problems exit 3 and failed Audible requests 4, a refused connection or a timeout included ("Audible request failed (could not connect)"). The retry warnings read `Warning: Audible request failed (could not connect); retrying in 1s (attempt 2 of 3)`; `-v` adds the endpoint and the raw error. The full table is "Exit codes and errors" in the command guide.
- **Teams alerts are Adaptive Cards.** `--webhook-format teams` sends an Adaptive Card for Power Automate Workflows webhooks, the replacement for the Office 365 connectors Microsoft retired. URLs on `*.webhook.office.com` (or `outlook.office.com/webhook/`) keep getting a MessageCard automatically; `--webhook-format teams-legacy` forces one.
- **Webhooks on your own network need an opt-in.** Webhook addresses are now checked when the alert is sent, not only when the URL is entered: the host is resolved once, a non-public address is refused (never retried) and the connection goes to the address that was checked. A webhook to `127.0.0.1` or a LAN address (self-hosted ntfy, Home Assistant), which used to be refused only when entered, now needs `deals config set webhook-allow-private true`. Behind an HTTP(S) proxy the proxy resolves the name, so `deals` only refuses a URL that names a private place outright (an address such as `10.0.0.5`, `localhost`) and leaves the rest to the proxy; hosts in `NO_PROXY` get the full check (see "Where a webhook may point" in the automation guide).
- **`deals notify` without a webhook always prints valid JSON.** An empty wishlist printed a sentence on stdout; it now prints `{"deals": [], "count": 0}` and the sentence goes to stderr. `notify --cooldown 0` is accepted and means no cooldown.
- **Catalog scans are cached for two hours.** Re-running or refining a `find`, `search`, `for-me` or `series` scan makes no API calls and says how old the prices are; `--refresh` fetches live data and `deals config set cache-ttl MINUTES` changes the lifetime (`0` turns it off). Cached prices are never recorded in the price history, and `track run`, `notify`, `watch`, `wishlist` and monitors always use live data. Progress and `--dry-run` count cached pages as free: `api_calls` now counts only requests that would reach Audible, and `cached_calls` is a new key in the dry-run JSON.
- **`find` also re-prices titles you have seen.** Up to 100 titles it showed you in the last 90 days, in the categories being scanned, are fetched live (at most two batch calls, shown in `--dry-run` as `known_candidate_calls` and counted toward the 60-call limit) and merged into the results, so a cheap title beyond the last scanned page, or one you first saw through `search`, still turns up. `--keywords` scans skip it; `--no-known` or `deals config set known-candidates false` turns it off. The product index behind it is schema version 2 of `state.db` (an automatic, additive migration).
- **Messages about a run go to stderr** as `Warning: ...` or `Note: ...`, no longer on stdout and without logger names. Errors are one line (`Error: ...`); `-v` restores the detailed format and `-vv` the traceback. With `--json`, an error is written to stderr as `{"error": {"code": N, "message": "..."}}`.
- **Requirements:** `click>=8.2`. Python 3.11 to 3.14 are supported.
- **Deprecated:** `deals watch --every` (use `deals track run --every`). It still works, prints a warning, and now takes the shared run lock and backs off after failures instead of ending with a traceback.

### Added

**Browsing and output**
- A full-screen results browser for `-i` on `find`, `search`, `for-me`, `series` and `last` (optional extra `tui`, which adds `textual>=8.2,<9`; included in the pre-built binaries): a table with the wide table's columns, a detail pane (Enter), filter-as-you-type (`/`, applied a moment after the last keystroke), sort cycling saved to the result session so `@N` and `deals last` follow it (`s`), marking (space), wishlist add with a target-price dialog (`w`), open (`o`), compare 2-4 (`c`), price history (`h`), dismiss (`n`), help (`?`) and quit (`q`). It is a view over the same actions as the prompt browser and opens only when textual is installed and stdin and stdout are terminals; otherwise, and with `deals config set interactive-ui prompt`, `-i` uses the prompt loop as before.
- On an interactive terminal `find` and `search` show a live "best so far" table under the progress bar. Ctrl-C during a scan now shows what was gathered ("Scan interrupted after X/Y pages"), saves the session so `@N` works, records history for the live-fetched items and exits 130 (before, it discarded the scan).
- `--color auto|always|never` on every command; an explicit `always`/`never` beats `NO_COLOR`/`FORCE_COLOR`.
- `--locale` and `-v/--verbose` are accepted after the command as well as before it.
- `deals completions bash|zsh|fish --install` writes the completion script to the per-user directory the shell loads from, without touching any startup file, and is safe to re-run after an upgrade.
- `deals profile save` and `deals monitor add` take every filter `find` takes; `find` and `search` gain `--no-exclude-seen` and `--no-require-history`; `deals config set max-effective-price N` sets a global default.

**Scripting**
- `--json` on `doctor`, `watch`, `track status`, `monitor list`, `monitor show`, `config list`, `config get`, `profile list`, `profile show`, `categories`, `detail`, `compare`, `cache info` and `notify`. New outputs are objects with `"schema_version": 1`; stdout carries only the document (hints, warnings and progress go to stderr); webhook URLs and header values are never printed. [docs/json.md](docs/json.md) documents every shape and `tests/golden/json` pins their key structure.
- `--exit-code` on `find`, `search` and `last` (exit 10 when nothing matched), and documented statuses for every command.
- `--refresh` on `find`, `search` and `series` (and extended on `for-me`).
- `DEALS_HOME` selects the data directory on every run (an existing `~/.config/audible-deals` is still kept; `%APPDATA%\audible-deals` on Windows otherwise). `deals doctor` and `deals cache info [--json]` show the directory and why it was chosen; `deals cache clear` empties only the disposable cache and gives its disk space back ("Cleared 40 cached item(s), freed 2,945,832 bytes"). `deals doctor` creates nothing: on a machine that has not saved anything yet it reports `state.db` and `cache.db` as "not created yet" instead of making them. `deals doctor` also checks `state.db` (`PRAGMA quick_check`, schema version), the outcome of the JSON import and `cache.db`; a `state.db` that cannot be read is moved to `state.db.corrupt-<timestamp>` with a one-line warning and recreated, and one written by a newer release is refused untouched.

**Automation and notifications**
- `deals track run --every 30m` runs the background job in the foreground for people without a scheduler: the same run lock as scheduled runs, exponential backoff with jitter after failures (capped at an hour), config re-read before each run, a clean stop on Ctrl+C or SIGTERM.
- `deals track install` writes the data directory into the scheduled job (launchd, systemd, cron, Task Scheduler) and prints it. Where systemd has no user session (containers, WSL, ssh), it no longer leaves unit files behind: it uses a crontab entry instead and says so, or, without cron, stops with the `deals track run --every ...` command to use. `deals doctor` warns when the installed schedule points at a program that no longer exists.
- `track.log` rotates at 1 MB (`track.log.1` to `.3`) and `deals track log` reads only the end of the file.
- Optional keychain storage of secrets: `deals config set secret-storage keyring|file` (extra `keyring`) moves the login and webhook URLs and headers between the data directory and the OS keychain, after a confirmation and with every secret verified before any file changes. Not bundled in the pre-built binaries; the default (`file`) never imports `keyring`.
- `--webhook-format teams-legacy`, and `webhook-allow-private` (see Upgrade notes).

**Distribution and development**
- One version authority: `audible_deals.__version__` in `src/audible_deals/__init__.py`. The package version is read from it, `deals --version` prints it (no more hardcoded fallback for frozen builds) and the release workflow rewrites that one line.
- Pre-built binaries are built from `uv.lock` with the `tui` extra and smoke-tested on every platform before they are published (`--version`, a dry-run search, `cache info`, textual present); CI builds and smoke-tests the Linux binary on every change.
- CI runs the tests on Linux (Python 3.11 to 3.14), macOS and Windows (3.13), in parallel with pytest-xdist, checks the built wheel's version, and runs ruff and mypy. The macOS and Windows jobs are new.
- The release workflow has a `pypi` job that publishes with PyPI trusted publishing. It is off until the repository owner completes the one-time setup in CONTRIBUTING.md; nothing is published by this change.
- Every filter's flag, type, bounds, help, default and availability is declared once in `src/audible_deals/filter_specs.py` (see "Adding a filter" in CONTRIBUTING.md); mypy is in the `dev` extra and CI; ruff checks `E`, `F`, `W`, `I`, `B`, `UP`; `tests/test_golden.py` pins every command's option names.
- Benchmarks: `benchmarks/benchmark_scan.py` (cold vs cached scans) and `benchmarks/benchmark_history.py` (history, recap and lookup on thousands of tracked ASINs).

### Changed

- Price-history reads are queries instead of "load every file": `deals recap` on 5,000 tracked titles takes under a third of the time and under half the memory, recording prices is about twenty times faster, and `deals history ASIN` opens one database. Ties in recap ordering and `--atl-all` are broken by ASIN rather than directory order.
- Webhook titles are escaped for the service they go to (Slack, Discord and Teams Markdown); Discord messages carry `allowed_mentions: {"parse": []}` so a title cannot ping `@everyone`. ntfy shows prices with the shared formatter and sends a non-ASCII `Title` as an RFC 2047 encoded word. Every format shares one provider interface.
- Every option of `deals last` and `deals profile save` now has help text, and `last`, `profile save` and `monitor add` complete genre names.
- A webhook URL with an invalid port (`https://example.com:99999/x`, `:abc`) is a usage error (exit 2) when entered, instead of a delivery failure later.
- `-y` is accepted wherever `--yes` is (`history --purge-older-than`, `config reset`, `monitor remove`); `recap --webhook-format` takes its choices from the same list as `notify`, `track install` and `monitor add`.
- `--released-after` and `--released-before` are checked when the command line is parsed (an invalid date is a usage error naming the option; `20240101` is still accepted).
- Every state file is read and written as UTF-8 regardless of the platform's locale encoding (files written in the old locale encoding still load). A zero-byte or whitespace-only data file is treated as empty without a warning.
- `deals login` no longer changes the process-wide umask; it writes the same JSON through the atomic 0600 writer. The launchd plist is built with `plistlib` and the systemd units and plist are written atomically. `track install` and `uninstall` update only the install record, atomically.
- `--skip-owned` reuses the list of owned titles for 24 hours (`deals library` and `--refresh` update it) and category names are cached for 7 days.
- Internal: one paginator and worker-pool lifecycle serve the catalog, library, wishlist and series readers, and genre matching lives in `genres.py`; requests, ordering and call counts are unchanged.
- Price history is purged per marketplace without the old "has no entries, skipping purge" warning; `-v`/`-vv` also apply to warnings emitted while loading saved configuration at startup.
- Catalog requests still ask for every response group, including `product_desc` and `media`, which nothing reads. Dropping them would shrink the responses, but needs one check against a live account that `language`, `subtitle`, `publisher_name` and `release_date` come from `product_attrs`; `tests/test_product_fields.py` records which group each parsed field is expected in.

### Fixed

- A `config.json`, `profiles.json`, `monitors.json` or `dismissed_asins.json` that could not be parsed (for example after a typo while hand-editing) was silently replaced with an empty file the next time a command saved it, deleting every saved profile, monitor or default. Commands that would write to such a file now stop with a one-line error naming the file, line and column, and leave it untouched; commands that only read it print a one-line warning.
- `deals track run` looked every wishlist title up in the command's marketplace, so a title added from another marketplace (`--locale uk wishlist add ...`) was priced in the wrong one. It now uses the same wishlist check as `notify`.
- A webhook that could not be delivered made `deals track run` fail without a trace in the run history. The failed run is now recorded (`deals track status` shows `WebhookDeliveryError: ...`) and the alert is not marked as sent, so the next run sends it again.
- Monitor alerts to ntfy failed to send because the `Title` header (`Audible Deals — <monitor>`) contains an em dash.
- Error messages that list choices on a second line (`deals find --genre nosuchgenre`) printed a literal `\n`, and the `--json` error object carried a doubled backslash. They now print real separate lines.
- `deals config set webhook URL` refused a bad URL with "Invalid value for '--webhook'", an option `config set` does not have; it names the `webhook` key now.
- Interactive mode printed the literal text `[dim]...[/dim]` around its "Invalid input" hint.
- `install.sh`, the README and the installation guide told users to `pip install audible-deals`, which fails because the package is not on PyPI; they now show `uv tool install git+https://github.com/chauduyphanvu/audible-deals` (or the `pipx` equivalent). The optional-extra hints do the same.

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
