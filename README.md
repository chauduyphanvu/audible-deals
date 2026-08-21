# audible-deals

A command-line tool for finding cheap Audible audiobooks. It scans the Audible catalog, filters by price and genre, and helps you catch deals before they disappear.

## Highlights

- Find inexpensive audiobooks by genre, price, rating, length, narrator, author, series, and more.
- Get personalized recommendations from the authors, narrators, genres, and unfinished series in your library.
- Compare cash prices with the cost of an Audible credit.
- Track wishlist prices and receive Slack, Discord, Teams, ntfy, or generic webhook alerts.
- Monitor saved catalog searches for new matches and price drops on the same schedule.
- Widen, narrow, sort, and reset recent results locally without another API call.
- Use the correct currency and Audible store across all nine supported marketplaces.

## Quick start

### Install

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

Windows users should install from source. See the [installation guide](docs/installation.md) for manual downloads, platform notes, and troubleshooting.

### Authenticate

```bash
deals login
```

Your browser opens the sign-in page. After signing in, it may land on a “page not found” page; that is expected. Copy the full URL from the address bar and paste it into the terminal when prompted. On a remote terminal, use `deals login --no-open --via-file /tmp/url.txt` instead, then delete the callback file when login finishes.

You can also import existing credentials from audible-cli or Libation. See [authentication options](docs/installation.md#authenticate).

### Find a deal

```bash
# Science fiction under $5
deals find --genre sci-fi --max-price 5

# Cheap, long listens sorted by value per hour
deals find --min-hours 10 --max-price 5

# Search by author, title, or keyword
deals search "Brandon Sanderson" --sort price

# Personalized recommendations based on your library
deals for-me --max-price 5 --on-sale
```

Add `-i` to `find` or `search` to browse results interactively. Run `deals --help` or `deals COMMAND --help` for the complete CLI reference.

Result rows use shell-safe references such as `@1`. They work anywhere an
ASIN does, including `deals detail @1`, `deals compare @1 @3`, and
`deals wishlist add @1-3,5`. Product URLs are accepted too. Result output
automatically switches between wide tables, compact tables, and narrow cards.

## Common workflows

| Goal | Command |
|------|---------|
| Find catalog deals | `deals find` |
| Search by keyword | `deals search [QUERY]` |
| Get personalized picks | `deals for-me` |
| Continue series you own | `deals series` |
| Revisit recent results | `deals last` |
| Inspect or compare books | `deals detail`, `deals compare`, `deals open` |
| Browse your library | `deals library` |
| Manage tracked books | `deals wishlist`, `deals watch`, `deals history` |
| Automate price checks | `deals track`, `deals notify`, `deals recap` |
| Monitor a saved search | `deals monitor add --profile NAME`, `deals monitor add --query QUERY` |
| Save preferences | `deals profile`, `deals config` |
| Check the installation | `deals doctor` |

## Documentation

- [Full documentation](https://chauduyphanvu.github.io/audible-deals/) — searchable guides and advanced workflows
- [Documentation source](docs/index.md) — browse the same guides on GitHub
- [Installation and authentication](docs/installation.md) — platform setup, manual installation, and credential import
- [Command guide](docs/commands.md) — filters, sorting, profiles, personalized results, exports, locales, and other command details
- [Price tracking and automation](docs/automation.md) — wishlists, history, scheduled checks, notifications, and webhooks
- [Advanced recipes](docs/recipes.md) — deeper scans, reusable searches, cross-locale checks, scripting, and automation examples

Preview the documentation site locally with `uv run --extra docs mkdocs serve`. After the repository’s Pages source is set to **GitHub Actions**, documentation changes pushed to `main` publish automatically.

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

The tool uses the [audible](https://github.com/mkb79/Audible) Python package to query Audible’s catalog API. Results are filtered and re-sorted locally because the API does not support price sorting. Configuration, cached results, wishlists, monitor definitions and snapshots, and price history stay in `~/.config/audible-deals/`.

See [CONTRIBUTING.md](CONTRIBUTING.md) for development guidelines and [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## Support

If audible-deals saves you time or money, consider [supporting its development on GitHub Sponsors](https://github.com/sponsors/chauduyphanvu).

## Acknowledgements

- [audible](https://github.com/mkb79/Audible) by mkb79 provides access to the Audible API.
- [Libation](https://github.com/rmcrackan/Libation) was a valuable reference for Audible’s undocumented API, and its exported credentials can be imported directly.

## License

[MIT](LICENSE)

This project depends on [audible](https://github.com/mkb79/Audible), which is licensed under AGPL-3.0. The pre-built binaries bundle this library; see its license for details.
