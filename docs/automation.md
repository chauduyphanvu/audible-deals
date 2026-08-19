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

# Export for scripting or backup
deals wishlist list --json | jq -r '.items[].asin'
deals wishlist list -o wishlist.csv
```

`wishlist sync` skips existing local entries, so running it repeatedly is safe. Add `--update` with `--max-price` to change existing targets in bulk.

ASIN commands also accept positions from the last result set through `--last`. Remove an author watch with `deals wishlist remove --author "NAME"`. To remove owned items after previewing them, rerun `deals wishlist purge --owned` and confirm the prompt.

Author watches are checked by `deals notify` and scheduled `deals track` runs. `deals watch` checks explicit ASIN entries only.

The watchlist is stored in `~/.config/audible-deals/wishlist.json`.

## Check prices

`watch` labels each item `BUY` when it reaches its target, shows a discount when it is on sale but above target, and otherwise shows `waiting`.

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

`--every` accepts hours, minutes, seconds, or combinations and continues until you press Ctrl+C.

## Price history

Every ASIN returned by `find`, `search`, `for-me`, or `series` records at most one price per day, retained for up to 365 days.

```bash
deals history B00R6S1RCY
```

The history view includes relative dates and a sparkline. Result tables include a `vs hist` column after an ASIN has at least three price points, and mark an all-time low with a star.

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

- refreshes wishlist items and author watches;
- refreshes recently tracked ASINs, capped at 200 per run;
- records history and sends target-price alerts;
- applies a one-day alert cooldown unless the price falls further; and
- stores the last 10 run summaries in `track_state.json`.

Saved-search monitors run in the same scheduled command. Create either a frozen profile-backed browse monitor or a direct search monitor:

```bash
deals monitor add sci-fi-steals --profile my-scifi
deals monitor add sanderson --query "Brandon Sanderson" --max-price 6
deals monitor list
deals monitor test sci-fi-steals
```

The first successful monitor run is a silent baseline. Later runs alert on new matches and price drops. Monitor definitions and independent snapshots live in `monitors.json` and `monitor_state.json`; enabled monitors rotate through a shared 60 catalog-page-call budget per scheduled run.

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

The Python `str.format`-style template is rendered once per hit and joined with newlines. It can use `title`, `price`, `target`, `url`, `currency`, `asin`, and `discount_pct`. Use `{{` and `}}` for literal braces.

Custom templates are sent as `text/plain; charset=utf-8`. They require `--webhook` and cannot be combined with a non-default webhook format.

## Related guides

- [Command guide](commands.md)
- [Advanced recipes](recipes.md)
