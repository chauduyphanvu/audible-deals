# Advanced recipes

These workflows combine commands and filters for broader searches, repeatable preferences, and external automation.

## Scan subcategories

Broad categories can hide books beyond the catalog page horizon. Scan every resolved subcategory separately to improve coverage:

```bash
deals find --genre sci-fi --subcategories --max-price 5
```

Combine this with `--deep` for maximum coverage at the cost of more API calls:

```bash
deals find --genre sci-fi --subcategories --deep --max-price 5
```

## Preview an expensive scan

`--dry-run` reports the sort orders, pages, and API-call count without fetching catalog data:

```bash
deals find --genre sci-fi --deep --dry-run
```

For the defaults, a deep scan reports three sort orders, 10 pages per sort, and 30 API calls.

## Find a new series by narrator

```bash
deals find \
  --narrator "R.C. Bray" \
  --first-in-series \
  --skip-owned \
  --max-price 5
```

## Find books in a series

```bash
deals find --genre sci-fi --series "Bobiverse" --max-price 5
deals search "Brandon Sanderson" --series "Cosmere" --max-price 10
```

## Save profiles for different moods

```bash
deals profile save long-cheap \
  --max-price 3 --min-hours 15 --sort price-per-hour --deep --skip-owned

deals profile save hidden-gems \
  --max-price 5 --min-rating 4.5 --min-ratings 50 --first-in-series

deals profile save binge-series \
  --max-price 5 --min-hours 20 --sort price-per-hour
```

Then reuse a profile with either catalog entry point:

```bash
deals find --profile long-cheap
deals search "fantasy epic" --profile long-cheap
```

## Apply preferences globally

```bash
deals config set skip-owned true
deals config set max-price 5
deals config set min-rating 3.5
```

Every later `find` or `search` uses those defaults. A profile overrides the global configuration, and an explicit CLI option overrides both.

## Search several phrasings

Use `|` for an OR search. Results are deduplicated automatically:

```bash
deals search "WWII | second world war" \
  --max-price 5 --on-sale --skip-owned --sort value

deals last --max-price-per-hour 0.25 --show-url
```

## Browse adjacent genres without repeats

`--exclude-seen` removes ASINs returned by earlier searches:

```bash
deals find --genre sci-fi --max-price 5 --skip-owned --sort value
deals find --genre fantasy --max-price 5 --skip-owned --exclude-seen
```

The seen list accumulates across runs. Clear it with:

```bash
deals last --clear-seen
```

## Rework results without another API call

```bash
deals find --genre mystery --max-price 5
deals last --sort discount
deals last --min-rating 4.5 -n 5
deals last -o mystery-deals.csv
deals detail --last 1
deals compare --last 1 --last 3
```

## Hunt for historically low prices

```bash
# Keep titles in the cheapest quartile of their tracked history
deals find --genre sci-fi --hist-below 25 --require-history

# Require a 30% drop and limit results to recent releases
deals find --min-price-drop 30 --released-after 2024-01-01
```

Without `--require-history`, titles without enough data pass through so new catalog discoveries are not hidden.

## Fill gaps in series you own

```bash
# Group missing books by series
deals series --gaps

# Focus on series where you own at least three books
deals series --min-books 3 --max-price 10 --sort discount
```

## Compare marketplaces

```bash
deals detail B00R6S1RCY
deals --locale uk detail B00R6S1RCY
deals --locale de detail B00R6S1RCY
```

Prices can differ between Audible stores. Authentication and availability rules still apply per marketplace.

## Filter JSON with jq

Use `--json` for criteria not built into the CLI:

```bash
deals find --genre sci-fi --max-price 5 --json | \
  jq '[.[] | select(.num_ratings > 1000 and .discount_pct > 80)] | sort_by(-.num_ratings)'

deals find --genre thriller --max-price 3 --json | jq -r '.[].asin'
```

## Run a daily genre sweep

```bash
#!/bin/bash
for genre in sci-fi thriller mystery romance; do
  echo "=== $genre ==="
  deals find \
    --genre "$genre" --max-price 3 --skip-owned --deep \
    -n 5 -q -o "deals-${genre}.csv"
done
```

## Send scheduled alerts with cron

The built-in [`track` scheduler](automation.md#background-tracking) is the simplest option. If you prefer cron:

```bash
deals wishlist add B00R6S1RCY B082FKF7RC --max-price 3
```

Open the crontab with `crontab -e`, then add:

```cron
0 8 * * * deals notify --webhook https://hooks.slack.com/services/XXX/YYY/ZZZ
```

Without `--webhook`, `notify` prints JSON for piping into another tool.

## Related guides

- [Command guide](commands.md)
- [Price tracking and automation](automation.md)
