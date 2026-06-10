# audible-deals

A command-line tool for finding cheap Audible audiobooks. It scans the Audible catalog, filters by price and genre, and helps you snag deals before they disappear.

## What it does

- **Find deals** — scan hundreds of catalog pages and surface audiobooks under your price threshold
- **Search** — keyword search with filters for genre, rating, length, narrator, language, and more
- **Deep scan** — hit the catalog from multiple angles (bestsellers, newest, highest-rated) to maximize coverage (works on `find` and `search`)
- **Last results** — re-sort and re-filter your most recent results without any API calls; reference items by number in other commands
- **Compare** — side-by-side comparison of multiple audiobooks, with a $/hr value calculation
- **Interactive mode** — browse results, view details, open in browser, or add to wishlist without leaving the CLI
- **Wishlist & watch** — save ASINs you're eyeing and check back for price drops
- **Price history** — automatically tracks prices over time with sparkline charts and relative dates; flags an all-time-low with a ★ badge and shows a `vs hist` column with the percent above/below the historical median
- **Credit-aware buy advice** — tell it what a credit costs you and every table shows whether to pay cash, burn a credit, or grab it free on Plus
- **Saved profiles** — save your favorite search configurations and reuse them with `--profile` (works on `find` and `search`)
- **Global config** — set persistent defaults (max price, skip-owned, locale, etc.) applied to every command
- **Recap & notify** — get a summary of recent price drops, or send webhook notifications (Slack / Discord / Teams / ntfy) for deals at target
- **Doctor** — run `deals doctor` for a one-shot diagnostic of auth, config, and marketplace reachability
- **Locale support** — correct currency symbols and Audible URLs for all 9 marketplaces
- **Export** — dump results to JSON or CSV; using `-o` automatically suppresses the table display

## Quick start

### Install

**One-liner (macOS / Linux):**

```bash
curl -fsSL https://raw.githubusercontent.com/chauduyphanvu/audible-deals/main/install.sh | bash
```

This detects your OS and architecture, downloads the right binary, and installs it to `~/.local/bin`. If that directory isn't in your PATH, the script adds it automatically. You may need to restart your terminal (or run `source ~/.zshrc`) for the `deals` command to become available.

**Manual download:**

Grab the archive for your platform from [Releases](https://github.com/chauduyphanvu/audible-deals/releases/latest):

| Platform | File |
|----------|------|
| macOS (Apple Silicon) | `deals-macos-arm64.tar.gz` |
| Linux (x64) | `deals-linux-x64.tar.gz` |
| Windows (x64) | `deals-windows-x64.zip` |

```bash
# Example: macOS Apple Silicon
tar xzf deals-macos-arm64.tar.gz
mkdir -p ~/.local/lib/deals
mv deals-macos-arm64/* ~/.local/lib/deals/
ln -sf ~/.local/lib/deals/deals ~/.local/bin/deals
```

**Windows (recommended — install from source):**

The pre-built Windows binary may not work on all systems. Install from source instead (requires [Python 3.11+](https://www.python.org/downloads/)):

```
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e .
```

Then open **Command Prompt**, **PowerShell**, or **Windows Terminal** and run `deals` from there.

> **Note:** `deals` is a command-line tool. Do not double-click it — always run it from a terminal, or the window will open and immediately close.

**From source on macOS / Linux (requires Python 3.11+):**

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e .
```

### Authenticate

You need an Audible account. The easiest method:

```bash
deals login --external --via-file /tmp/url.txt
```

This opens a sign-in URL in your browser. After logging in, you'll land on a "page not found" — that's expected. Copy that URL, save it to `/tmp/url.txt`, and press Enter.

Alternatively, import auth from [audible-cli](https://github.com/mkb79/audible-cli) or [Libation](https://github.com/rmcrackan/Libation):

```bash
deals import-auth ~/.audible/auth.json          # audible-cli
deals import-auth ~/Libation/AccountsSettings.json  # Libation
```

### Find deals

```bash
# Sci-fi under $5
deals find --genre sci-fi --max-price 5

# Thrillers on sale, sorted by biggest discount
deals find --genre thriller --sort discount --on-sale

# Deep scan romance for maximum coverage
deals find --genre romance --max-price 3 --deep

# Cheap long listens (sorted by $/hr value by default)
deals find --min-hours 10 --max-price 5

# Only well-reviewed books narrated by a specific person
deals find --narrator "Tim Gerard Reynolds" --min-ratings 1000 --max-price 5

# Interactive mode — browse results, view details, open in browser
deals find --genre sci-fi --max-price 5 -i

# Use a saved profile
deals find --profile my-scifi
```

### Search by keyword

```bash
deals search "Brandon Sanderson" --sort price --min-hours 5

# Only show the first book in each series
deals search "Red Rising" --first-in-series

# Deep scan for broader coverage
deals search "Sanderson" --deep --max-price 5

# OR search — combine keywords, deduplicate results automatically
deals search "WWII | second world war | world war 2" --max-price 5 --on-sale

# Browse a genre without a keyword (QUERY is optional with --genre)
deals search --genre romance --max-price 3

# Show Audible URLs in the table
deals search "Dune" --max-price 5 --show-url
```

## Commands

| Command | Description |
|---------|-------------|
| `deals find` | Browse and filter deals (the main command) |
| `deals search [QUERY]` | Search by keyword with filters (QUERY optional if `--genre`/`--category` is given) |
| `deals library` | List all books in your Audible library (exportable) |
| `deals last` | Re-display results from the last search or find (no API call) |
| `deals detail ASIN` | Detailed info for a single audiobook |
| `deals open ASIN` | Open the Audible page in your browser |
| `deals compare ASIN ASIN ...` | Side-by-side comparison |
| `deals categories` | List genres (use `--parent ID` to drill in) |
| `deals wishlist add/list/remove` | Manage your watchlist |
| `deals wishlist sync` | Pull your Audible account wishlist for local price tracking and alerts |
| `deals watch` | Check wishlist prices — highlights items at/below target |
| `deals notify` | Send deal notifications via webhook or JSON stdout |
| `deals profile save/list/show/delete` | Manage saved search profiles |
| `deals config set/get/list/reset` | Manage global defaults applied to all commands |
| `deals history ASIN` | View price history with sparkline chart |
| `deals recap` | Summary of recent price drops across tracked items |
| `deals login` | Authenticate with Audible |
| `deals import-auth PATH` | Import auth from audible-cli or Libation |
| `deals doctor` | Diagnostic checks for auth, config, and marketplace reachability |
| `deals completions SHELL` | Generate shell completions (bash/zsh/fish) |

## Filtering & sorting

### Filters

| Flag | What it does |
|------|-------------|
| `--max-price 5.00` | Only items under this price (default: $5 for `find`, no default for `search`) |
| `--genre sci-fi` | Fuzzy genre match — `sci-fi`, `sf`, `thriller`, `horror`, `true crime`, `historical`, `ya`, etc. |
| `--category ID` | Filter by category ID (alternative to `--genre` — use `deals categories` to find IDs) |
| `--exclude-genre erotica` | Remove genres from results (repeatable) |
| `--keywords "space opera"` | Keyword filter within a category browse (`find` only) |
| `--narrator "Reynolds"` | Filter by narrator name (case-insensitive substring match, client-side) |
| `--author "Andy Weir"` | Filter by author name (case-insensitive substring match) |
| `--series "Bobiverse"` | Filter by series name (case-insensitive substring match) |
| `--exclude-author "Maas"` | Exclude books by a matching author (repeatable) |
| `--exclude-narrator "Bray"` | Exclude books by a matching narrator (repeatable) |
| `--exclude-keyword "abridged"` | Drop results whose title or subtitle contains the keyword (case-insensitive, repeatable) |
| `--min-rating 4.0` | Minimum star rating |
| `--min-ratings 100` | Minimum number of ratings (default: 1 for `find`, filters unreviewed books) |
| `--min-hours 5` | Minimum audio length |
| `--on-sale` | Only discounted items |
| `--min-discount 70` | Only items with at least this discount percentage |
| `--language english` | Filter by language (default: locale language) |
| `--all-languages` | Include all languages |
| `--first-in-series` | Only show book 1 of each series |
| `--max-price-per-hour 0.50` | Only items under this $/hr threshold (e.g. `0.35` for heavy value filtering) |
| `--max-effective-price 12` | Only items whose effective price — the cheaper of cash and one credit — is under this (see [Credit-aware buy advice](#credit-aware-buy-advice)) |
| `--skip-owned` | Exclude books already in your library |
| `--skip-plus` | Exclude Audible Plus catalog titles (free with membership) |
| `--only-plus` | Show only Audible Plus catalog titles (mutually exclusive with `--skip-plus`) |
| `--exclude-seen` | Exclude previously-seen ASINs (cumulative across runs; clear with `deals last --clear-seen`) |
| `-n, --limit 20` | Cap the number of results (default: 25; use `-n 0` for unlimited) |
| `--pages 10` | Number of catalog pages to scan (default: 10 for `find`, 3 for `search`) |
| `--deep` | Scan with 3 sort orders for broader coverage — 3x the API calls (`find` and `search`) |
| `--subcategories` | Scan each subcategory of the resolved genre separately — surfaces items buried in broad top-level buckets (`find` only, requires `--genre` or `--category`) |
| `--dry-run` | Show what would be scanned (sort orders, pages, API calls) without fetching anything |
| `--show-url` | Add a full Audible URL column to the results table |
| `-i, --interactive` | Browse results interactively after the table is shown |
| `--profile NAME` | Load a saved search profile (`find` and `search`) |

### Sort options

`price`, `-price`, `discount`, and `price-per-hour` are calculated client-side after fetching. The rest are server-side sorts provided by the Audible API.

| Sort | Description |
|------|-------------|
| `price` | Cheapest first |
| `-price` | Most expensive first |
| `discount` | Biggest discount percentage first |
| `price-per-hour` | Best value — lowest cost per hour of audio (default for `find`) |
| `value` | Composite score: `(rating × hours) / price` — favors long, highly-rated, cheap books |
| `rating` | Highest rated first |
| `bestsellers` | Audible's bestseller ranking |
| `length` | Longest first |
| `date` | Newest first |
| `title` | Alphabetical by title |
| `relevance` | Audible's relevance ranking (default for `search`) |

## Saved profiles

Save frequently-used search configurations and replay them with `--profile`:

```bash
# Save a profile
deals profile save my-scifi --genre sci-fi --max-price 5 --min-rating 4 --min-hours 8 --first-in-series --deep

# Include skip-owned, language filter, and interactive mode in the profile
deals profile save my-scifi-owned --genre sci-fi --max-price 5 --skip-owned --language english --interactive

# Use it — works on both find and search
deals find --profile my-scifi
deals search "Brandon Sanderson" --profile my-scifi

# CLI flags override profile values
deals find --profile my-scifi --max-price 3

# List and manage
deals profile list
deals profile show my-scifi
deals profile delete my-scifi
```

Profiles support all filter and sort flags, including `--skip-owned`, `--language`, `--interactive`, `--author`, `--series`, `--exclude-author`, `--exclude-narrator`, and `--deep`.

## Last results

`deals last` re-displays results from your most recent `find` or `search` without making any API calls. The table title shows the original query context. You can re-filter and re-sort the cached results:

```bash
# Show the last results (title shows the original query)
deals last

# Re-sort by discount
deals last --sort discount

# Apply new filters
deals last --max-price 3 --min-rating 4.5

# Filter by narrator, author, or series
deals last --narrator "R.C. Bray" --min-ratings 100
deals last --author "Andy Weir"
deals last --series "Bobiverse"
deals last --exclude-author "Sarah J. Maas"

# Filter by language
deals last --language english

# Export the cached results
deals last -o last.csv

# Quick count of cached results (no table)
deals last --count

# Clear the cached results
deals last --clear
```

You can also reference items from the last results by position number in other commands:

```bash
# View details for result #1
deals detail --last 1

# Open result #3 in your browser
deals open --last 3

# Compare results #1 and #2
deals compare --last 1 --last 2

# Range or comma list (compare, wishlist add/remove)
deals compare --last 1-3
deals wishlist add --last 1,3,5 --max-price 5
deals wishlist add --last 1-3,7 --max-price 5

# Mix positional ASINs with --last references
deals compare B00EXAMPLE --last 2
```

Single-item commands (`detail`, `open`, `history`) only accept a single position — a range like `--last 1-3` is rejected with an error. Range width is capped at under 1000 (e.g. `--last 1-1000` errors out).

The cache is updated every time you run `deals find` or `deals search`.

## Global defaults config

Set persistent defaults that apply to all `find` and `search` commands. Useful for things you always want, like skipping owned books or setting a maximum price:

```bash
# Set a global max price
deals config set max-price 5

# Always skip owned books
deals config set skip-owned true

# Set default sort order
deals config set sort discount

# View a specific setting
deals config get max-price

# List all set defaults
deals config list

# Remove a specific default
deals config reset max-price

# Clear all defaults
deals config reset
```

**Precedence:** `CLI flag > profile > global config`. A `--profile` overrides config; an explicit CLI flag overrides both.

You can also set a default locale:

```bash
deals config set locale uk    # always use the UK store
```

## Credit-aware buy advice

If you have an Audible membership, the real question is rarely "is this under $5?" — it's "cash, credit, or wait?" Tell the CLI what a credit costs you and it answers that everywhere:

```bash
deals config set credit-price 11.25
```

Once set:

- Results tables (`find`, `search`, `last`, `series`), `watch`, `detail`, and `compare` gain a **Buy** column/line: **cash** (the sale price beats a credit), **credit** (the cash price exceeds one credit), or **plus** (free with membership).
- `--max-effective-price` filters on the *effective* price — the cheaper of cash and one credit. `deals find --genre sci-fi --max-effective-price 12` keeps a $24.99 book, because to you it costs one $11.25 credit.
- `deals notify` payloads include `verdict` and `effective_price` for each hit, so automations can tell "buy this for cash" from "use a credit".

Unset it (`deals config reset credit-price`) and all of this disappears — output is unchanged for cash-only users.

## Interactive mode

Add `-i` to `find` or `search` to browse results interactively:

```bash
deals find --genre sci-fi --max-price 5 -i
```

After the table displays, you can:
- Type a **number** to view detailed info (e.g. `3`)
- Type **`o 3`** to open that book's Audible page in your browser
- Type **`w 3`** to add it to your wishlist (you'll be prompted for an optional target price)
- Type **`q`** to quit

## Library export

List everything you own on Audible — useful for feeding to other tools, tracking what you have, or analyzing your collection:

```bash
# Show your library (newest first by default)
deals library

# Export as JSON (great for feeding to AI tools or scripts)
deals library --json > my-books.json

# Export as CSV for spreadsheets
deals library -o library.csv

# Top 20 by rating
deals library --sort rating -n 20
```

## Wishlist & price tracking

The CLI maintains a local watchlist with target prices. You add books, set your price, and then `watch` or `notify` tells you when they hit your target. The watchlist persists between sessions in `~/.config/audible-deals/wishlist.json`.

```bash
# Add books manually with a target price
deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5

# Or import everything from your Audible account wishlist at once
deals wishlist sync --max-price 5

# Update the target price for all existing items
deals wishlist sync --max-price 3 --update

# Check current prices — shows BUY for items at/below target
deals watch

# Set up automated alerts (e.g. via cron)
deals notify --webhook https://hooks.slack.com/services/...

# View price history (recorded automatically during find/search)
deals history B00R6S1RCY
```

`wishlist sync` pulls books you've already saved on Audible's website into the local watchlist. This is useful if you've been saving books on Audible and want to start tracking their prices without manually adding each ASIN. Items already tracked locally are skipped — re-running sync is safe. Use `--update` with `--max-price` to bulk-change the target price for items already on the local watchlist.

`watch` shows a status for each item: **BUY** (at or below target), the current discount percentage (on sale but above target), or "waiting" (no discount). By default it checks once and exits. Use `--every` to keep it running:

```bash
# Check once
deals watch

# Re-check every 30 minutes (runs until Ctrl+C)
deals watch --every 30m

# Also accepts hours, seconds, or combinations
deals watch --every 2h
deals watch --every 1h30m

# Only show items at or below target price
deals watch --buy-only

# Sort by title, author, price, asin, or release-date
deals watch --sort price

# Show Audible URLs for each item
deals watch --show-url

# Combine flags
deals watch --buy-only --sort price --show-url
```

For fully automated alerts without a terminal, schedule `notify` on a cron job (see [Recap & notifications](#recap--notifications)).

Price history is recorded automatically every time an ASIN appears in `find` or `search` results — one entry per day, kept for up to 365 days. The `history` command shows a table with relative dates ("3d ago", "1w ago") and a sparkline chart of the price trend. Results tables also show a `vs hist` column (percent above/below each ASIN's historical median price, with `-` for ASINs that don't yet have at least three price points).

You can also open any audiobook's Audible page directly:

```bash
deals open B00R6S1RCY
```

## Recap & notifications

```bash
# See what changed in the last 7 days
deals recap

# Look back further
deals recap --days 30

# Also list wishlist items currently at their all-time low
deals recap --atl
```

The recap shows up to 10 biggest price drops (with book titles when available), a count of newly tracked items, and wishlist items currently at target. Use `--show-new` to include individual newly tracked item details. Use `--atl` to add a section listing wishlist items whose latest tracked price equals their local all-time low.

For automation (cron jobs, CI, monitoring):

```bash
# Print deal alerts as JSON to stdout
deals notify

# Send a generic JSON webhook
deals notify --webhook https://example.com/hook

# Slack incoming webhook (requires --webhook-format slack)
deals notify --webhook https://hooks.slack.com/services/... --webhook-format slack

# Discord, Microsoft Teams, or ntfy.sh
deals notify --webhook https://discord.com/api/webhooks/... --webhook-format discord
deals notify --webhook https://<tenant>.webhook.office.com/webhookb2/... --webhook-format teams
deals notify --webhook https://ntfy.sh/your-topic --webhook-format ntfy
```

`notify` checks your wishlist and outputs items at or below your target price. Without `--webhook` it prints `{"deals": [...], "count": N}` (or `{"deals": [], "count": 0}` when no items hit, so automation consumers can distinguish "no deals" from a crash).

`--webhook-format` shapes the POST body for the destination platform:

| Format | Body shape |
|--------|------------|
| `generic` (default) | `{"deals": [...], "count": N}` JSON — same as the stdout payload |
| `slack` | `{"text": "..."}` with Slack mrkdwn formatting |
| `discord` | `{"content": "..."}` with markdown links (embed previews suppressed) |
| `teams` | Legacy `MessageCard` JSON for Teams incoming webhooks — works on tenants that still accept the deprecated O365 connector format, not on the newer Power Automate / Workflows webhooks (which need Adaptive Cards) |
| `ntfy` | Plain-text body with `Title`, `Tags`, and `Priority` headers |

For any other destination, supply a custom template with `--webhook-template PATH`:

```bash
deals notify --webhook https://example.com/hook \
  --webhook-template ./tmpl.txt
```

The file contains a Python `str.format`-style template that is rendered once per hit and joined with newlines. Available keys: `title`, `price`, `target`, `url`, `currency`, `asin`, `discount_pct`. Use `{{` and `}}` for literal braces. The body is sent as `text/plain; charset=utf-8`. `--webhook-template` requires `--webhook` and cannot be combined with a non-default `--webhook-format` (slack/discord/teams/ntfy).

## Export

| Flag | What it does |
|------|-------------|
| `-o, --output FILE` | Export results to a file (`.json` or `.csv`) — implies `-q` |
| `--json` | Print results as JSON to stdout (for piping) |
| `-q, --quiet` | Suppress the table display |

```bash
# JSON file (-o is short for --output) — table is suppressed automatically
deals find --genre mystery --max-price 5 -o deals.json

# CSV for spreadsheets
deals find --genre mystery --max-price 5 -o deals.csv

# JSON to stdout (for piping)
deals find --genre mystery --max-price 5 --json | jq '.[0]'
```

> **Note:** In CSV exports, multi-value fields (authors, narrators, categories) are joined with `; ` (semicolon-space) to avoid breaking CSV column parsing.

Using `-o` automatically suppresses the table display (same as adding `-q`). This makes scripted exports cleaner — no need to remember to add `-q` when writing to a file.

## Marketplace support

Use `--locale` to switch Audible marketplaces. Prices, currency symbols, URLs, and the `$/hr` column header all automatically adjust to the locale's currency:

```bash
deals --locale uk find --genre fantasy --max-price 3    # shows £/hr, links to audible.co.uk
deals --locale de find --genre thriller                  # shows €/hr, links to audible.de
deals --locale jp find --genre sci-fi                    # shows ¥/hr, links to audible.co.jp
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
# Bash
deals completions bash >> ~/.bashrc

# Zsh
deals completions zsh >> ~/.zshrc

# Fish
deals completions fish > ~/.config/fish/completions/deals.fish
```

After sourcing the completions, `--profile`, `--genre`, and `--exclude-genre` tab-complete from your saved profiles and the built-in genre alias list. `deals profile show <tab>` and `deals profile delete <tab>` also complete profile names.

## Debugging

```bash
# Run with INFO-level logs on stderr
deals -v find --genre sci-fi --max-price 5

# Run with full DEBUG-level logs
deals -vv find --genre sci-fi --max-price 5

# Force DEBUG regardless of -v
DEALS_DEBUG=1 deals find --genre sci-fi

# Also persist DEBUG to a rotating log file (5 MB × 3 backups)
DEALS_LOG_FILE=~/.cache/deals.log deals notify --webhook https://...
```

`DEALS_LOG_FILE` is useful for cron and `watch --every` runs where stderr is discarded — the file handler captures DEBUG records regardless of the `-v` flag. Transient API failures get up to three attempts (two retries) with jittered exponential backoff; each attempt-failure line is logged at WARNING, including the final "giving up" before the exception is re-raised.

## Advanced recipes

These combine flags and commands for power-user workflows.

### Scan subcategories for deeper coverage

```bash
# Cheap books buried beyond the page horizon of "Science Fiction & Fantasy" surface here
deals find --genre "sci-fi" --subcategories --max-price 5
# Combine with --deep for maximum coverage (more API calls)
deals find --genre "sci-fi" --subcategories --deep --max-price 5
```

### Preview a deep scan before running it

```bash
# See how many API calls --deep would make
deals find --genre sci-fi --deep --dry-run
# Output: Sort orders: BestSellers, -ReleaseDate, AvgRating | Pages per sort: 10 | API calls: 30
```

### Find new series by a favorite narrator

```bash
deals find --narrator "R.C. Bray" --max-price 5 --first-in-series --skip-owned
```

Only book 1s, only stuff you don't own, only narrated by that person.

### Find all books in a specific series

```bash
# All Bobiverse books under $5
deals find --genre sci-fi --max-price 5 --series "Bobiverse"

# Search and filter to a series
deals search "Brandon Sanderson" --max-price 10 --series "Cosmere"
```

### Profiles for different moods

```bash
deals profile save long-cheap --max-price 3 --min-hours 15 --sort price-per-hour --deep --skip-owned
deals profile save hidden-gems --max-price 5 --min-rating 4.5 --min-ratings 50 --first-in-series
deals profile save binge-series --max-price 5 --min-hours 20 --sort price-per-hour

# Then just:
deals find --profile long-cheap
deals search "fantasy epic" --profile long-cheap
```

### Always apply your preferences globally

```bash
# Set it once, never type it again
deals config set skip-owned true
deals config set max-price 5
deals config set min-rating 3.5

# Now every find/search uses these defaults
deals find --genre sci-fi
deals search "Stephen King"
```

### Value hunting across keyword variations

```bash
# OR search covers multiple phrasings in one pass, deduped automatically
deals search "WWII | second world war" --max-price 5 --on-sale --skip-owned --sort value

# Re-filter for only the best $/hr deals, with URLs for quick buying
deals last --max-price-per-hour 0.25 --show-url
```

### Explore related genres without overlap

`--exclude-seen` filters out ASINs from searches you've already done. It's useful for browsing adjacent genres back-to-back without seeing the same cross-listed titles twice:

```bash
# Browse sci-fi deals
deals find --genre sci-fi --max-price 5 --skip-owned --sort value

# Now browse fantasy — skip anything that already showed up in sci-fi
deals find --genre fantasy --max-price 5 --skip-owned --exclude-seen
```

The seen list accumulates across runs — each search/find appends its results. Use `deals last --clear-seen` to start fresh.

### Re-examine results without a new API call

```bash
# Run a search, then slice and dice the results without hitting the API again
deals find --genre mystery --max-price 5
deals last --sort discount           # what's the biggest discount?
deals last --min-rating 4.5 -n 5    # top 5 by quality
deals last -o mystery-deals.csv     # export a copy
deals detail --last 1               # full details on result #1
deals compare --last 1 --last 3     # compare two results
```

### Cross-locale price comparison

```bash
deals detail B00R6S1RCY                    # US price
deals --locale uk detail B00R6S1RCY        # UK price
deals --locale de detail B00R6S1RCY        # DE price
```

Same book, different prices across marketplaces. Sometimes one store has a deeper cut.

### Custom filtering with jq

`--json` lets you filter beyond what the built-in flags support (requires [jq](https://jqlang.github.io/jq/)):

```bash
# Books with 1000+ ratings and 80%+ discount, sorted by popularity
deals find --genre sci-fi --max-price 5 --json | \
  jq '[.[] | select(.num_ratings > 1000 and .discount_pct > 80)] | sort_by(-.num_ratings)'

# Just grab ASINs for scripting
deals find --genre thriller --max-price 3 --json | jq -r '.[].asin'
```

### Daily sweep script

```bash
#!/bin/bash
for genre in sci-fi thriller mystery romance; do
  echo "=== $genre ==="
  deals find --genre "$genre" --max-price 3 --skip-owned --deep -n 5 -q -o "deals-${genre}.csv"
done
```

### Automated deal alerts (cron + webhook)

Load up your wishlist, then schedule `notify`:

```bash
# Add books you're watching
deals wishlist add B00R6S1RCY B082FKF7RC --max-price 3

# Cron job — check every morning at 8am, ping Slack when something hits target
# Add to crontab with: crontab -e
0 8 * * * deals notify --webhook https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Without `--webhook`, `notify` prints JSON to stdout — useful for piping into other tools.

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests
pytest tests/ -v

# Run only integration tests
pytest tests/test_integration.py -v
```

## How it works

The tool uses the [audible](https://github.com/mkb79/Audible) Python package to talk to Audible's catalog API. Since the API doesn't support sorting by price, `deals find` fetches multiple pages of results (sorted server-side by bestsellers, release date, etc.), then filters and re-sorts client-side. The `--deep` flag scans with three different sort orders to surface items that might be buried in any single ordering.

Genre matching is flexible — common names and abbreviations like `sci-fi`, `ya`, `bio`, `thriller`, `horror`, `true crime`, `historical fiction`, and `historical` are expanded via aliases, then unmatched queries fall through to substring matching and finally fuzzy matching (via `difflib`) against the full Audible category list. Top-level categories are cached locally for 7 days to avoid redundant API calls.

All data is stored locally in `~/.config/audible-deals/`:
- `auth.json` — Audible auth tokens
- `config.json` — global defaults (set via `deals config set`)
- `wishlist.json` — your watchlist
- `profiles.json` — saved search profiles
- `last_results.json` — cached results from the most recent `find` or `search`
- `history/` — per-ASIN price history (one JSON file per book)
- `categories_cache.*.json` — cached category listings per locale

## Acknowledgements

- [audible](https://github.com/mkb79/Audible) by mkb79 — the Python package that makes Audible API access possible
- [Libation](https://github.com/rmcrackan/Libation) — an excellent open-source Audible library manager whose source code was invaluable as a reference for understanding Audible's undocumented API (response groups, batch patterns, category structures, auth token format). Auth tokens exported from Libation can be imported directly via `deals import-auth`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, coding guidelines, and how to submit a PR. Please read the [Code of Conduct](CODE_OF_CONDUCT.md) before participating.

To report a security vulnerability, see [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)

This project depends on [audible](https://github.com/mkb79/Audible), which is licensed under AGPL-3.0. The pre-built binaries bundle this library — see its license for details.
