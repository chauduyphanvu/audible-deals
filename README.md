# audible-deals

A command-line tool for finding cheap Audible audiobooks. It scans the Audible catalog, filters by price and genre, and helps you snag deals before they disappear.

## What it does

- **Find deals** — scan hundreds of catalog pages and surface audiobooks under your price threshold
- **Search** — keyword search with filters for genre, rating, length, language, and more
- **Deep scan** — hit the catalog from multiple angles (bestsellers, newest, highest-rated) to maximize coverage
- **Compare** — side-by-side comparison of multiple audiobooks, with a $/hr value calculation
- **Wishlist & watch** — save ASINs you're eyeing and check back for price drops
- **Price history** — automatically tracks prices over time so you can spot trends
- **Export** — dump results to JSON or CSV for spreadsheets, scripts, or further analysis

## Quick start

### Install

```bash
# Clone and install (requires Python 3.11+)
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
| `deals compare ASIN ASIN ...` | Side-by-side comparison |
| `deals categories` | List genres (use `--parent ID` to drill in) |
| `deals wishlist add/list/remove` | Manage your watchlist |
| `deals watch` | Check wishlist prices — highlights items at/below target |
| `deals history ASIN` | View recorded price history |
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
| `--min-rating 4.0` | Minimum star rating |
| `--min-hours 5` | Minimum audio length |
| `--on-sale` | Only discounted items |
| `--language english` | Filter by language (default: locale language) |
| `--all-languages` | Include all languages |
| `--first-in-series` | Only show book 1 of each series |
| `--skip-owned` | Exclude books already in your library |
| `-n, --limit 20` | Cap the number of results |
| `--pages 10` | Number of catalog pages to scan (default: 10 for `find`, 3 for `search`) |
| `--deep` | Scan with 3 sort orders for broader coverage — 3x the API calls (`find` only) |

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
| `relevance` | Audible's relevance ranking (default for `search`) |

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

Price history is recorded automatically every time an ASIN appears in `find` or `search` results — one entry per day, kept for up to 365 days.

## Export

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

Use `--locale` to switch Audible marketplaces:

```bash
deals --locale uk find --genre fantasy --max-price 3
deals --locale de find --genre thriller
```

Supported: `us`, `uk`, `ca`, `au`, `in`, `de`, `fr`, `jp`, `es`

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

# Run tests (151 tests, ~0.2s)
pytest tests/ -v

# Run only integration tests
pytest tests/test_integration.py -v
```

## How it works

The tool uses the [audible](https://github.com/mkb79/Audible) Python package to talk to Audible's catalog API. Since the API doesn't support sorting by price, `deals find` fetches multiple pages of results (sorted server-side by bestsellers, release date, etc.), then filters and re-sorts client-side. The `--deep` flag scans with three different sort orders to surface items that might be buried in any single ordering.

Genre matching is fuzzy — common abbreviations like `sci-fi`, `ya`, `bio`, `thriller` are expanded to their full Audible category names. Top-level categories are cached locally for 7 days to avoid redundant API calls.

## License

MIT
