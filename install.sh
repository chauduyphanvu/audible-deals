#!/usr/bin/env bash
# Install audible-deals — downloads the latest pre-built binary for your platform.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chauduyphanvu/audible-deals/main/install.sh | bash
#
# Options (via env vars):
#   INSTALL_DIR  — where to put the symlink (default: ~/.local/bin)
#   LIB_DIR      — where to extract the binary bundle (default: ~/.local/lib/deals)
#   VERSION      — specific version to install (default: latest)

set -euo pipefail

REPO="chauduyphanvu/audible-deals"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
LIB_DIR="${LIB_DIR:-$HOME/.local/lib/deals}"
BINARY_NAME="deals"
FALLBACK_VERSION="0.9.1"

# --- Detect platform ---

detect_platform() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"

    case "$os" in
        Linux)  os="linux" ;;
        Darwin) os="macos" ;;
        MINGW*|MSYS*|CYGWIN*)
            echo "Error: On Windows, download the .zip manually from:" >&2
            echo "  https://github.com/$REPO/releases/latest" >&2
            exit 1
            ;;
        *)
            echo "Error: Unsupported OS: $os" >&2
            exit 1
            ;;
    esac

    case "$arch" in
        x86_64|amd64)  arch="x64" ;;
        arm64|aarch64) arch="arm64" ;;
        *)
            echo "Error: Unsupported architecture: $arch" >&2
            exit 1
            ;;
    esac

    # Only arm64 macOS and x64 Linux binaries are available
    if [ "$os" = "linux" ] && [ "$arch" = "arm64" ]; then
        echo "Error: Linux arm64 binaries are not available yet." >&2
        echo "Install from source instead: pip install audible-deals" >&2
        exit 1
    fi
    if [ "$os" = "macos" ] && [ "$arch" = "x64" ]; then
        echo "Error: macOS Intel binaries are not available. Install from source:" >&2
        echo "  pip install audible-deals" >&2
        exit 1
    fi

    echo "${os}-${arch}"
}

# --- Resolve version ---

resolve_version() {
    if [ -n "${VERSION:-}" ]; then
        echo "${VERSION#v}"
        return
    fi

    local latest
    latest="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4)" || true

    if [ -z "$latest" ]; then
        echo "Warning: Could not determine latest version from GitHub API; falling back to v${FALLBACK_VERSION}" >&2
        echo "${FALLBACK_VERSION}"
        return
    fi

    # Strip leading 'v' if present
    echo "${latest#v}"
}

# --- Checksum helper ---

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# --- Main ---

main() {
    local platform version artifact url

    platform="$(detect_platform)"
    version="$(resolve_version)"

    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]]; then
        echo "Error: resolved version '${version}' does not look like a valid version." >&2
        exit 1
    fi

    artifact="deals-${platform}"

    echo "Installing audible-deals v${version} (${platform})..."

    url="https://github.com/$REPO/releases/download/v${version}/${artifact}.tar.gz"

    # Download to temp file
    local tmpfile tmpsha
    tmpfile="$(mktemp)"
    tmpsha="$(mktemp)"
    trap 'rm -f "$tmpfile" "$tmpsha"' EXIT

    if ! curl -fsSL -o "$tmpfile" "$url"; then
        echo "Error: Download failed." >&2
        echo "  URL: $url" >&2
        echo "" >&2
        echo "Check that v${version} exists at https://github.com/$REPO/releases" >&2
        echo "" >&2
        echo "Alternatively, install from source (requires Python 3.11+):" >&2
        echo "  git clone https://github.com/$REPO.git && cd audible-deals && pip install ." >&2
        exit 1
    fi

    # Verify checksum if sidecar is available
    if curl -fsSL -o "$tmpsha" "${url}.sha256" 2>/dev/null; then
        local expected actual
        expected="$(awk '{print $1}' "$tmpsha")"
        actual="$(sha256_file "$tmpfile")"
        if [ "$actual" != "$expected" ]; then
            echo "Error: Checksum mismatch — download may be corrupt." >&2
            echo "  Expected: $expected" >&2
            echo "  Got:      $actual" >&2
            exit 1
        fi
    else
        echo "Warning: checksum file not found for v${version}; skipping verification." >&2
    fi

    # Remove previous installation if present
    if [ -d "$LIB_DIR" ]; then
        rm -rf "$LIB_DIR"
    fi

    # Extract archive to lib directory
    mkdir -p "$LIB_DIR"
    tar xzf "$tmpfile" -C "$LIB_DIR" --strip-components=1

    chmod +x "${LIB_DIR}/${BINARY_NAME}"

    # Create symlink in INSTALL_DIR
    mkdir -p "$INSTALL_DIR"
    ln -sf "${LIB_DIR}/${BINARY_NAME}" "${INSTALL_DIR}/${BINARY_NAME}"

    echo ""
    echo "Installed to ${LIB_DIR}/"
    echo "Symlinked ${INSTALL_DIR}/${BINARY_NAME} -> ${LIB_DIR}/${BINARY_NAME}"

    # Ensure INSTALL_DIR is in PATH
    if ! echo "$PATH" | tr ':' '\n' | grep -qx "$INSTALL_DIR"; then
        local shell_name rc_file export_line
        shell_name="$(basename "${SHELL:-/bin/bash}")"
        case "$shell_name" in
            zsh)  rc_file="$HOME/.zshrc" ;;
            fish) rc_file="$HOME/.config/fish/config.fish" ;;
            *)    rc_file="$HOME/.bashrc" ;;
        esac

        if [ "$shell_name" = "fish" ]; then
            export_line="fish_add_path ${INSTALL_DIR}"
        else
            export_line="export PATH=\"${INSTALL_DIR}:\$PATH\""
        fi

        # Only add if not already present in the rc file
        if [ -f "$rc_file" ] && grep -qF "$INSTALL_DIR" "$rc_file"; then
            echo ""
            echo "PATH entry already in ${rc_file} — restart your terminal or run:"
            echo "  source ${rc_file}"
        else
            echo "$export_line" >> "$rc_file"
            echo ""
            echo "Added ${INSTALL_DIR} to PATH in ${rc_file}"
            echo ""
            echo "To use 'deals' right now, run:"
            echo "  source ${rc_file}"
        fi
    fi

    echo ""
    echo "Get started:"
    echo "  deals --help"
    echo "  deals login --external --via-file /tmp/url.txt"
}

main
