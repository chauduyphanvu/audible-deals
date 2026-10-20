# Price tracking and automation

audible-deals keeps its watchlist and price history locally. You can check them manually, keep a terminal loop running, or install an operating-system schedule.

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

ASIN commands also accept `@N` positions, ranges such as `@1-3,5`, Audible product URLs, and the retained `--last` compatibility option. Remove an author watch with `deals wishlist remove --author "NAME"`. To remove owned items after previewing them, rerun `deals wishlist purge --owned` and confirm the prompt.

`wishlist repair` refuses malformed JSON and non-list files. When confirmed, it keeps valid entries in order and saves an owner-only backup beside `wishlist.json` before removing invalid entries. Use `--yes` to skip confirmation in trusted automation.

Author watches are checked by `deals notify` and scheduled `deals track` runs. `deals watch` checks explicit ASIN entries only.

The watchlist is stored in `~/.config/audible-deals/wishlist.json`.

## Check prices

`watch` labels an item without a numeric price `unavailable`. Otherwise it shows `BUY` at the target price, a discount when the item is on sale above its target, or `waiting`.

```bash
# Check once
deals watch

# Show only target hits, sorted by price, with Audible links
deals watch --buy-only --sort price --show-url

# Exit 0 for a hit and 10 for no hit (see "Exit codes and errors" in the command guide)
deals watch --exit-code
```

`watch --every` (re-check on an interval until Ctrl+C) is deprecated; use [`deals track run --every`](#running-without-a-scheduler), which does the same and more. Until it is removed `watch --every` still works, and now takes the same run lock and backs off after failures. `--buy-only` hides unavailable rows, but the summary still reports their count. Only `BUY` rows contribute to the hit count and `--exit-code` result.

## Price history

Flat discovery with `find`, `search`, `for-me`, or `series` caches its complete candidate pool for `last`, but records only finite-price rows surfaced after applying the result limit. `series --gaps` has no flat-view limit and records every finite-price gap that survives its filters. Terminal, `--quiet`, `--json`, and successful export output all count as surfaced, including quiet gaps; `--count` and `--dry-run` record neither history nor refresh eligibility. Prices that came from the catalog cache (see [Cached results](commands.md#cached-results)) are never recorded, and `track run`, `notify`, `watch` and monitors always read live prices. Each ASIN records at most one price per observation date; dated backfills remain chronological, idempotent, and capped at 365 entries.

```bash
deals history B00R6S1RCY
```

The history view includes relative dates and a sparkline. Result tables include a `vs hist` column after an ASIN has at least three price points, and mark an all-time low with a star. When `last` first surfaces a cached finite-price product, it backfills history at the original session date and marks refresh eligibility on the date shown. Unavailable products create neither record.

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

The installer uses launchd on macOS, a systemd user timer or cron on Linux, and Task Scheduler on Windows. The minimum interval is 10 minutes. The job is pinned to the data directory in use when you install (it sets `DEALS_HOME` for the job), so it reads and writes the same data you see interactively even if `XDG_CONFIG_HOME` is only set in your shell; `deals track install` prints that directory. Re-run it after moving your data.

On Linux without a systemd user session (a container, WSL without systemd, an ssh session whose login did not start one) the timer cannot be installed. `deals track install` then removes whatever it wrote and, if `crontab` exists, installs the crontab entry instead and says so (`Note: ... a crontab entry was installed instead`). Without cron it stops with nothing installed and suggests `deals track run --every 6h` (with your interval), which keeps tracking in the foreground.

### Running without a scheduler

On a machine that is always on, or where you would rather not install a schedule, run the same job in the foreground:

```bash
deals track run --every 30m
```

It does exactly what a scheduled `deals track run` does (wishlist and author watches, extra tracked titles, monitors, alerts, run history), once per interval (at least 10 minutes), until you press Ctrl+C or send SIGTERM (for a service manager). It uses the same run lock as scheduled runs, `notify` and `recap`, so a loop and a scheduler never work at the same time: a run that finds the lock held is skipped with a note, and the next one happens a normal interval later. If a run fails (Audible or the webhook is down, the login expired), it is reported on one line and recorded in the run history, and the next attempt waits twice as long each time, with some random jitter, up to an hour, until a run succeeds. The config file is read again before every run, so changes apply without restarting it. Stopping it with Ctrl+C exits with status 0.

Everything a scheduled run prints goes to `track.log` in the data directory (`deals track log` shows the end of it, reading only the last part of the file). When a run starts and the log is larger than 1 MB, the older content moves to `track.log.1` (and `.1` to `.2`, `.2` to `.3`; the oldest is dropped) and `track.log` starts again, so the log stays small without a log-rotation setup.

Each run:

- refreshes every wishlist item and author watch regardless of recent-result eligibility;
- refreshes up to 200 eligible recent-result ASINs for the current marketplace;
- records history and sends target-price alerts;
- applies a one-day alert cooldown unless the price falls further; and
- stores the last 10 run summaries in `state.db`.

Finite-price products become refresh-eligible only when presented. Eligibility is marketplace-specific and lasts through 30 days after presentation. When upgrading from the JSON files of earlier releases, the import derives the store only for ASINs in both the old seen list and valid numeric history, using each marketplace's latest history date (see [Upgrading from JSON files](commands.md#upgrading-from-json-files)).

Extra refreshes exclude wishlist and globally dismissed ASINs. Candidates are ordered by oldest presentation date and ASIN, then selected with a stable, wrapping cursor so changes cannot starve later entries. Wishlist entries remain active even when dismissed or older than 30 days. Background refreshes record history without extending eligibility.

Saved-search monitors run in the same scheduled command. Create either a frozen profile-backed browse monitor or a direct search monitor:

```bash
deals monitor add sci-fi-steals --profile my-scifi
deals monitor add sanderson --query "Brandon Sanderson" --max-price 6
deals monitor list
deals monitor test sci-fi-steals
```

The first successful monitor run is a silent baseline. Later runs alert on new matches and price drops. Monitor definitions live in `monitors.json` and each monitor's independent snapshot in `state.db`; enabled monitors rotate through a shared 60 catalog-page-call budget per scheduled run.

Runs share a cross-process lock with `notify`, so overlapping schedules exit cleanly. A run that fails, including one whose webhook could not be delivered, is recorded with its error and shown by `deals track status`; the alert is sent again by the next run. Authentication failures are recorded and can trigger a one-time reauthentication alert. `deals doctor` reports three or more consecutive tracking failures, and warns when the installed schedule runs a program that no longer exists (for example after the tool was moved or reinstalled elsewhere); re-run `deals track install` to fix it. Schedule files are written atomically.

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

# Exit 0 for a hit and 10 for no hit; a busy run lock exits 0 with a note
deals notify --exit-code

# Suppress repeat alerts for seven days unless the price falls again
deals notify --cooldown 7

# Send to a webhook and also print the JSON
deals notify --webhook https://ntfy.sh/mytopic --json
```

It prints `{"deals": [...], "count": N}` (see [JSON output](json.md#notify)). Standard output is always valid JSON, even for an empty wishlist or a busy run lock, where it prints a count of zero and the explanation goes to standard error, so automation can tell “no deals” from a failure by the exit status.

`--cooldown` differs on purpose between the two automation commands. `deals notify` is a check you run yourself, so it reports every deal at target unless you pass `--cooldown N` (0 means no cooldown, the default). `deals track run` runs unattended every few hours and would repeat the same alert, so it defaults to a one-day cooldown (`--cooldown 0` turns it off). Both suppress an alert for N days unless the price falls further.

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

# Microsoft Teams (Power Automate Workflows webhook)
deals notify \
  --webhook https://prod-00.westus.logic.azure.com:443/workflows/... \
  --webhook-format teams

# Microsoft Teams, old Office 365 connector
deals notify \
  --webhook https://example.webhook.office.com/... \
  --webhook-format teams-legacy

# ntfy
deals notify \
  --webhook https://ntfy.sh/your-topic \
  --webhook-format ntfy
```

| Format | Request body |
|--------|--------------|
| `generic` | The same JSON object printed to standard output |
| `slack` | A `text` field with Slack mrkdwn; `&`, `<`, `>` in titles are escaped and a `\|` in a title is replaced so it cannot end the link early |
| `discord` | A `content` field with Markdown links (titles are escaped) and `allowed_mentions` set so a title can never ping `@everyone` or a user |
| `teams` | An Adaptive Card message for **Power Automate Workflows** webhooks. A URL on `*.webhook.office.com` (or an `outlook.office.com/webhook/` URL) is a retired Office 365 connector and automatically gets a MessageCard instead |
| `teams-legacy` | A MessageCard, always (for connector webhooks that still work in your tenant) |
| `ntfy` | Plain text with `Title`, `Tags`, and `Priority` headers (a non-ASCII title is sent as an RFC 2047 encoded word, which ntfy decodes) |

Microsoft retired the Office 365 connectors that took MessageCards; the replacement is a Workflows webhook ("When a Teams webhook request is received"), which expects an Adaptive Card. `--webhook-format teams` now sends that. Payload shapes for every format are pinned by the captured requests in `tests/fixtures/webhooks`.

### Known limits

Webhook services cap the size of one message, and `deals` does not split or shorten a payload: a notification with a very long list of hits can be rejected by the service.

- **Discord** accepts at most 2000 characters in a message, which is roughly 15 to 20 hits.
- **ntfy** accepts a message body of up to 4096 bytes.
- **Slack** (about 40,000 characters) and **Teams** (about 28 KB per card) are far less likely to be reached.

A rejected delivery is not lost: the run is recorded as failed (`deals track status` shows the error), the alert is not marked as sent, and the next run tries again. If you routinely have this many titles at target, tighten the targets, use `--cooldown` so each title is reported once, or use the `generic` format and let your own receiver do the splitting.

### Authentication headers

Add repeatable headers for services such as Home Assistant, n8n, or a private endpoint:

```bash
deals notify \
  --webhook https://hooks.example.com/x \
  --webhook-header 'Authorization: Bearer TOKEN'
```

`Content-Type` cannot be overridden. Deliveries make up to three attempts with a short backoff.

### Where a webhook may point

A webhook host must resolve to public internet addresses. The check runs when you enter the URL (`--webhook`, `deals config set webhook`) and again **when the webhook is sent**, on the address the connection actually uses: `deals` resolves the name once, refuses a non-public address (loopback, private ranges, link-local and cloud metadata addresses, carrier-grade NAT), and connects to the address it checked, so a name that resolves to a public address when you set it up but to `127.0.0.1` later (DNS rebinding) is refused. TLS still verifies the certificate against the host name. Redirects are never followed.

To send to a server on your own network, such as a self-hosted ntfy or Home Assistant, opt in:

```bash
deals config set webhook-allow-private true
deals notify --webhook http://192.168.1.20:8080/deals --webhook-format ntfy
```

With the setting on, any address is allowed for every webhook, including the monitor webhooks you configure, so only use URLs you trust. Turn it off again with `deals config reset webhook-allow-private`.

If an HTTP proxy is configured for the URL (`HTTPS_PROXY` and friends), the proxy looks the name up and opens the connection, so `deals` can neither resolve the name nor pin the address. What it can still do is read the URL: a webhook that names a private place outright (an address such as `10.0.0.5` or `[::1]`, `localhost` or `*.localhost`) is refused before the request reaches the proxy, unless `webhook-allow-private` is on. **A public-looking name that the proxy resolves to a private address is not caught by `deals`**; the proxy's own policy decides. Hosts that `NO_PROXY` exempts are connected to directly and get the full resolve-and-pin check above. Entering a URL (`--webhook`, `deals config set webhook`) follows the same rule, so a host that only the proxy can resolve can be saved; without a proxy the name must resolve on this machine.

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
