# Find your next Audible deal

audible-deals searches the Audible catalog, filters books by price and taste, and tracks the titles you are waiting to buy.

## What you can do

- Find inexpensive audiobooks by genre, price, rating, length, narrator, author, or series.
- Rank personalized recommendations from your Audible library.
- Compare cash prices with the cost of an Audible credit.
- Track wishlist prices and send alerts to Slack, Discord, Teams, ntfy, or a generic webhook.
- Re-filter recent results without another API request.
- Export results to JSON or CSV for further analysis.

## Install

On macOS or Linux:

```bash
curl -fsSL https://raw.githubusercontent.com/chauduyphanvu/audible-deals/main/install.sh | bash
```

Or install from source with Python 3.11 or newer:

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e .
```

See [Installation and authentication](installation.md) for Windows instructions, manual downloads, and credential import.

## Authenticate

```bash
deals login
```

The command opens the sign-in page in your browser. After signing in, it may land on a “page not found” page; that is expected. Copy the full URL from the address bar and paste it into the terminal when prompted. For a remote terminal, use `deals login --no-open --via-file /tmp/url.txt`, then delete the callback file when login finishes.

## Find a deal

```bash
# Science fiction under $5
deals find --genre sci-fi --max-price 5

# Cheap, long listens sorted by value per hour
deals find --min-hours 10 --max-price 5

# Search by author, title, or keyword
deals search "Brandon Sanderson" --sort price

# Personalized recommendations based on your library
deals for-you --max-price 5 --on-sale
```

Add `-i` to `find` or `search` to browse results interactively. Run `deals --help` or `deals COMMAND --help` for help in the terminal.

## Where to go next

- [Command guide](commands.md) covers filters, sorting, profiles, exports, and every main workflow.
- [Price tracking and automation](automation.md) covers wishlists, scheduled checks, history, and webhooks.
- [Advanced recipes](recipes.md) combines commands into power-user workflows.
