# JSON output

Commands that report something take `--json` and print one JSON document on stdout, so scripts can use `jq` or any JSON parser. This page lists every shape.

## Rules that hold for every `--json` command

- **stdout is the document and nothing else.** Progress, hints, warnings and notes go to stderr, so `deals ... --json | jq` always parses.
- **A failure leaves stdout empty** and prints one JSON object on stderr, with the [exit status](commands.md#exit-codes-and-errors) as `code`:

    ```json
    {"error": {"code": 3, "message": "Not authenticated. Run 'deals login' first."}}
    ```

    The exceptions are a Ctrl-C during `find`/`search` (the titles gathered so far are still printed, then the error object with code 130) and `deals doctor`, which prints its report and exits 1 when a check fails.
- **No `NaN` or `Infinity`.** A value that has no number is `null`.
- **Schema version 1.** Everything described here is version 1. Outputs that were added together with this page are objects whose first key is `"schema_version": 1`. Older outputs keep the shapes they always had (some are lists, so there is nowhere to put a version) and count as version 1 too. A change that removes or renames a key, or changes what a value means, would increase the version; adding a key is not a change in version, so ignore keys you do not know.
- Secrets never appear: webhook URLs and header values are shown as `<redacted>`, or as `has_webhook` flags.

The shapes are pinned by `tests/golden/json`, so they cannot change by accident.

| Command | Output |
|---------|--------|
| `find`, `search`, `for-me`, `last`, `library`, `series` | [list of products](#products) |
| `find --dry-run` | [scan plan](#scan-plan) |
| `library --stats` | [library statistics](#library-statistics) |
| `series --gaps` | [series gaps](#series-gaps) |
| `history ASIN` / `history --all` | [price history](#price-history) |
| `wishlist list` | [wishlist](#wishlist) |
| `recap` | [recap](#recap) |
| `notify` | [deals](#notify) |
| `watch` | [watch](#watch) |
| `detail`, `compare` | [detail and compare](#detail-and-compare) |
| `categories` | [categories](#categories) |
| `doctor` | [doctor](#doctor) |
| `cache info` | [cache info](#cache-info) |
| `track status` | [track status](#track-status) |
| `monitor list`, `monitor show` | [monitors](#monitors) |
| `config list`, `config get` | [config](#config) |
| `profile list`, `profile show` | [profiles](#profiles) |

## Products

`find`, `search`, `last`, `library` and `series` print a list of products (an empty list when nothing matched). Each product:

```json
{
  "asin": "B0EXAMPLE1",
  "title": "Project Hail Mary",
  "subtitle": "",
  "full_title": "Project Hail Mary",
  "authors": ["Andy Weir"],
  "narrators": ["Ray Porter"],
  "publisher": "Audible Studios",
  "price": 4.95,
  "list_price": 24.95,
  "discount_pct": 80,
  "length_minutes": 970,
  "hours": 16.2,
  "price_per_hour": 0.31,
  "rating": 4.8,
  "num_ratings": 120000,
  "categories": ["Science Fiction & Fantasy"],
  "category_ids": ["18580606011"],
  "series_name": "",
  "series_position": "",
  "series_asin": "",
  "language": "english",
  "release_date": "2021-05-04",
  "in_plus_catalog": false,
  "locale": "us",
  "url": "https://www.audible.com/pd/B0EXAMPLE1"
}
```

`price`, `list_price` and `price_per_hour` are `null` when Audible lists no price. Prices are in the currency of `locale`.

## Scan plan

`find --dry-run --json` prints what a scan would request, without making any catalog request:

```json
{
  "dry_run": true,
  "category": "Science Fiction & Fantasy",
  "subcategories": 1,
  "query": "golden",
  "result_sort": "price",
  "limit": 25,
  "profile": null,
  "filters": ["max price $20.00"],
  "sort_orders": ["BestSellers", "Price"],
  "pages_per_sort": 1,
  "max_items": 100,
  "api_calls": 2,
  "cached_calls": 0,
  "known_candidate_calls": 0,
  "catalog_call_limit": 60,
  "requires_large_scan_override": false
}
```

`category`, `query`, `limit` and `profile` are `null` when not set. `api_calls` is the number of requests that would reach Audible: the plan, minus pages the [catalog cache](commands.md#cached-results) already holds, plus the calls that re-price titles from earlier scans. `api_calls` and `requires_large_scan_override` are `null` when the size of the scan is not known in advance.

## Library statistics

`library --stats --json`:

```json
{
  "total_books": 312,
  "total_hours": 4120.5,
  "avg_hours": 13.2,
  "avg_rating": 4.41,
  "top_genres": [{"name": "Science Fiction & Fantasy", "count": 120}],
  "top_authors": [{"name": "Brandon Sanderson", "count": 14}],
  "top_narrators": [{"name": "Michael Kramer", "count": 11}]
}
```

Each `top_*` list has at most five entries.

## Series gaps

`series --gaps --json` lists, for each series you own at least `--min-books` books of, the unowned books found:

```json
[
  {
    "series": "The Stormlight Archive",
    "owned": 4,
    "total_known": 5,
    "missing": [
      {"asin": "B0EXAMPLE5", "title": "Rhythm of War", "position": "4", "price": 7.49}
    ]
  }
]
```

## Price history

`history ASIN --json` is the recorded prices of one title, oldest first:

```json
[{"date": "2026-06-01", "price": 12.0, "title": "Golden Wishlist Book"}]
```

`history --all --json` is an object holding the same lists, keyed by ASIN, for the selected marketplace.

## Wishlist

`wishlist list --json`:

```json
{
  "items": [
    {"asin": "B0EXAMPLE1", "title": "Project Hail Mary", "max_price": 6.0, "added": "2026-06-01", "locale": "us"}
  ],
  "author_watches": [
    {"type": "author", "author": "Brandon Sanderson", "max_price": 5.0, "added": "2026-06-01"}
  ]
}
```

`max_price` is `null` for an item with no target. Items saved by older releases may not have a `locale`; they use your default marketplace.

## Recap

`recap --json`:

```json
{
  "days": 7,
  "new_count": 3,
  "drops": [
    {"asin": "B0EXAMPLE1", "title": "Project Hail Mary", "old_price": 12.0, "new_price": 5.0, "drop_pct": 58}
  ],
  "wishlist_hits": [{"asin": "B0EXAMPLE1", "title": "Project Hail Mary"}]
}
```

## Notify

`deals notify` prints this when no `--webhook` is given, and also with `--json` when it sends to a webhook. It is always valid JSON, including `{"deals": [], "count": 0}` for an empty wishlist, nothing at target, or a run that found the lock busy (the explanation is on stderr). This shape has no `schema_version`.

```json
{
  "deals": [
    {
      "asin": "B0EXAMPLE1",
      "title": "Project Hail Mary",
      "price": 3.0,
      "target": 5.0,
      "url": "https://www.audible.com/pd/B0EXAMPLE1"
    }
  ],
  "count": 1
}
```

A hit found through an author watch also has `"author"`. With a configured credit price, a hit has `"verdict"` and `"effective_price"` too.

## Watch

`watch --json` checks the wishlist once (it cannot be combined with `--every`) and lists every wishlist title, or only the ones at target with `--buy-only`:

```json
{
  "schema_version": 1,
  "hits": 1,
  "items": [
    {
      "asin": "B0EXAMPLE1",
      "title": "Project Hail Mary",
      "authors": ["Andy Weir"],
      "price": 3.0,
      "list_price": 24.95,
      "currency": "$",
      "target": 5.0,
      "status": "buy",
      "verdict": "credit",
      "url": "https://www.audible.com/pd/B0EXAMPLE1"
    }
  ],
  "missing": ["B0EXAMPLE9"],
  "author_watches": 1
}
```

- `status` is `buy` (at or below `target`), `discounted` (cheaper than list price but above target), `waiting` or `unavailable` (no price).
- `hits` counts the `buy` titles, even with `--buy-only`.
- `verdict` is `plus`, `cash` or `credit` when a [credit price](commands.md#credit-aware-recommendations) is configured, otherwise `null`.
- `missing` lists wishlist ASINs Audible did not return. Author watches are only counted here; `notify` checks them.

## Detail and compare

`detail ASIN --json`:

```json
{
  "schema_version": 1,
  "product": {"asin": "B0EXAMPLE1", "title": "Project Hail Mary", "...": "..."},
  "credit_price": 10.0,
  "verdict": "cash",
  "effective_price": 4.95
}
```

`product` has the [product](#products) keys. `credit_price`, `verdict` and `effective_price` are `null` unless a credit price is configured.

`compare ASIN ASIN... --json`:

```json
{
  "schema_version": 1,
  "products": [{"asin": "B0EXAMPLE1", "...": "...", "verdict": "cash"}],
  "missing": ["B0EXAMPLE9"],
  "credit_price": 10.0
}
```

Each entry of `products` is a product plus `verdict`. At least two products must be found, otherwise the command fails with exit status 1.

## Categories

`categories --json`, with `--parent ID` for subcategories:

```json
{
  "schema_version": 1,
  "locale": "us",
  "parent": null,
  "categories": [{"id": "18580606011", "name": "Science Fiction & Fantasy"}]
}
```

## Doctor

`doctor --json` runs the same checks as the table. It exits 1 when `ok` is false:

```json
{
  "schema_version": 1,
  "ok": false,
  "failures": 1,
  "warnings": 0,
  "checks": [
    {"name": "Auth file present", "status": "fail", "detail": "Run 'deals login' or 'deals import-auth'"}
  ]
}
```

`status` is `pass`, `warn` or `fail`. The names of the checks may change between releases; key off `status`, `ok` and the counts.

## Cache info

`cache info --json` reports where the data lives and what each database holds. It is one of the older outputs, so it has no `schema_version` (version 1 by definition):

```json
{
  "data_dir": "/home/me/.config/audible-deals",
  "data_dir_reason": "default ~/.config/audible-deals",
  "state_db": {
    "path": "/home/me/.config/audible-deals/state.db",
    "schema": 2,
    "bytes": 135776,
    "rows": {"price_history": 412, "products": 96, "seen": 240, "dismissed": 3, "...": 0},
    "last_result_session_saved_at": "2026-10-20T10:27:40+00:00",
    "json_import": {
      "at": "2026-10-20T10:27:40+00:00",
      "sources": ["history", "seen_asins.json"],
      "counts": {"price_history": 412, "seen": 240},
      "skipped": {},
      "notes": [],
      "renames_pending": []
    }
  },
  "cache_db": {
    "path": "/home/me/.config/audible-deals/cache.db",
    "schema": 1,
    "bytes": 45136,
    "namespaces": {"categories": {"entries": 1, "newest": "2026-10-20T10:27:40+00:00"}}
  }
}
```

`state_db.rows` has one count per table. `json_import` is the record of the one-time import of the JSON files earlier releases kept (see [Local data](commands.md#local-data)): which sources it read, how many records it brought in, how many entries it skipped and why, and any `.migrated` renames still pending. It is `null` if no import has run. `cache_db` is disposable: `deals cache clear` empties it.

## Track status

`track status --json`; `--history` adds `history`, every recorded run, newest first:

```json
{
  "schema_version": 1,
  "installed": true,
  "schedule": {
    "every": "6h",
    "method": "launchd agent",
    "installed_at": "2026-06-01T09:00:00",
    "present": true,
    "where": "~/Library/LaunchAgents/..."
  },
  "monitors": {"enabled": 2, "with_errors": ["cheap-sf"]},
  "last_run": {
    "at": "2026-06-09T08:00:00",
    "duration_s": 4.2,
    "wishlist_checked": 3,
    "extra_tracked_checked": 7,
    "hits": 1,
    "monitors_checked": 2,
    "monitor_events": 0,
    "error": null
  }
}
```

`schedule` is `null` when nothing is installed and `last_run` is `null` before the first run. `present` says whether the OS scheduler still has the entry. A failed run has a message in `error`.

## Monitors

`monitor list --json`:

```json
{
  "schema_version": 1,
  "monitors": [
    {
      "name": "cheap-sf",
      "valid": true,
      "enabled": true,
      "mode": "find",
      "locale": "us",
      "query": null,
      "profile": "scifi",
      "has_webhook": true,
      "estimated_calls": 4,
      "matches": 12,
      "last_success": "2026-06-09T08:00:00",
      "last_error": null
    }
  ]
}
```

A monitor whose definition is damaged has `"valid": false` and an `error` message, and the others are still listed. `monitor show NAME --json` is `{"schema_version": 1, "monitor": {...}}` with the same keys plus `settings`, the frozen options of the monitor. The webhook URL, headers and template are never printed.

## Config

`config list --json` (webhook URL and header values are `<redacted>` unless you pass `--show-secrets`):

```json
{"schema_version": 1, "config": {"max_price": 5.0, "webhook": "<redacted>"}}
```

`config get KEY --json`:

```json
{"schema_version": 1, "key": "max_price", "set": true, "value": 5.0}
```

For a key that is not set, `set` is `false` and `value` is `null`.

## Profiles

`profile list --json`:

```json
{
  "schema_version": 1,
  "profiles": [
    {"name": "scifi", "valid": true, "options": {"genre": "sci-fi", "max_price": 5.0}},
    {"name": "old", "valid": false, "options": null, "error": "genre must be text"}
  ]
}
```

`profile show NAME --json` is `{"schema_version": 1, "profile": {"name": "scifi", "options": {...}}}`. A damaged profile cannot be shown; the command fails with exit status 1.
