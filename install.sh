#!/usr/bin/env bash
# Install audible-deals — downloads the latest pre-built binary for your platform.
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/chauduyphanvu/audible-deals/main/install.sh | bash
#
# Options (via env vars):
#   INSTALL_DIR  — where to put the binary (default: ~/.local/bin)
#   VERSION      — specific version to install (default: latest)

set -euo pipefail

REPO="chauduyphanvu/audible-deals"
INSTALL_DIR="${INSTALL_DIR:-$HOME/.local/bin}"
BINARY_NAME="deals"

# --- Detect platform ---

detect_platform() {
    local os arch
    os="$(uname -s)"
    arch="$(uname -m)"

    case "$os" in
        Linux)  os="linux" ;;
        Darwin) os="macos" ;;
        MINGW*|MSYS*|CYGWIN*)
            echo "Error: On Windows, download the .exe manually from:" >&2
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
        echo "$VERSION"
        return
    fi

    local latest
    latest="$(curl -fsSL "https://api.github.com/repos/$REPO/releases/latest" \
        | grep '"tag_name"' | head -1 | cut -d'"' -f4)"

    if [ -z "$latest" ]; then
        echo "Error: Could not determine latest version." >&2
        echo "Set VERSION=0.2.0 explicitly, or check https://github.com/$REPO/releases" >&2
        exit 1
    fi

    # Strip leading 'v' if present
    echo "${latest#v}"
}

# --- Main ---

main() {
    local platform version artifact url

    platform="$(detect_platform)"
    version="$(resolve_version)"
    artifact="deals-${platform}"

    echo "Installing audible-deals v${version} (${platform})..."

    url="https://github.com/$REPO/releases/download/v${version}/${artifact}"

    # Download to temp file
    local tmpfile
    tmpfile="$(mktemp)"
    trap 'rm -f "$tmpfile"' EXIT

    if ! curl -fsSL -o "$tmpfile" "$url"; then
        echo "Error: Download failed." >&2
        echo "  URL: $url" >&2
        echo "" >&2
        echo "Check that v${version} exists at https://github.com/$REPO/releases" >&2
        exit 1
    fi

    # Install
    mkdir -p "$INSTALL_DIR"
    mv "$tmpfile" "${INSTALL_DIR}/${BINARY_NAME}"
    chmod +x "${INSTALL_DIR}/${BINARY_NAME}"

    echo ""
    echo "Installed to ${INSTALL_DIR}/${BINARY_NAME}"

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
