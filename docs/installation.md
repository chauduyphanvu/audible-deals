# Installation and authentication

audible-deals requires an Audible account. Installing from source requires Python 3.11 or newer (3.11 to 3.14 are tested on Linux; 3.13 on macOS and Windows).

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

audible-deals is not published on PyPI yet, so install it from the GitHub repository. [uv](https://docs.astral.sh/uv/) or [pipx](https://pipx.pypa.io/) puts the `deals` command on your `PATH` in an isolated environment:

```bash
uv tool install git+https://github.com/chauduyphanvu/audible-deals
# or
pipx install git+https://github.com/chauduyphanvu/audible-deals
```

To work on the code instead, clone the repository and install it in editable mode:

```bash
git clone https://github.com/chauduyphanvu/audible-deals.git
cd audible-deals
pip install -e .
```

On Windows, `uv tool install` as above is the recommended way: the one-line installer is for macOS and Linux, and the pre-built binary may not work on every system. Run `deals` from Command Prompt, PowerShell, or Windows Terminal. Do not double-click the executable; the terminal window will close as soon as the command finishes.

## Optional: the full-screen results browser

`deals find -i` (and `search`, `for-me`, `series`, `last`) can open a full-screen browser instead of a line-by-line prompt. It needs the `tui` extra:

```bash
uv tool install 'audible-deals[tui] @ git+https://github.com/chauduyphanvu/audible-deals'
# or
pipx install 'audible-deals[tui] @ git+https://github.com/chauduyphanvu/audible-deals'
```

Without it, or when output is piped, `-i` uses the prompt browser. The pre-built binaries include the full-screen browser. See [Interactive mode](commands.md#interactive-mode).

## Upgrading

Run the one-line installer again, download the newer archive, or reinstall from source (`uv tool install --reinstall git+https://github.com/chauduyphanvu/audible-deals`, or `pipx install --force` with the same address). Your data directory is kept. The first run of a new release imports records that older releases kept in separate JSON files into `state.db` and renames the originals to `*.migrated`; read [Upgrading from JSON files](commands.md#upgrading-from-json-files) before you move back to an older release, and the [changelog](https://github.com/chauduyphanvu/audible-deals/blob/main/CHANGELOG.md) for behavior that changed.

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

## Keep secrets in your OS keychain (optional)

By default your login (`auth.json`) and any webhook URLs and headers are files in the data directory, readable only by you (mode 600). If you would rather have them in the operating system's keychain (macOS Keychain, Windows Credential Manager, the Secret Service on Linux), install the optional extra and switch:

```bash
uv tool install 'audible-deals[keyring] @ git+https://github.com/chauduyphanvu/audible-deals'
deals config set secret-storage keyring      # shows what moves and asks first (-y to skip)
```

The switch stores each secret in the keychain (service `audible-deals`), reads it back to check it, and only then rewrites the files to hold *references*: `auth.json` becomes a small file saying where the login is (and when it expires, which is not secret), and the webhook settings in `config.json` and `monitors.json` read `keyring:<name>`. Nothing else changes, and every command works as before; a refreshed login token is written back to the keychain. `deals config set secret-storage file` moves everything back and removes the keychain entries. Either direction can be interrupted safely: the files are only changed after the new copy is stored and verified.

Things to know:

- Without the `keyring` package, or on a machine with no keychain (a headless server), commands that need a secret stop with a one-line error that says how to fix it or switch back.
- **Background tracking reads the keychain too.** A scheduled `deals track run` may not be able to reach it (a locked login keychain, no desktop session, a service without a user session). `deals doctor` warns about this when both are in use; if runs fail with a keychain error, go back to `file`.
- `deals config get webhook --show-secrets` and `deals config list --show-secrets` look the values up in the keychain.
- The pre-built binaries do not include `keyring` support (it would add platform backends to every download); install with `uv tool` or `pip` to use it.
- The switch moves `deals`' own copy of the credentials. A file you imported from (`deals import-auth PATH`) or any other copy you made stays where it is.

## Next steps

- [Command guide](commands.md)
- [Price tracking and automation](automation.md)
- [Advanced recipes](recipes.md)
