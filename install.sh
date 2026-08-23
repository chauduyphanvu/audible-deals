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
INSTALL_DIR="${INSTALL_DIR-$HOME/.local/bin}"
LIB_DIR="${LIB_DIR-$HOME/.local/lib/deals}"
BINARY_NAME="deals"
INSTALL_MARKER_NAME=".audible-deals-install"
INSTALL_MARKER_CONTENT="repository=chauduyphanvu/audible-deals"

DOWNLOAD_DIR=""
TRANSACTION_DIR=""
BACKUP_MOVE_INTENT=0
BACKUP_ACTIVE=0
BACKUP_RESTORE_INTENT=0
NEW_MOVE_INTENT=0
NEW_ACTIVE=0
NEW_ROLLBACK_INTENT=0
LINK_CREATE_INTENT=0
LINK_CREATED=0
LIBRARY_STATE_AMBIGUOUS=0
PRESERVE_TRANSACTION=0
COMMITTED=0

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

    local release_prefix latest_url latest
    release_prefix="https://github.com/$REPO/releases/tag/"

    if latest_url="$(curl -fsSL -o /dev/null -w '%{url_effective}' "https://github.com/$REPO/releases/latest" 2>/dev/null)"; then
        case "$latest_url" in
            "$release_prefix"*)
                latest="${latest_url#"$release_prefix"}"
                latest="${latest#v}"
                if [[ "$latest" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]]; then
                    echo "$latest"
                    return
                fi
                ;;
        esac
    fi

    echo "Error: Could not resolve the latest release from GitHub; retry or set VERSION explicitly." >&2
    return 1
}

# --- Checksum helper ---

sha256_file() {
    if command -v sha256sum >/dev/null 2>&1; then
        sha256sum "$1" | awk '{print $1}'
    else
        shasum -a 256 "$1" | awk '{print $1}'
    fi
}

# --- Path validation ---

resolve_directory_path() {
    local input="$1"
    local physical component candidate result remainder
    local -a pending
    local pending_count=0
    local index

    if [ -z "$input" ]; then
        echo "Error: directory path must not be empty." >&2
        return 1
    fi

    if [[ "$input" = /* ]]; then
        physical="/"
    else
        physical="$(pwd -P)" || return 1
    fi

    pending=()
    remainder="$input/"

    while [ -n "$remainder" ]; do
        component="${remainder%%/*}"
        remainder="${remainder#*/}"
        case "$component" in
            ""|.)
                continue
                ;;
            ..)
                if [ "$pending_count" -gt 0 ]; then
                    index=$((pending_count - 1))
                    unset "pending[$index]"
                    pending_count=$index
                else
                    physical="$(cd -P -- "$physical/.." 2>/dev/null && pwd -P)" || {
                        echo "Error: could not resolve directory path '$input'." >&2
                        return 1
                    }
                fi
                ;;
            *)
                if [ "$pending_count" -gt 0 ]; then
                    pending[pending_count]="$component"
                    pending_count=$((pending_count + 1))
                    continue
                fi

                if [ "$physical" = "/" ]; then
                    candidate="/$component"
                else
                    candidate="$physical/$component"
                fi

                if [ -d "$candidate" ]; then
                    physical="$(cd -P -- "$candidate" 2>/dev/null && pwd -P)" || {
                        echo "Error: could not resolve directory path '$input'." >&2
                        return 1
                    }
                elif [ -e "$candidate" ] || [ -L "$candidate" ]; then
                    echo "Error: path component '$candidate' is not a directory." >&2
                    return 1
                else
                    pending[pending_count]="$component"
                    pending_count=$((pending_count + 1))
                fi
                ;;
        esac
    done

    result="$physical"
    index=0
    while [ "$index" -lt "$pending_count" ]; do
        component="${pending[$index]}"
        if [ "$result" = "/" ]; then
            result="/$component"
        else
            result="$result/$component"
        fi
        index=$((index + 1))
    done

    echo "$result"
}

directory_is_empty() {
    local directory="$1"
    local entry

    for entry in "$directory"/* "$directory"/.[!.]* "$directory"/..?*; do
        if [ -e "$entry" ] || [ -L "$entry" ]; then
            return 1
        fi
    done
    return 0
}

path_exists() {
    [ -e "$1" ] || [ -L "$1" ]
}

has_install_marker() {
    local directory="$1"
    local marker="$directory/$INSTALL_MARKER_NAME"
    local marker_size

    if [ ! -f "$marker" ] || [ -L "$marker" ]; then
        return 1
    fi

    marker_size="$(wc -c < "$marker")" || return 1
    [ "$marker_size" -eq "${#INSTALL_MARKER_CONTENT}" ] &&
        [ "$(cat "$marker")" = "$INSTALL_MARKER_CONTENT" ]
}

validate_lib_dir() {
    local input="${1-}"
    local resolved canonical_home canonical_default parent

    if [ -z "$input" ]; then
        echo "Error: LIB_DIR must not be empty." >&2
        return 1
    fi
    if [[ "$input" = *$'\n'* ]]; then
        echo "Error: LIB_DIR must not contain newline characters." >&2
        return 1
    fi

    resolved="$(resolve_directory_path "$input")" || return 1
    canonical_home="$(resolve_directory_path "$HOME")" || {
        echo "Error: HOME could not be resolved." >&2
        return 1
    }
    canonical_default="$(resolve_directory_path "$HOME/.local/lib/deals")" || {
        echo "Error: the default LIB_DIR could not be resolved." >&2
        return 1
    }

    if [ "$resolved" = "/" ]; then
        echo "Error: LIB_DIR must not be the filesystem root." >&2
        return 1
    fi

    if [ "$resolved" = "$canonical_home" ] || [[ "$canonical_home" = "$resolved/"* ]]; then
        echo "Error: LIB_DIR must not be HOME or an ancestor of HOME." >&2
        return 1
    fi

    parent="${resolved%/*}"
    if [ -z "$parent" ]; then
        parent="/"
    fi
    if [ "$parent" = "/" ]; then
        echo "Error: LIB_DIR must not be a one-component top-level path." >&2
        return 1
    fi

    if [ -e "$resolved" ] && [ ! -d "$resolved" ]; then
        echo "Error: LIB_DIR exists and is not a directory: $resolved" >&2
        return 1
    fi

    if [ -d "$resolved" ] && ! directory_is_empty "$resolved"; then
        if has_install_marker "$resolved"; then
            :
        elif [ "$resolved" = "$canonical_default" ] &&
             [ -f "$resolved/$BINARY_NAME" ] &&
             [ ! -L "$resolved/$BINARY_NAME" ]; then
            :
        else
            echo "Error: existing LIB_DIR is not a dedicated audible-deals installation: $resolved" >&2
            return 1
        fi
    fi

    echo "$resolved"
}

validate_install_dir() {
    local input="${1-}"
    local lib_dir="$2"
    local resolved link_path

    if [ -z "$input" ]; then
        echo "Error: INSTALL_DIR must not be empty." >&2
        return 1
    fi
    if [[ "$input" = *$'\n'* ]]; then
        echo "Error: INSTALL_DIR must not contain newline characters." >&2
        return 1
    fi

    resolved="$(resolve_directory_path "$input")" || return 1
    link_path="$resolved/$BINARY_NAME"

    if [ "$link_path" = "$lib_dir" ] ||
       [[ "$link_path" = "$lib_dir/"* ]] ||
       [[ "$lib_dir" = "$link_path/"* ]]; then
        echo "Error: INSTALL_DIR/$BINARY_NAME and LIB_DIR must not overlap." >&2
        return 1
    fi

    if [ -d "$link_path" ]; then
        echo "Error: a directory already exists at $link_path." >&2
        return 1
    fi

    echo "$resolved"
}

command_link_is_managed() {
    local link_path="$1/$BINARY_NAME"
    local binary_path="$2/$BINARY_NAME"

    [ -L "$link_path" ] && [ -e "$binary_path" ] && [ "$link_path" -ef "$binary_path" ]
}

command_link_is_exact() {
    local link_path="$1/$BINARY_NAME"
    local expected_target="$2/$BINARY_NAME"
    local actual_target

    [ -L "$link_path" ] || return 1
    actual_target="$(readlink "$link_path")" || return 1
    [ "$actual_target" = "$expected_target" ]
}

validate_command_link() {
    local link_path="$1/$BINARY_NAME"

    if [ -L "$link_path" ]; then
        if command_link_is_managed "$1" "$2"; then
            return 0
        fi
        echo "Error: a foreign symlink already exists at $link_path." >&2
        return 1
    fi

    if [ -e "$link_path" ]; then
        echo "Error: an unmanaged file already exists at $link_path." >&2
        return 1
    fi

    return 0
}

# --- Transaction cleanup ---

report_move_ambiguity() {
    echo "Error: Could not determine the result of the $1 rename." >&2
    echo "Recovery paths were preserved for manual inspection:" >&2
    echo "  $2" >&2
    echo "  $3" >&2
    PRESERVE_TRANSACTION=1
}

reconcile_backup_move() {
    if path_exists "$LIB_DIR" && ! path_exists "$TRANSACTION_DIR/backup"; then
        BACKUP_MOVE_INTENT=0
        BACKUP_ACTIVE=0
        return 0
    fi
    if ! path_exists "$LIB_DIR" && path_exists "$TRANSACTION_DIR/backup"; then
        BACKUP_MOVE_INTENT=0
        BACKUP_ACTIVE=1
        return 0
    fi

    BACKUP_MOVE_INTENT=0
    LIBRARY_STATE_AMBIGUOUS=1
    report_move_ambiguity "backup" "$LIB_DIR" "$TRANSACTION_DIR/backup"
    return 1
}

reconcile_payload_move() {
    if path_exists "$TRANSACTION_DIR/payload" && ! path_exists "$LIB_DIR"; then
        NEW_MOVE_INTENT=0
        NEW_ACTIVE=0
        return 0
    fi
    if ! path_exists "$TRANSACTION_DIR/payload" && path_exists "$LIB_DIR"; then
        NEW_MOVE_INTENT=0
        NEW_ACTIVE=1
        return 0
    fi

    NEW_MOVE_INTENT=0
    LIBRARY_STATE_AMBIGUOUS=1
    report_move_ambiguity "payload promotion" "$TRANSACTION_DIR/payload" "$LIB_DIR"
    return 1
}

reconcile_new_rollback() {
    if path_exists "$LIB_DIR" && ! path_exists "$TRANSACTION_DIR/payload"; then
        NEW_ROLLBACK_INTENT=0
        NEW_ACTIVE=1
        return 0
    fi
    if ! path_exists "$LIB_DIR" && path_exists "$TRANSACTION_DIR/payload"; then
        NEW_ROLLBACK_INTENT=0
        NEW_ACTIVE=0
        return 0
    fi

    NEW_ROLLBACK_INTENT=0
    LIBRARY_STATE_AMBIGUOUS=1
    report_move_ambiguity "new-library rollback" "$LIB_DIR" "$TRANSACTION_DIR/payload"
    return 1
}

reconcile_backup_restore() {
    if path_exists "$TRANSACTION_DIR/backup" && ! path_exists "$LIB_DIR"; then
        BACKUP_RESTORE_INTENT=0
        BACKUP_ACTIVE=1
        return 0
    fi
    if ! path_exists "$TRANSACTION_DIR/backup" && path_exists "$LIB_DIR"; then
        BACKUP_RESTORE_INTENT=0
        BACKUP_ACTIVE=0
        return 0
    fi

    BACKUP_RESTORE_INTENT=0
    LIBRARY_STATE_AMBIGUOUS=1
    report_move_ambiguity "backup restoration" "$TRANSACTION_DIR/backup" "$LIB_DIR"
    return 1
}

report_backup_recovery() {
    PRESERVE_TRANSACTION=1
    echo "Error: automatic restoration failed." >&2
    if path_exists "$TRANSACTION_DIR/backup"; then
        echo "The previous installation backup was preserved at:" >&2
        echo "  $TRANSACTION_DIR/backup" >&2
        echo "Move it back to $LIB_DIR manually after resolving the filesystem error." >&2
    else
        echo "Installer recovery data was preserved at:" >&2
        echo "  $TRANSACTION_DIR" >&2
        echo "The replacement library may still be at $LIB_DIR." >&2
    fi
}

rollback_install() {
    if [ "$NEW_ACTIVE" -eq 1 ]; then
        if ! path_exists "$LIB_DIR" || path_exists "$TRANSACTION_DIR/payload"; then
            report_backup_recovery
            return 1
        fi

        NEW_ROLLBACK_INTENT=1
        if ! mv "$LIB_DIR" "$TRANSACTION_DIR/payload"; then
            :
        fi
        if ! reconcile_new_rollback; then
            report_backup_recovery
            return 1
        fi
        if [ "$NEW_ACTIVE" -ne 0 ]; then
            report_backup_recovery
            return 1
        fi
    fi

    if [ "$BACKUP_ACTIVE" -eq 1 ]; then
        if ! path_exists "$TRANSACTION_DIR/backup" || path_exists "$LIB_DIR"; then
            report_backup_recovery
            return 1
        fi

        BACKUP_RESTORE_INTENT=1
        if ! mv "$TRANSACTION_DIR/backup" "$LIB_DIR"; then
            :
        fi
        if ! reconcile_backup_restore; then
            report_backup_recovery
            return 1
        fi
        if [ "$BACKUP_ACTIVE" -ne 0 ]; then
            report_backup_recovery
            return 1
        fi
    fi

    return 0
}

remove_created_link() {
    local link_path="$INSTALL_DIR/$BINARY_NAME"

    if [ "$LINK_CREATED" -ne 1 ]; then
        return 0
    fi

    if command_link_is_exact "$INSTALL_DIR" "$LIB_DIR"; then
        if ! rm -f -- "$link_path"; then
            echo "Error: Could not remove the command link created by this installer." >&2
            return 1
        fi
    elif path_exists "$link_path"; then
        echo "Warning: The command path changed during installation and was preserved: $link_path" >&2
    fi

    LINK_CREATED=0
    return 0
}

cleanup_install() {
    local status=$?

    trap - EXIT
    trap '' HUP INT TERM

    if [ "$COMMITTED" -eq 0 ]; then
        if [ "$BACKUP_MOVE_INTENT" -eq 1 ] && ! reconcile_backup_move; then
            status=1
        fi
        if [ "$NEW_MOVE_INTENT" -eq 1 ] && ! reconcile_payload_move; then
            status=1
        fi
        if [ "$LINK_CREATE_INTENT" -eq 1 ]; then
            LINK_CREATE_INTENT=0
            if command_link_is_exact "$INSTALL_DIR" "$LIB_DIR"; then
                echo "Warning: Command-link creation was interrupted; the ambiguous link was preserved: $INSTALL_DIR/$BINARY_NAME" >&2
            fi
        fi

        if ! remove_created_link; then
            status=1
        fi

        if [ "$LIBRARY_STATE_AMBIGUOUS" -eq 0 ] &&
           { [ "$BACKUP_ACTIVE" -eq 1 ] || [ "$NEW_ACTIVE" -eq 1 ]; }; then
            if ! rollback_install; then
                status=1
            fi
        fi
    fi

    if [ "$BACKUP_ACTIVE" -eq 1 ] || [ "$NEW_ACTIVE" -eq 1 ] ||
       [ "$BACKUP_MOVE_INTENT" -eq 1 ] || [ "$BACKUP_RESTORE_INTENT" -eq 1 ] ||
       [ "$NEW_MOVE_INTENT" -eq 1 ] || [ "$NEW_ROLLBACK_INTENT" -eq 1 ]; then
        PRESERVE_TRANSACTION=1
    fi

    if [ -n "$DOWNLOAD_DIR" ] && [ -d "$DOWNLOAD_DIR" ]; then
        if ! rm -rf -- "$DOWNLOAD_DIR"; then
            status=1
        fi
    fi
    if [ "$PRESERVE_TRANSACTION" -eq 0 ] &&
       [ -n "$TRANSACTION_DIR" ] && [ -d "$TRANSACTION_DIR" ]; then
        if ! rm -rf -- "$TRANSACTION_DIR"; then
            status=1
        fi
    fi

    exit "$status"
}

# --- Main ---

main() {
    local platform version artifact url archive checksum
    local expected actual lib_parent marker_path

    LIB_DIR="$(validate_lib_dir "$LIB_DIR")" || exit 1
    INSTALL_DIR="$(validate_install_dir "$INSTALL_DIR" "$LIB_DIR")" || exit 1
    validate_command_link "$INSTALL_DIR" "$LIB_DIR" || exit 1

    platform="$(detect_platform)"
    version="$(resolve_version)"

    if [[ ! "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+([.-][A-Za-z0-9.]+)?$ ]]; then
        echo "Error: resolved version '${version}' does not look like a valid version." >&2
        exit 1
    fi

    artifact="deals-${platform}"

    echo "Installing audible-deals v${version} (${platform})..."

    url="https://github.com/$REPO/releases/download/v${version}/${artifact}.tar.gz"

    DOWNLOAD_DIR="$(mktemp -d)"
    archive="$DOWNLOAD_DIR/$artifact.tar.gz"
    checksum="$DOWNLOAD_DIR/$artifact.tar.gz.sha256"
    trap cleanup_install EXIT
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM

    if ! curl -fsSL -o "$archive" "$url"; then
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
    if curl -fsSL -o "$checksum" "${url}.sha256" 2>/dev/null; then
        expected="$(awk '{print $1}' "$checksum")"
        actual="$(sha256_file "$archive")"
        if [ "$actual" != "$expected" ]; then
            echo "Error: Checksum mismatch — download may be corrupt." >&2
            echo "  Expected: $expected" >&2
            echo "  Got:      $actual" >&2
            exit 1
        fi
    else
        echo "Warning: checksum file not found for v${version}; skipping verification." >&2
    fi

    lib_parent="${LIB_DIR%/*}"
    mkdir -p "$lib_parent"
    TRANSACTION_DIR="$(mktemp -d "$lib_parent/.deals-install.XXXXXX")"
    mkdir "$TRANSACTION_DIR/payload"

    if ! tar xzf "$archive" -C "$TRANSACTION_DIR/payload" --strip-components=1; then
        echo "Error: Could not extract downloaded archive." >&2
        exit 1
    fi

    if [ ! -f "$TRANSACTION_DIR/payload/$BINARY_NAME" ] || [ -L "$TRANSACTION_DIR/payload/$BINARY_NAME" ]; then
        echo "Error: Downloaded archive does not contain the expected $BINARY_NAME binary." >&2
        exit 1
    fi

    marker_path="$TRANSACTION_DIR/payload/$INSTALL_MARKER_NAME"
    if path_exists "$marker_path"; then
        echo "Error: Downloaded archive contains the reserved installer marker." >&2
        exit 1
    fi
    printf '%s' "$INSTALL_MARKER_CONTENT" > "$marker_path"

    chmod +x "$TRANSACTION_DIR/payload/$BINARY_NAME"
    if [ ! -x "$TRANSACTION_DIR/payload/$BINARY_NAME" ]; then
        echo "Error: Downloaded $BINARY_NAME binary is not executable." >&2
        exit 1
    fi
    if ! "$TRANSACTION_DIR/payload/$BINARY_NAME" --version >/dev/null 2>&1; then
        echo "Error: Downloaded $BINARY_NAME binary failed its staged --version check." >&2
        exit 1
    fi

    mkdir -p "$INSTALL_DIR"
    validate_command_link "$INSTALL_DIR" "$LIB_DIR" || exit 1

    if path_exists "$LIB_DIR"; then
        BACKUP_MOVE_INTENT=1
        if mv "$LIB_DIR" "$TRANSACTION_DIR/backup"; then
            if ! reconcile_backup_move || [ "$BACKUP_ACTIVE" -ne 1 ]; then
                echo "Error: Could not confirm preservation of the previous installation." >&2
                exit 1
            fi
        else
            reconcile_backup_move || true
            echo "Error: Could not preserve the previous installation." >&2
            exit 1
        fi
    fi

    NEW_MOVE_INTENT=1
    if mv "$TRANSACTION_DIR/payload" "$LIB_DIR"; then
        if ! reconcile_payload_move || [ "$NEW_ACTIVE" -ne 1 ]; then
            echo "Error: Could not confirm installation of the new library." >&2
            exit 1
        fi
    else
        reconcile_payload_move || true
        echo "Error: Could not install the new library." >&2
        exit 1
    fi

    if [ ! -L "$INSTALL_DIR/$BINARY_NAME" ]; then
        LINK_CREATE_INTENT=1
        if ln -s "$LIB_DIR/$BINARY_NAME" "$INSTALL_DIR/$BINARY_NAME"; then
            LINK_CREATE_INTENT=0
            if command_link_is_exact "$INSTALL_DIR" "$LIB_DIR"; then
                LINK_CREATED=1
            else
                echo "Error: Could not confirm installation of the command symlink." >&2
                exit 1
            fi
        else
            LINK_CREATE_INTENT=0
            if command_link_is_exact "$INSTALL_DIR" "$LIB_DIR"; then
                echo "Warning: Command-link ownership is ambiguous; the link was preserved: $INSTALL_DIR/$BINARY_NAME" >&2
            fi
            echo "Error: Could not install the command symlink." >&2
            exit 1
        fi
    fi

    if ! command_link_is_managed "$INSTALL_DIR" "$LIB_DIR"; then
        echo "Error: Installed command link does not resolve to the new $BINARY_NAME binary." >&2
        exit 1
    fi
    if ! "$INSTALL_DIR/$BINARY_NAME" --version >/dev/null 2>&1; then
        echo "Error: Installed command failed its final --version check." >&2
        exit 1
    fi

    COMMITTED=1
    rm -rf -- "$TRANSACTION_DIR"
    TRANSACTION_DIR=""
    BACKUP_ACTIVE=0
    NEW_ACTIVE=0
    LINK_CREATED=0
    rm -rf -- "$DOWNLOAD_DIR"
    DOWNLOAD_DIR=""

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

if [ "${BASH_SOURCE[0]:-$0}" = "$0" ]; then
    main "$@"
fi
