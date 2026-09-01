# Installation and authentication

audible-deals requires an Audible account. Installing from source requires Python 3.11 or newer.

## macOS and Linux

The installer detects your OS and architecture, downloads the matching binary, and verifies its SHA-256 checksum when the release provides one:

```bash
curl -fsSL https://raw.githubusercontent.com/chauduyphanvu/audible-deals/main/install.sh | bash
```

It installs to `~/.local/bin`. If that directory is not already in your `PATH`, the installer adds it. Restart the terminal or reload your shell configuration afterward, for example:

```bash
source ~/.zshrc
```

## Manual download

Download the archive for your platform from the [latest release](https://github.com/chauduyphanvu/audible-deals/releases/latest).

| Platform | File |
|----------|------|
| macOS (Apple Silicon) | `deals-macos-arm64.tar.xz` |
| Linux (x64) | `deals-linux-x64.tar.xz` |
| Windows (x64) | `deals-windows-x64.zip` |

For example, on Apple Silicon:

```bash
tar xJf deals-macos-arm64.tar.xz
mkdir -p ~/.local/lib/deals ~/.local/bin
mv deals-macos-arm64/* ~/.local/lib/deals/
ln -sf ~/.local/lib/deals/deals ~/.local/bin/deals
```

## Install from source

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e .
```

Installing from source is recommended on Windows because the pre-built binary may not work on every system. Run `deals` from Command Prompt, PowerShell, or Windows Terminal. Do not double-click the executable; the terminal window will close as soon as the command finishes.

## Authenticate

The external browser flow works across platforms:

```bash
deals login
```

The command opens the sign-in page in your browser. After signing in, it may land on a “page not found” page; this is expected. Copy the full URL from the address bar and paste it into the terminal when prompted. For a remote terminal or an unusually long callback URL, use `deals login --no-open --via-file /tmp/url.txt` instead, then delete the callback file when login finishes.

Alternatively, import credentials from audible-cli or Libation:

```bash
deals import-auth ~/.audible/auth.json
deals import-auth ~/Libation/AccountsSettings.json
```

Run the built-in diagnostic if authentication or marketplace access is not working:

```bash
deals doctor
```

## Next steps

- [Command guide](commands.md)
- [Price tracking and automation](automation.md)
- [Advanced recipes](recipes.md)
