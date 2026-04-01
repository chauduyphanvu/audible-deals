# audible-deals

A command-line tool for finding cheap Audible audiobooks. It scans the Audible catalog, filters by price and genre, and helps you snag deals before they disappear.

## What it does

- **Find deals** — scan hundreds of catalog pages and surface audiobooks under your price threshold
- **Search** — keyword search with filters for genre, rating, length, narrator, language, and more
- **Deep scan** — hit the catalog from multiple angles (bestsellers, newest, highest-rated) to maximize coverage
- **Compare** — side-by-side comparison of multiple audiobooks, with a $/hr value calculation
- **Interactive mode** — browse results, view details, open in browser, or add to wishlist without leaving the CLI
- **Wishlist & watch** — save ASINs you're eyeing and check back for price drops
- **Price history** — automatically tracks prices over time with sparkline charts and relative dates
- **Saved profiles** — save your favorite search configurations and reuse them with `--profile`
- **Recap & notify** — get a summary of recent price drops, or send webhook notifications for deals at target
- **Locale support** — correct currency symbols and Audible URLs for all 9 marketplaces
- **Export** — dump results to JSON or CSV for spreadsheets, scripts, or further analysis

## Quick start

### Install

**One-liner (macOS / Linux):**

```bash
curl -fsSL https://raw.githubusercontent.com/chauduyphanvu/audible-deals/main/install.sh | bash
```

This detects your OS and architecture, downloads the right binary, and installs it to `~/.local/bin`.

**Manual download:**

Grab the binary for your platform from [Releases](https://github.com/chauduyphanvu/audible-deals/releases/latest):

| Platform | File |
|----------|------|
| macOS (Apple Silicon) | `deals-macos-arm64` |
| Linux (x64) | `deals-linux-x64` |
| Windows (x64) | `deals-windows-x64.exe` |

```bash
# Example: macOS Apple Silicon
chmod +x deals-macos-arm64
sudo mv deals-macos-arm64 /usr/local/bin/deals
```

**From source (requires Python 3.11+):**

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e .
```

### Authenticate

You need an Audible account. The easiest method on macOS:

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

# Cheap long listens (great $/hr value)
deals find --sort price-per-hour --min-hours 10 --max-price 5

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
```

## Commands

| Command | Description |
|---------|-------------|
| `deals find` | Browse and filter deals (the main command) |
| `deals search QUERY` | Search by keyword with filters |
| `deals detail ASIN` | Detailed info for a single audiobook |
| `deals open ASIN` | Open the Audible page in your browser |
| `deals compare ASIN ASIN ...` | Side-by-side comparison |
| `deals categories` | List genres (use `--parent ID` to drill in) |
| `deals wishlist add/list/remove` | Manage your watchlist |
| `deals watch` | Check wishlist prices — highlights items at/below target |
| `deals notify` | Send deal notifications via webhook or JSON stdout |
| `deals profile save/list/delete` | Manage saved search profiles |
| `deals history ASIN` | View price history with sparkline chart |
| `deals recap` | Summary of recent price drops across tracked items |
| `deals login` | Authenticate with Audible |
| `deals import-auth PATH` | Import auth from audible-cli or Libation |
| `deals completions SHELL` | Generate shell completions (bash/zsh/fish) |

## Filtering & sorting

### Filters

| Flag | What it does |
|------|-------------|
| `--max-price 5.00` | Only items under this price (default: $5 for `find`, no default for `search`) |
| `--genre sci-fi` | Fuzzy genre match — `sci-fi`, `sf`, `scifi` all work |
| `--category ID` | Filter by category ID (alternative to `--genre` — use `deals categories` to find IDs) |
| `--exclude-genre erotica` | Remove genres from results (repeatable) |
| `--keywords "space opera"` | Keyword filter within a category browse (`find` only) |
| `--narrator "Reynolds"` | Filter by narrator name (case-insensitive substring match) |
| `--min-rating 4.0` | Minimum star rating |
| `--min-ratings 100` | Minimum number of ratings — filters out unreviewed books |
| `--min-hours 5` | Minimum audio length |
| `--on-sale` | Only discounted items |
| `--language english` | Filter by language (default: locale language) |
| `--all-languages` | Include all languages |
| `--first-in-series` | Only show book 1 of each series |
| `--skip-owned` | Exclude books already in your library |
| `-n, --limit 20` | Cap the number of results |
| `--pages 10` | Number of catalog pages to scan (default: 10 for `find`, 3 for `search`) |
| `--deep` | Scan with 3 sort orders for broader coverage — 3x the API calls (`find` only) |
| `-i, --interactive` | Browse results interactively after the table is shown |
| `--profile NAME` | Load a saved search profile (`find` only — see note below) |

### Sort options

`price`, `-price`, `discount`, and `price-per-hour` are calculated client-side after fetching. The rest are server-side sorts provided by the Audible API.

| Sort | Description |
|------|-------------|
| `price` | Cheapest first (default for `find`) |
| `-price` | Most expensive first |
| `discount` | Biggest discount percentage first |
| `price-per-hour` | Best value — lowest cost per hour of audio |
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

# Use it
deals find --profile my-scifi

# CLI flags override profile values
deals find --profile my-scifi --max-price 3

# List and manage
deals profile list
deals profile delete my-scifi
```

Profiles support most filter and sort flags. Flags like `--skip-owned`, `--language`, and `--interactive` are session-specific and cannot be saved to a profile.

## Interactive mode

Add `-i` to `find` or `search` to browse results interactively:

```bash
deals find --genre sci-fi --max-price 5 -i
```

After the table displays, you can:
- Type a **number** to view detailed info (e.g. `3`)
- Type **`o 3`** to open that book's Audible page in your browser
- Type **`w 3`** to add it to your wishlist
- Type **`q`** to quit

## Wishlist & price tracking

```bash
# Add books to your wishlist with a target price
deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5

# Check current prices against your targets
deals watch

# View price history (recorded automatically during find/search)
deals history B00R6S1RCY
```

The `watch` command shows a status for each item: **BUY** (at or below your target price), the current discount percentage (if it's on sale but still above target), or "waiting" (no discount detected).

Price history is recorded automatically every time an ASIN appears in `find` or `search` results — one entry per day, kept for up to 365 days. The `history` command shows a table with relative dates ("3d ago", "1w ago") and a sparkline chart of the price trend.

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
```

The recap shows up to 10 biggest price drops, newly tracked items, and wishlist items currently at target.

For automation (cron jobs, CI, monitoring):

```bash
# Print deal alerts as JSON to stdout
deals notify

# Send to a Slack/Discord/generic webhook
deals notify --webhook https://hooks.slack.com/services/...
```

`notify` checks your wishlist and only outputs items that are at or below your target price.

## Export

| Flag | What it does |
|------|-------------|
| `-o, --output FILE` | Export results to a file (`.json` or `.csv`) |
| `--json` | Print results as JSON to stdout (for piping) |
| `-q, --quiet` | Suppress the table display |

```bash
# JSON file (-o is short for --output)
deals find --genre mystery --max-price 5 -o deals.json

# CSV for spreadsheets
deals find --genre mystery --max-price 5 -o deals.csv

# JSON to stdout (for piping)
deals find --genre mystery --max-price 5 --json | jq '.[0]'

# Export without the table display (-q is short for --quiet)
deals find --genre mystery --max-price 5 -o deals.json -q
```

## Marketplace support

Use `--locale` to switch Audible marketplaces. Prices, currency symbols, and URLs automatically adjust:

```bash
deals --locale uk find --genre fantasy --max-price 3    # shows £, links to audible.co.uk
deals --locale de find --genre thriller                  # shows €, links to audible.de
deals --locale jp find --genre sci-fi                    # shows ¥, links to audible.co.jp
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

## Development

```bash
# Install with dev dependencies
pip install -e ".[dev]"

# Run tests (178 tests, ~0.2s)
pytest tests/ -v

# Run only integration tests
pytest tests/test_integration.py -v
```

## How it works

The tool uses the [audible](https://github.com/mkb79/Audible) Python package to talk to Audible's catalog API. Since the API doesn't support sorting by price, `deals find` fetches multiple pages of results (sorted server-side by bestsellers, release date, etc.), then filters and re-sorts client-side. The `--deep` flag scans with three different sort orders to surface items that might be buried in any single ordering.

Genre matching is flexible — common abbreviations like `sci-fi`, `ya`, `bio`, `thriller` are expanded via aliases, then unmatched queries fall through to substring matching and finally fuzzy matching (via `difflib`) against the full Audible category list. Top-level categories are cached locally for 7 days to avoid redundant API calls.

All data is stored locally in `~/.config/audible-deals/`:
- `auth.json` — Audible auth tokens
- `wishlist.json` — your watchlist
- `profiles.json` — saved search profiles
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
