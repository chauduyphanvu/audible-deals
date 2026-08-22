# Command guide

Run `deals --help` to see every command or `deals COMMAND --help` for authoritative option details.

## Command overview

| Command | Description |
|---------|-------------|
| `deals find` | Browse and filter catalog deals |
| `deals search [QUERY]` | Search by keyword; the query is optional with `--genre` or `--category` |
| `deals for-me` | Rank personalized deals using your library taste profile |
| `deals series` | Find continuation books in series represented in your library |
| `deals library` | List or export books in your Audible library |
| `deals last` | Re-display and re-filter the last search or find without an API call |
| `deals detail ASIN` | Show detailed information for one audiobook |
| `deals open ASIN` | Open an audiobook on Audible |
| `deals compare ASIN ASIN ...` | Compare audiobooks side by side |
| `deals categories` | List genres; use `--parent ID` to drill down |
| `deals wishlist` | Add, list, remove, sync, or repair tracked books |
| `deals watch` | Check wishlist prices |
| `deals history ASIN` | View local price history |
| `deals notify` | Print or send alerts for wishlist deals |
| `deals track` | Install and manage background price tracking |
| `deals monitor` | Save catalog searches and alert on new matches or price drops |
| `deals recap` | Summarize recent price changes |
| `deals profile` | Manage saved search profiles |
| `deals config` | Manage global defaults |
| `deals login` | Authenticate with Audible |
| `deals import-auth PATH` | Import audible-cli or Libation credentials |
| `deals doctor` | Check authentication, configuration, and marketplace access |
| `deals completions SHELL` | Generate bash, zsh, or fish completions |

## Find and search

`find` browses the catalog, while `search` accepts a title, author, or keyword query. Both commands support most of the same filters.

```bash
# Browse one genre
deals find --genre sci-fi --max-price 5

# Discounted thrillers, biggest discount first
deals find --genre thriller --on-sale --sort discount

# Search by author
deals search "Brandon Sanderson" --min-hours 5 --sort price

# Match any of several phrases and deduplicate the results
deals search "WWII | second world war | world war 2" --on-sale

# Browse a genre without a keyword
deals search --genre romance --max-price 3
```

Use `--deep` to scan three catalog sort orders for broader coverage. It makes roughly three times as many API calls. Use `--dry-run` to preview the work without fetching anything.

### Filters

| Flag | Purpose |
|------|---------|
| `--max-price 5.00` | Set the maximum cash price; `find` defaults to 5 |
| `--genre sci-fi` | Fuzzy-match a genre name or common alias |
| `--category ID` | Use an Audible category ID instead of a genre name |
| `--exclude-genre erotica` | Exclude a genre; repeatable |
| `--keywords "space opera"` | Filter within a category browse on `find` |
| `--narrator "Reynolds"` | Match part of a narrator name |
| `--author "Andy Weir"` | Match part of an author name |
| `--series "Bobiverse"` | Match part of a series name |
| `--publisher "Podium"` | Match part of a publisher name |
| `--exclude-author "Maas"` | Exclude an author; repeatable |
| `--exclude-narrator "Bray"` | Exclude a narrator; repeatable |
| `--exclude-keyword "abridged"` | Exclude a title or subtitle keyword; repeatable |
| `--hist-below 25` | Keep prices at or below a tracked-history percentile |
| `--min-price-drop 30` | Require a percentage drop from the last tracked price |
| `--require-history` | Drop items without enough history for a history filter |
| `--released-after 2024-01-01` | Keep releases on or after a date |
| `--released-before 2025-01-01` | Keep releases on or before a date |
| `--min-rating 4.0` | Require a minimum star rating |
| `--min-ratings 100` | Require a minimum rating count |
| `--min-hours 5` | Require a minimum audio length |
| `--on-sale` | Keep only discounted items |
| `--min-discount 70` | Require a minimum discount percentage |
| `--language english` | Select a language; defaults to the locale language |
| `--all-languages` | Include every language |
| `--first-in-series` | Keep only the first book in each series |
| `--max-price-per-hour 0.50` | Set a maximum cash price per hour |
| `--max-effective-price 12` | Limit the cheaper of cash and one configured credit |
| `--skip-owned` | Exclude books already in your library |
| `--skip-plus` | Exclude Audible Plus titles |
| `--only-plus` | Keep only Audible Plus titles |
| `--exclude-seen` | Exclude ASINs returned by earlier searches |
| `-n, --limit 20` | Cap results; use `-n 0` for no limit |
| `--pages 10` | Set catalog pages per scan |
| `--deep` | Scan three server-side sort orders |
| `--subcategories` | Scan each resolved subcategory separately on `find` |
| `--dry-run` | Preview sort orders, pages, and API calls |
| `--show-url` | Include Audible URLs in the table |
| `-i, --interactive` | Browse the results interactively |
| `--profile NAME` | Load a saved profile on `find` or `search` |

`--skip-plus` and `--only-plus` cannot both be true in one source layer; cross-layer rules are described under [Global defaults](#global-defaults). Use `deals categories` to look up category IDs.

`--hist-below` requires at least five history entries for percentile comparison. By default, history filters let items without enough data pass through; add `--require-history` when those items should be excluded. Release dates use `YYYY-MM-DD`.

### Sort options

`price`, `-price`, `discount`, and `price-per-hour` are calculated after fetching. The remaining sorts are provided by Audible.

| Sort | Description |
|------|-------------|
| `price` | Cheapest first |
| `-price` | Most expensive first |
| `discount` | Largest discount first |
| `price-per-hour` | Lowest cost per hour; the default for `find` |
| `value` | `(rating × hours) / price` |
| `rating` | Highest rated first |
| `bestsellers` | Audible bestseller ranking |
| `length` | Longest first |
| `date` | Newest first |
| `title` | Alphabetical by title |
| `relevance` | Audible relevance; the default for `search` |

## Personalized deals

`deals for-me` builds a local taste profile from your library, including favorite authors, narrators, genres, and series in progress.

```bash
deals for-me
deals for-me --max-price 5 --on-sale --min-rating 4
deals for-me --exclude-author "Maas" --skip-plus --sort discount
deals for-me --refresh

# Requires a cached profile; run deals for-me once first
deals for-me --dry-run
```

The recommendation score adds 5 points for the `next in SERIES` continuation and 2 for another `in SERIES` match. Favorite authors, narrators, genres, and favorable price history add further boosts. The `Match` column explains each result.

Owned books are always excluded. Results feed `deals last` and the price-history tracker. The profile is cached in `taste_cache.json` for 24 hours and never leaves your machine.

## Series continuations

`deals series` looks for books you do not own in series where your library already contains multiple titles.

```bash
# Scan the 20 series you are most invested in
deals series

# Require three owned books and keep continuations under $10
deals series --min-books 3 --max-price 10

# Inspect missing books in each series instead of a flat deals table
deals series --gaps

# Restrict the scan to one series
deals series --series "Expeditionary Force" --on-sale
```

Use `--max-series` to cap the number of series scanned and `--pages` to control catalog pages per series. Series identity uses the series ASIN when available, otherwise a Unicode-normalized, case-folded, whitespace-collapsed name. Within a series, book identity uses the position when it contains exactly one number, otherwise the normalized title. These identities remove owned books and duplicate editions; a priced edition beats an unavailable one, then the cheaper price wins. An ASIN associated with multiple series appears once in the flat view while retaining every match.

The flat result view supports price, rating, length, sale, sort, limit, interactive, and export options, but omits unpriced books. `--gaps` retains and labels them unavailable; `--max-price` filters priced gaps without removing unavailable ones.

## Last results

`deals last` reuses the complete candidate pool from the most recent `find`, `search`, `for-me`, or flat `series` view without making an API call. Refinements are cumulative: an explicit option replaces that part of the current recipe and is retained for the next invocation. Repeatable options replace their complete inherited list.

```bash
deals last
deals last --sort discount
deals last --max-price 8
deals last --clear-filter language
deals last --reset
deals last --reset --max-price 3 --min-rating 4.5
deals last --narrator "R.C. Bray" --min-ratings 100
deals last --language english
deals last -o last.csv
deals last --count
deals last --clear-seen
deals last --clear-dismissed
deals last --clear
```

References only address rows in the current, limit-applied view. Use `@N`,
`@N-M,...`, an ASIN, or a recognized Audible product URL:

```bash
deals detail @1
deals open https://www.audible.com/pd/example/B00EXAMPLE
deals compare @1 @3
deals wishlist add @1-3,5 --max-price 5
deals compare B00EXAMPLE @2
```

`--last` remains supported as a compatibility spelling. Single-item commands such as `detail`, `open`, and `history` reject ranges. Bulk commands deduplicate selections in first-seen order. A URL or cached row supplies its marketplace unless global `--locale` was explicitly provided.
Wishlist additions retain that marketplace for later `watch`, `notify`, and price-history checks.

Product lists use a full table at 120 columns and wider, a compact table from 80–119 columns, and wrapped cards below 80 columns. Optional Buy, history, Match, identifiers, series details, and URLs fold into secondary lines instead of disappearing.

Seen results used by `--exclude-seen`, dismissed results, and the last-results cache are independent. Clear them with `deals last --clear-seen`, `deals last --clear-dismissed`, and `deals last --clear`, respectively.

## Interactive mode

Add `-i` to `find`, `search`, `for-me`, flat `series`, or `last`, then enter an action:

| Input | Action |
|-------|--------|
| `3` or `@3` | View item 3 |
| `o @3` | Open item 3 in a browser |
| `w @1-3,5` | Add several items to the wishlist |
| `c @1 @3` | Compare two items |
| `h @3` | Show price history |
| `s discount` | Re-sort in place |
| `n 1-3` | Dismiss items globally and remove them from the current view immediately |
| `q` | Quit |

Dismissed ASINs are excluded from future `find`, `search`, `for-me`, `series`, monitor, and `last` results.

## Saved profiles

Profiles store reusable `find` and `search` options:

```bash
deals profile save my-scifi --genre sci-fi --max-price 5 --min-rating 4 --min-hours 8 --first-in-series --deep
deals find --profile my-scifi
deals search "Brandon Sanderson" --profile my-scifi
deals profile list
deals profile show my-scifi
deals profile delete my-scifi
```

Most filter and sort flags can be saved. An explicit CLI option overrides the profile value. A profile cannot save both `--skip-plus` and `--only-plus` as true.

## Saved-search monitors

Monitors freeze resolved settings and locale when created. Create either a profile-backed `find` monitor or a direct-query `search` monitor; `--profile` and `--query` are mutually exclusive. `track run` checks enabled monitors under its normal run lock. The first successful scan is a silent baseline; later scans emit `new` and `price_drop` events. Monitors with no price are omitted because they cannot produce a purchasable-deal alert.

The catalog-page estimate printed by `add`, `list`, and `show` is used to keep scheduled scans bounded: enabled monitors share a 60-call page-scan budget per run. When the total is larger, runs rotate deterministically through the monitor list.

```bash
deals monitor add sci-fi-steals --profile my-scifi
deals monitor add sanderson --query "Brandon Sanderson" --max-price 6
deals monitor add sanderson --query "Brandon Sanderson | Martha Wells" --webhook https://example.invalid/hook
deals monitor list
deals monitor show sci-fi-steals
deals monitor test sci-fi-steals
deals monitor pause sci-fi-steals
deals monitor resume sci-fi-steals
deals monitor remove sci-fi-steals --yes
```

For monitor creation, explicit supported filter options (such as `--max-price`, `--pages`, `--sort`, `--genre`, `--author`, `--on-sale`, and `--skip-owned`) override profile and global values before the settings are frozen. Monitor-specific webhooks never receive global custom webhook headers.

## Global defaults

```bash
deals config set max-price 5
deals config set skip-owned true
deals config set sort discount
deals config get max-price
deals config list
deals config reset max-price
deals config reset
```

Precedence is `explicit CLI > profile > global config > built-in default`. For `skip-plus` and `only-plus`, a higher layer's true value disables the lower opposite; false values follow normal precedence without conflicting. Both true in one layer is invalid. Setting either to `true` with `deals config set` removes and reports the opposite persisted key; setting it to `false` leaves the opposite key unchanged. Existing same-layer conflicts are diagnosed rather than silently rewritten. Set a default marketplace with `deals config set locale uk`.

### Credit-aware recommendations

Set the amount you pay for one credit:

```bash
deals config set credit-price 11.25
```

Result tables, `watch`, `detail`, and `compare` then recommend `cash`, `credit`, or `plus`. `--max-effective-price` filters on the cheaper of cash and one credit, and notification payloads include `verdict` and `effective_price`.

Remove the setting with `deals config reset credit-price` to restore cash-only output.

## Library and exports

```bash
deals library
deals library --json > my-books.json
deals library -o library.csv
deals library --sort rating -n 20
```

`find`, `search`, and `last` support the same export controls:

| Flag | Purpose |
|------|---------|
| `-o, --output FILE` | Write `.json` or `.csv` and suppress the table |
| `--json` | Print JSON to standard output |
| `-q, --quiet` | Suppress the table |

Multi-value CSV fields are joined with a semicolon and space.

## Marketplace support

Pass `--locale` before the command. Currency symbols, Audible URLs, and price-per-hour headings adjust automatically.

```bash
deals --locale uk find --genre fantasy --max-price 3
deals --locale de detail B00R6S1RCY
```

| Locale | Currency | Domain |
|--------|----------|--------|
| `us` | $ | www.audible.com |
| `uk` | £ | www.audible.co.uk |
| `ca` | CA$ | www.audible.ca |
| `au` | A$ | www.audible.com.au |
| `in` | ₹ | www.audible.in |
| `de` | € | www.audible.de |
| `fr` | € | www.audible.fr |
| `jp` | ¥ | www.audible.co.jp |
| `es` | € | www.audible.es |

## Shell completions

```bash
deals completions bash >> ~/.bashrc
deals completions zsh >> ~/.zshrc
deals completions fish > ~/.config/fish/completions/deals.fish
```

After reloading the shell, profiles, genres, and excluded genres support tab completion. Profile `show` and `delete` commands also complete profile names.

## Debugging

```bash
deals -v find --genre sci-fi --max-price 5
deals -vv find --genre sci-fi
DEALS_DEBUG=1 deals find --genre sci-fi
DEALS_LOG_FILE=~/.cache/deals.log deals notify --webhook https://example.com/hook
```

`DEALS_LOG_FILE` captures DEBUG records even without `-v`, which is useful for scheduled runs. The log rotates at 5 MB and keeps three backups. Transient API failures are retried twice with jittered exponential backoff.

## Local data

All application data is stored in `~/.config/audible-deals/`:

| Path | Contents |
|------|----------|
| `auth.json` | Audible credentials |
| `config.json` | Global defaults |
| `wishlist.json` | Local watchlist |
| `profiles.json` | Saved profiles |
| `last_results.json` | Versioned candidate session from the latest flat `find`, `search`, `for-me`, or `series` result view |
| `seen_asins.json` | ASINs returned by earlier result views for `--exclude-seen` |
| `dismissed_asins.json` | ASINs dismissed globally with interactive `n` |
| `refresh_eligibility.json` | Versioned per-marketplace dates for recently surfaced products eligible for background refresh |
| `history/` | Per-ASIN price history |
| `categories_cache.*.json` | Per-locale category caches |
| `track_state.json`, `track.log` | Background tracking state and log |
| `taste_cache.json` | Local taste profile for `for-me` |

## Related guides

- [Installation and authentication](installation.md)
- [Price tracking and automation](automation.md)
- [Advanced recipes](recipes.md)
