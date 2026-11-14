# Price tracking and automation

audible-deals keeps its watchlist and price history locally. You can check them manually, run a persistent terminal watcher, or install an operating-system schedule.

## Wishlist

Add ASINs manually or import your Audible wishlist:

```bash
# Add books with a target price
deals wishlist add B00R6S1RCY B00I2VWW5U --max-price 5

# Track every matching title by an author
deals wishlist add --author "Brandon Sanderson" --max-price 5

# Import your Audible account wishlist
deals wishlist sync --max-price 5

# Change the target for items already tracked
deals wishlist sync --max-price 3 --update

# Update or clear the target for selected items
deals wishlist update B00R6S1RCY --max-price 3.99
deals wishlist update B00R6S1RCY --clear-target

# Preview removal of wishlist entries you already own
deals wishlist purge --owned --dry-run

# Inspect invalid local entries, then repair with confirmation
deals wishlist repair --dry-run
deals wishlist repair

# Export for scripting or backup
deals wishlist list --json | jq -r '.items[].asin'
deals wishlist list -o wishlist.csv
```

`wishlist sync` skips existing local entries, so running it repeatedly is safe. Add `--update` with `--max-price` to change existing targets in bulk.

An entry without a target alerts in `notify` and `track run` only when you have set a credit price and the book costs less than one credit in the current marketplace; see [Alert on your Audible wishlist](#alert-on-your-audible-wishlist).

ASIN commands also accept `@N` positions, ranges such as `@1-3,5`, Audible product URLs, and the retained `--last` compatibility option. Remove an author watch with `deals wishlist remove --author "NAME"`. To remove owned items after previewing them, rerun `deals wishlist purge --owned` and confirm the prompt.

`wishlist repair` refuses malformed JSON and non-list files. When confirmed, it keeps valid entries in order and saves an owner-only backup beside `wishlist.json` before removing invalid entries. Use `--yes` to skip confirmation in trusted automation.

Author watches are checked by `deals notify` and scheduled `deals track` runs. `deals watch` checks explicit ASIN entries only.

The watchlist is stored in `~/.config/audible-deals/wishlist.json`.

## Check prices

`watch` labels an item without a numeric price `unavailable`. Otherwise it shows `BUY` at the target price, a discount when the item is on sale above its target, or `waiting`. Items with enough history also show their [price grade](#price-grades) under the price.

```bash
# Check once
deals watch

# Keep checking every 30 minutes
deals watch --every 30m

# Durations can combine units
deals watch --every 1h30m

# Show only target hits, sorted by price, with Audible links
deals watch --buy-only --sort price --show-url

# Return exit code 0 for a hit and 1 for no hits
deals watch --exit-code
```

`--every` accepts hours, minutes, seconds, or combinations and continues until you press Ctrl+C. `--buy-only` hides unavailable rows, but the summary still reports their count. Only `BUY` rows contribute to the hit count and `--exit-code` result.

## Price history

Flat discovery with `find`, `search`, `for-me`, or `series` caches its complete candidate pool for `last` and records every finite-price book in that pool, whether or not a filter or the result limit hides it. Every price the scan already paid an API call for becomes history, at no extra API cost. Refresh eligibility is narrower: only finite-price rows that were shown receive it. `series --gaps` has no flat-view limit and records every finite-price gap that survives its filters. Terminal, `--quiet`, `--json`, and successful export output all count as shown, including quiet gaps; `--count` and `--dry-run` record neither history nor refresh eligibility. Each ASIN records at most one price per observation date; dated backfills remain chronological, idempotent, and capped at 365 entries.

```bash
deals history B00R6S1RCY
```

The history view includes relative dates, a sparkline, and a verdict on the latest price. Result tables, `watch`, and `detail` grade the price on screen the same way. `detail` also records the price it fetches, so looking a book up adds to its history. When `last` first surfaces a cached finite-price product, it backfills the session's whole finite-price candidate pool at the original session date, which changes nothing for books already recorded, and marks refresh eligibility on the date shown for the newly shown products only. Unavailable products create neither record.

### Price grades

Audible's discount badge is measured against a list price few members pay, and member prices can differ between accounts. A grade instead compares a price with the prices this tool has recorded for the same book, on your account, in the same marketplace.

| Grade | What it means |
|-------|---------------|
| `lowest` | At or below every earlier price, and at least 10% under the usual price. |
| `good` | At least 10% under the usual price, and closer to the lowest price than to the usual one. |
| `typical` | Close to the usual price, or under it but nearer the usual price than the lowest. |
| `high` | At least 10% above the usual price. It has been cheaper; the history view says how cheap and how often. |
| `new` | Not enough history to judge yet. Tables show `-`. |

A grade needs at least five earlier checks, and the first of them at least 14 days before the price being graded. Fewer checks, or checks bunched inside two weeks, cannot tell a sale from the normal price. Checks from the same day as the price are never counted, so seeing a book twice in one day does not change its grade. `last` grades cached prices against the history before the session date.

The usual price is the middle price over time rather than over checks: each check stands for the days until the next one, up to 14 days. Checking a book daily during a one-week sale therefore does not make the sale price look normal. A price that has never changed is `typical`, however long the history.

Under the sparkline, `deals history` and interactive `h @N` print a verdict that names how many earlier checks it rests on:

```text
  GOOD: second-lowest price in 102 earlier checks since 12 May
  Lowest $7.49 on 1 Aug · usual price $24.50 · at or below $9.99 twice before
```

For a `high` price, the second line says what to wait for, for example `It was $7.49 on 1 Aug; it has dropped to $12.99 or less 3 times.` The `detail` panel shows the first line. Each grade only knows the checks this tool has made, so a book seen rarely can miss sales that happened between checks; [background tracking](#background-tracking) fills those gaps for wishlist items and recent results.

## Background tracking

Install a recurring OS-level schedule instead of keeping a terminal open:

```bash
# Refresh every six hours
deals track install

# Refresh every three hours and send target hits to ntfy
deals track install --every 3h \
  --webhook https://ntfy.sh/mytopic \
  --webhook-format ntfy

# Save an authentication header for a self-hosted receiver
deals track install \
  --webhook https://hooks.example.com/x \
  --webhook-header 'Authorization: Bearer TOKEN'

# Inspect the schedule and recent runs
deals track status
deals track status --history
deals track log

# Remove the schedule
deals track uninstall
```

The installer uses launchd on macOS, a systemd user timer or cron on Linux, and Task Scheduler on Windows. The minimum interval is 10 minutes.

Each run:

- refreshes every wishlist item in its own marketplace, and every author watch, regardless of recent-result eligibility;
- checks your Audible wishlist when `audible-wishlist` is on;
- refreshes up to 200 eligible recent-result ASINs for the current marketplace;
- records history, including every priced book a monitor scans, and sends target-price and below-credit alerts;
- applies a one-day alert cooldown unless the price falls further; and
- stores the last 10 run summaries in `track_state.json`.

Finite-price products become refresh-eligible only when presented; a book recorded from a scan but never shown is not refreshed in the background. Eligibility is marketplace-specific and lasts through 30 days after presentation. On first use, the store migrates only ASINs in both the legacy seen list and valid numeric history, using each marketplace's latest history date.

Extra refreshes exclude wishlist and globally dismissed ASINs. Candidates are ordered by oldest presentation date and ASIN, then selected with a stable, wrapping cursor so changes cannot starve later entries. Wishlist entries remain active even when dismissed or older than 30 days. Background refreshes record history without extending eligibility.

Saved-search monitors run in the same scheduled command. Create either a frozen profile-backed browse monitor or a direct search monitor:

```bash
deals monitor add sci-fi-steals --profile my-scifi
deals monitor add sanderson --query "Brandon Sanderson" --max-price 6
deals monitor list
deals monitor test sci-fi-steals
```

The first successful monitor run is a silent baseline. Later runs alert on new matches and price drops. Each run also records the price of every priced book its scan returned, matched or not, in the monitor's marketplace; `deals monitor test` records nothing. Monitor definitions and independent snapshots live in `monitors.json` and `monitor_state.json`; enabled monitors rotate through a shared 60 catalog-page-call budget per scheduled run.

Runs share a cross-process lock with `notify`, so overlapping schedules exit cleanly. Authentication failures are recorded and can trigger a one-time reauthentication alert. `deals doctor` reports three or more consecutive tracking failures.

## Recap

```bash
# Summarize the last seven days
deals recap

# Use a longer window
deals recap --days 30

# Include wishlist titles at their all-time low
deals recap --atl

# Include every tracked title at its all-time low
deals recap --atl-all

# Produce machine-readable output
deals recap --json
```

The recap shows up to 10 of the largest price drops, the number of newly tracked items, and wishlist items currently at target. Add `--show-new` to list the new items individually. Recaps can also use the same `--webhook`, `--webhook-format`, and `--webhook-header` controls as notifications.

## Notifications

Without a webhook, `notify` writes JSON to standard output:

```bash
deals notify

# Return exit code 0 for a hit and 1 for no hits
deals notify --exit-code

# Suppress repeat alerts for seven days unless the price falls again
deals notify --cooldown 7
```

It prints `{"deals": [...], "count": N}`. An empty result still prints a valid payload with a count of zero, allowing automation to distinguish “no deals” from a failure.

### Alert on your Audible wishlist

You can be told when a book on your Audible account wishlist costs less than one of your credits, without copying the list into `deals wishlist` or setting a target for each book. Set what you pay for a credit, then switch the check on:

```bash
deals config set credit-price 11.25
deals config set audible-wishlist true
```

`deals notify` and scheduled `deals track run` then read your Audible wishlist in the current marketplace, one request per 50 books. A book alerts when its cash price is below your credit price, and the alert says so, for example “below your $11.25 credit”. A book at or above the credit price does not alert. Plus catalog titles and titles you dismissed are skipped. A book that also has a local wishlist entry follows that entry's target and marketplace instead. Local entries saved without a target use the same credit-price rule in the current marketplace, whether or not `audible-wishlist` is on.

A book that stays below your credit price alerts once, then again only if its price falls further, or after it rises above the credit price and drops back. `track run` always works this way; `notify` suppresses repeats only when you pass `--cooldown`. One message lists at most 20 below-credit books, largest saving first, followed by a line such as “and 37 more below your credit price”. Target-price hits are always listed in full.

In JSON output and the `generic` webhook, a below-credit hit has `"reason": "below_credit_price"` and its `target` is your credit price. A message that hit the cap also has `"more_below_credit_price": 37`; `count` stays the number of listed deals.

If the credit price is removed later, runs print a warning and skip your Audible wishlist. If the Audible wishlist cannot be fetched, the local wishlist is still checked and the failure appears in the run summary or as a warning. Turn the check off with `deals config reset audible-wishlist`.

### Webhook formats

```bash
# Generic JSON
deals notify --webhook https://example.com/hook

# Slack
deals notify \
  --webhook https://hooks.slack.com/services/... \
  --webhook-format slack

# Discord
deals notify \
  --webhook https://discord.com/api/webhooks/... \
  --webhook-format discord

# Microsoft Teams
deals notify \
  --webhook https://example.webhook.office.com/... \
  --webhook-format teams

# ntfy
deals notify \
  --webhook https://ntfy.sh/your-topic \
  --webhook-format ntfy
```

| Format | Request body |
|--------|--------------|
| `generic` | The same JSON object printed to standard output |
| `slack` | A `text` field with Slack mrkdwn |
| `discord` | A `content` field with Markdown links |
| `teams` | A legacy MessageCard for tenants that still support O365 connectors |
| `ntfy` | Plain text with `Title`, `Tags`, and `Priority` headers |

Teams Workflows webhooks require Adaptive Cards and are not supported by the legacy `teams` format.

### Authentication headers

Add repeatable headers for services such as Home Assistant, n8n, or a private endpoint:

```bash
deals notify \
  --webhook https://hooks.example.com/x \
  --webhook-header 'Authorization: Bearer TOKEN'
```

`Content-Type` cannot be overridden. Deliveries make up to three attempts with a short backoff.

### Custom templates

```bash
deals notify \
  --webhook https://example.com/hook \
  --webhook-template ./tmpl.txt
```

The Python `str.format`-style template is rendered once per hit and joined with newlines. It can use `title`, `price`, `target`, `url`, `currency`, `asin`, and `discount_pct`. Use `{{` and `}}` for literal braces. For a below-credit hit, `target` is your credit price, and a message over the 20-book cap ends with its “and N more” line.

Custom templates are sent as `text/plain; charset=utf-8`. They require `--webhook` and cannot be combined with a non-default webhook format.

## Related guides

- [Command guide](commands.md)
- [Advanced recipes](recipes.md)
