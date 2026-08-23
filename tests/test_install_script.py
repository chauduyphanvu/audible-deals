from __future__ import annotations

import hashlib
import io
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "install.sh"
BASH = shutil.which("bash") or "/bin/bash"
REAL_MV = shutil.which("mv") or "/bin/mv"
REAL_LN = shutil.which("ln") or "/bin/ln"
REAL_CP = shutil.which("cp") or "/bin/cp"
INSTALL_MARKER_NAME = ".audible-deals-install"
INSTALL_MARKER_CONTENT = "repository=chauduyphanvu/audible-deals"


def write_executable(path: Path, output: str) -> None:
    path.write_text(f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(output)}\n")
    path.chmod(0o755)


def write_install_marker(directory: Path) -> None:
    (directory / INSTALL_MARKER_NAME).write_text(INSTALL_MARKER_CONTENT)


def make_archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, contents in files.items():
            info = tarfile.TarInfo(f"bundle/{name}")
            info.size = len(contents)
            info.mode = 0o644
            archive.addfile(info, fileobj=io.BytesIO(contents))


def write_checksum(archive: Path, checksum: Path, digest: str | None = None) -> None:
    value = digest or hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(f"{value}  {archive.name}\n")


def make_curl_shim(directory: Path) -> None:
    curl = directory / "curl"
    curl.write_text(
        f"""#!/bin/bash
set -eu
output=""
write_format=""
url=""
while [ "$#" -gt 0 ]; do
    case "$1" in
        -o)
            output="$2"
            shift 2
            ;;
        -w)
            write_format="$2"
            shift 2
            ;;
        -*)
            shift
            ;;
        *)
            url="$1"
            shift
            ;;
    esac
done
printf '%s\n' "$url" >> "$TEST_CURL_LOG"
if [ "$url" = "https://github.com/chauduyphanvu/audible-deals/releases/latest" ]; then
    [ "$output" = "/dev/null" ] || exit 64
    [ "$write_format" = '%{{url_effective}}' ] || exit 64
    if [ "${{TEST_LATEST_FAIL:-0}}" = "1" ]; then
        exit 22
    fi
    printf '%s' "${{TEST_LATEST_URL:-}}"
    exit 0
fi
if [ "${{TEST_DOWNLOAD_FAIL:-0}}" = "1" ] && [[ "$url" != *.sha256 ]]; then
    exit 22
fi
if [[ "$url" = *.sha256 ]]; then
    [ -n "${{TEST_CHECKSUM:-}}" ] || exit 22
    exec {shlex.quote(REAL_CP)} "$TEST_CHECKSUM" "$output"
fi
exec {shlex.quote(REAL_CP)} "$TEST_ARCHIVE" "$output"
"""
    )
    curl.chmod(0o755)


def make_mv_shim(directory: Path) -> None:
    mv = directory / "mv"
    mv.write_text(
        f"""#!/bin/bash
set -eu
args=("$@")
operands=()
for argument in "${{args[@]}}"; do
    case "$argument" in
        -*) ;;
        *) operands[${{#operands[@]}}]="$argument" ;;
    esac
done
count=${{#operands[@]}}
source_path="${{operands[$((count - 2))]}}"
destination="${{operands[$((count - 1))]}}"
mode="${{MV_FAIL_MODE:-}}"
if [ "$destination" = "$LIB_DIR" ]; then
    if [[ "$source_path" = */.deals-install.*/payload ]] && \
       {{ [ "$mode" = "promotion" ] || [ "$mode" = "promotion_restore" ]; }}; then
        exit 71
    fi
    if [[ "$source_path" = */.deals-install.*/payload ]] && \
       [ "$mode" = "promotion_post_move_failure" ]; then
        {shlex.quote(REAL_MV)} "$@"
        exit 74
    fi
    if [[ "$source_path" = */.deals-install.*/backup ]] && \
       [ "$mode" = "promotion_restore" ]; then
        exit 72
    fi
fi
if [ "$source_path" = "$LIB_DIR" ] && \
   [[ "$destination" = */.deals-install.*/backup ]] && \
   [ "$mode" = "signal_after_backup" ]; then
    {shlex.quote(REAL_MV)} "$@"
    kill -TERM "$PPID"
    exit 0
fi
if [ "$source_path" = "$LIB_DIR" ] && \
   [[ "$destination" = */.deals-install.*/backup ]] && \
   [ "$mode" = "backup_post_move_failure" ]; then
    {shlex.quote(REAL_MV)} "$@"
    exit 76
fi
exec {shlex.quote(REAL_MV)} "$@"
"""
    )
    mv.chmod(0o755)


def make_ln_shim(directory: Path) -> None:
    ln = directory / "ln"
    ln.write_text(
        f"""#!/bin/bash
set -eu
target="$2"
destination="$3"
mode="${{LN_FAIL_MODE:-}}"
case "$mode" in
    failure)
        exit 73
        ;;
    post_create_failure)
        {shlex.quote(REAL_LN)} "$@"
        exit 75
        ;;
    concurrent_exact_symlink)
        {shlex.quote(REAL_LN)} -s "$target" "$destination"
        ;;
    concurrent_regular)
        printf '#!/bin/sh\nprintf "%%s\\n" "concurrent command"\n' > "$destination"
        chmod +x "$destination"
        ;;
    concurrent_foreign_symlink)
        {shlex.quote(REAL_LN)} -s "$TEST_FOREIGN_COMMAND" "$destination"
        ;;
esac
exec {shlex.quote(REAL_LN)} -s "$target" "$destination"
"""
    )
    ln.chmod(0o755)


def base_environment(
    tmp_path: Path,
    lib_dir: Path,
    install_dir: Path,
    archive: Path,
    checksum: Path,
) -> dict[str, str]:
    home = tmp_path / "home"
    shim_dir = tmp_path / "shims"
    download_root = tmp_path / "downloads"
    home.mkdir(exist_ok=True)
    shim_dir.mkdir(exist_ok=True)
    download_root.mkdir(exist_ok=True)
    install_dir.mkdir(parents=True, exist_ok=True)
    make_curl_shim(shim_dir)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "INSTALL_DIR": str(install_dir),
            "LIB_DIR": str(lib_dir),
            "PATH": os.pathsep.join(
                [str(shim_dir), str(install_dir), os.environ.get("PATH", "")]
            ),
            "SHELL": BASH,
            "TEST_ARCHIVE": str(archive),
            "TEST_CHECKSUM": str(checksum),
            "TEST_CURL_LOG": str(tmp_path / "curl.log"),
            "TMPDIR": str(download_root),
            "VERSION": "1.2.3",
        }
    )
    return env


def setup_old_install(lib_dir: Path, install_dir: Path) -> Path:
    lib_dir.mkdir(parents=True)
    write_executable(lib_dir / "deals", "old")
    (lib_dir / "old-only").write_text("old payload")
    write_install_marker(lib_dir)
    install_dir.mkdir(parents=True, exist_ok=True)
    link = install_dir / "deals"
    link.symlink_to(lib_dir / "deals")
    return link


def run_installer(
    tmp_path: Path, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    cwd = tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    return subprocess.run(
        [BASH, str(INSTALL_SCRIPT)],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def run_validator(
    candidate: str,
    *,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            BASH,
            "-c",
            'source "$1"; validate_lib_dir "$2"',
            "bash",
            str(INSTALL_SCRIPT),
            candidate,
        ],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def assert_old_install_works(lib_dir: Path, link: Path) -> None:
    assert (
        subprocess.run(
            [str(lib_dir / "deals")], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )


def assert_no_temporary_files(tmp_path: Path, lib_dir: Path) -> None:
    assert list(lib_dir.parent.glob(".deals-install.*")) == []
    assert list((tmp_path / "downloads").iterdir()) == []


def install_fixture(
    tmp_path: Path,
    files: dict[str, bytes] | None = None,
) -> tuple[Path, Path, Path, Path, dict[str, str]]:
    archive = tmp_path / "release.tar.gz"
    checksum = tmp_path / "release.tar.gz.sha256"
    make_archive(
        archive,
        files
        or {
            "deals": b"#!/bin/sh\nprintf '%s\\n' new\n",
            "new-only": b"new payload\n",
        },
    )
    write_checksum(archive, checksum)
    lib_dir = tmp_path / "library" / "custom-location"
    install_dir = tmp_path / "commands" / "bin"
    link = setup_old_install(lib_dir, install_dir)
    env = base_environment(tmp_path, lib_dir, install_dir, archive, checksum)
    return lib_dir, install_dir, link, archive, env


def test_rejects_unsafe_lib_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    safe_cwd = tmp_path / "validator-work"
    safe_cwd.mkdir()
    occupied = tmp_path / "nested" / "occupied"
    occupied.mkdir(parents=True)
    (occupied / "unrelated-file").write_text("not an install")
    home_link = tmp_path / "home-link"
    home_link.symlink_to(home, target_is_directory=True)

    shim_dir = tmp_path / "validator-shims"
    shim_dir.mkdir()
    curl = shim_dir / "curl"
    curl.write_text("#!/bin/sh\nexit 22\n")
    curl.chmod(0o755)

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "INSTALL_DIR": str(tmp_path / "safe-source" / "bin"),
            "LIB_DIR": str(tmp_path / "safe-source" / "lib"),
            "PATH": os.pathsep.join([str(shim_dir), os.environ.get("PATH", "")]),
            "VERSION": "1.2.3",
        }
    )

    candidates = [
        "",
        "/",
        str(home),
        str(tmp_path),
        "/audible-deals-unsafe-validator-target",
        str(home_link),
        str(occupied),
    ]
    for candidate in candidates:
        result = run_validator(candidate, cwd=safe_cwd, env=env)
        assert result.returncode != 0, (candidate, result.stdout, result.stderr)

    assert not (tmp_path / "safe-source").exists()


def test_resolves_relative_lib_dirs(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = home / "projects" / "current"
    cwd.mkdir(parents=True)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "INSTALL_DIR": str(home / "safe" / "bin"),
            "LIB_DIR": str(home / "safe" / "lib"),
        }
    )

    current = run_validator("./custom", cwd=cwd, env=env)
    assert current.returncode == 0, current.stderr
    assert current.stdout.strip() == str(cwd / "custom")

    sibling = run_validator("../shared/custom", cwd=cwd, env=env)
    assert sibling.returncode == 0, sibling.stderr
    assert sibling.stdout.strip() == str(home / "projects" / "shared" / "custom")

    forbidden = run_validator("../..", cwd=cwd, env=env)
    assert forbidden.returncode != 0


def test_existing_install_marker_allows_custom_path(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "work"
    cwd.mkdir()
    custom = tmp_path / "arbitrary" / "bundle-location"
    custom.mkdir(parents=True)
    write_executable(custom / "deals", "old")
    (custom / "other-file").write_text("part of the bundle")
    write_install_marker(custom)
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "INSTALL_DIR": str(tmp_path / "safe" / "bin"),
            "LIB_DIR": str(tmp_path / "safe" / "lib"),
        }
    )

    result = run_validator(str(custom), cwd=cwd, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(custom)


def test_custom_binary_without_marker_is_rejected_and_preserved(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    checksum = tmp_path / "release.tar.gz.sha256"
    make_archive(archive, {"deals": b"#!/bin/sh\nexit 0\n"})
    write_checksum(archive, checksum)
    lib_dir = tmp_path / "project" / "broad-directory"
    install_dir = tmp_path / "commands" / "bin"
    lib_dir.mkdir(parents=True)
    write_executable(lib_dir / "deals", "unrelated")
    sentinel = lib_dir / "project-data"
    sentinel.write_text("preserve me")
    env = base_environment(tmp_path, lib_dir, install_dir, archive, checksum)

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "not a dedicated audible-deals installation" in result.stderr
    assert sentinel.read_text() == "preserve me"
    assert (
        subprocess.run(
            [str(lib_dir / "deals")], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "unrelated"
    )
    assert list((tmp_path / "downloads").iterdir()) == []


def test_canonical_default_legacy_install_is_accepted(tmp_path: Path) -> None:
    home = tmp_path / "home"
    default = home / ".local" / "lib" / "deals"
    cwd = tmp_path / "work"
    default.mkdir(parents=True)
    cwd.mkdir()
    write_executable(default / "deals", "legacy")
    (default / "legacy-file").write_text("legacy payload")
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "INSTALL_DIR": str(home / ".local" / "bin"),
            "LIB_DIR": str(default),
        }
    )

    result = run_validator(str(default), cwd=cwd, env=env)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(default)


@pytest.mark.parametrize("placement", ["trailing", "embedded"])
def test_newline_lib_dir_is_rejected_without_touching_sentinel(
    tmp_path: Path, placement: str
) -> None:
    archive = tmp_path / "release.tar.gz"
    checksum = tmp_path / "release.tar.gz.sha256"
    make_archive(archive, {"deals": b"#!/bin/sh\nexit 0\n"})
    write_checksum(archive, checksum)
    install_dir = tmp_path / "commands" / "bin"
    if placement == "trailing":
        protected_lib = tmp_path / "library" / "protected"
        configured_lib = f"{protected_lib}\n"
    else:
        protected_lib = tmp_path / "library" / "protected\nsegment"
        configured_lib = str(protected_lib)
    link = setup_old_install(protected_lib, install_dir)
    env = base_environment(tmp_path, protected_lib, install_dir, archive, checksum)
    env["LIB_DIR"] = configured_lib

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "LIB_DIR must not contain newline" in result.stderr
    assert_old_install_works(protected_lib, link)
    assert (protected_lib / "old-only").read_text() == "old payload"
    assert list((tmp_path / "downloads").iterdir()) == []


@pytest.mark.parametrize("placement", ["trailing", "embedded"])
def test_newline_install_dir_is_rejected_without_touching_sentinel(
    tmp_path: Path, placement: str
) -> None:
    archive = tmp_path / "release.tar.gz"
    checksum = tmp_path / "release.tar.gz.sha256"
    make_archive(archive, {"deals": b"#!/bin/sh\nexit 0\n"})
    write_checksum(archive, checksum)
    lib_dir = tmp_path / "library" / "custom-location"
    if placement == "trailing":
        protected_install = tmp_path / "commands" / "protected"
        configured_install = f"{protected_install}\n"
    else:
        protected_install = tmp_path / "commands" / "protected\nsegment"
        configured_install = str(protected_install)
    link = setup_old_install(lib_dir, protected_install)
    env = base_environment(tmp_path, lib_dir, protected_install, archive, checksum)
    env["INSTALL_DIR"] = configured_install

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "INSTALL_DIR must not contain newline" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert (lib_dir / "old-only").read_text() == "old payload"
    assert list((tmp_path / "downloads").iterdir()) == []


def test_rejects_reverse_install_path_overlap(tmp_path: Path) -> None:
    archive = tmp_path / "release.tar.gz"
    checksum = tmp_path / "release.tar.gz.sha256"
    make_archive(archive, {"deals": b"#!/bin/sh\nexit 0\n"})
    write_checksum(archive, checksum)
    install_dir = tmp_path / "commands" / "bin"
    lib_dir = install_dir / "deals" / "payload"
    env = base_environment(tmp_path, lib_dir, install_dir, archive, checksum)

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "must not overlap" in result.stderr
    assert not (install_dir / "deals").exists()
    assert list((tmp_path / "downloads").iterdir()) == []


@pytest.mark.parametrize("configured_version", ["2.3.4", "v2.3.4"])
def test_explicit_version_bypasses_latest_lookup_and_normalizes_leading_v(
    tmp_path: Path, configured_version: str
) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    env["VERSION"] = configured_version
    env["TEST_LATEST_FAIL"] = "1"

    result = run_installer(tmp_path, env)

    assert result.returncode == 0, result.stderr
    urls = Path(env["TEST_CURL_LOG"]).read_text().splitlines()
    assert len(urls) == 2
    assert all("/releases/latest" not in url for url in urls)
    assert all("/releases/download/v2.3.4/" in url for url in urls)
    assert urls[1] == f"{urls[0]}.sha256"
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "new"
    )
    assert (lib_dir / "new-only").read_text() == "new payload\n"


def test_unset_version_uses_latest_release_tag(tmp_path: Path) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    env.pop("VERSION")
    env["TEST_LATEST_URL"] = (
        "https://github.com/chauduyphanvu/audible-deals/releases/tag/v2.4.5"
    )

    result = run_installer(tmp_path, env)

    assert result.returncode == 0, result.stderr
    urls = Path(env["TEST_CURL_LOG"]).read_text().splitlines()
    assert urls[0] == ("https://github.com/chauduyphanvu/audible-deals/releases/latest")
    assert len(urls) == 3
    assert all("/releases/download/v2.4.5/" in url for url in urls[1:])
    assert urls[2] == f"{urls[1]}.sha256"
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "new"
    )
    assert (lib_dir / "new-only").read_text() == "new payload\n"


@pytest.mark.parametrize(
    ("transport_failure", "latest_url"),
    [
        (
            True,
            "https://github.com/chauduyphanvu/audible-deals/releases/tag/v2.4.5",
        ),
        (False, ""),
        (
            False,
            "https://example.com/chauduyphanvu/audible-deals/releases/tag/v2.4.5",
        ),
        (
            False,
            "https://github.com/chauduyphanvu/audible-deals/releases/download/v2.4.5",
        ),
        (
            False,
            "https://github.com/chauduyphanvu/audible-deals/releases/tag/not-a-version",
        ),
        (
            False,
            "https://github.com/chauduyphanvu/audible-deals/releases/tag/v2.4.5/notes",
        ),
        (
            False,
            "https://github.com/chauduyphanvu/audible-deals/releases/tag/v2.4.5?source=latest",
        ),
    ],
    ids=[
        "transport-error",
        "empty-url",
        "wrong-host",
        "wrong-path",
        "invalid-tag",
        "trailing-component",
        "query-string",
    ],
)
def test_unset_version_resolution_failure_preserves_existing_install(
    tmp_path: Path, transport_failure: bool, latest_url: str
) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    env.pop("VERSION")
    env["TEST_LATEST_URL"] = latest_url
    if transport_failure:
        env["TEST_LATEST_FAIL"] = "1"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert result.stderr.strip() == (
        "Error: Could not resolve the latest release from GitHub; "
        "retry or set VERSION explicitly."
    )
    assert Path(env["TEST_CURL_LOG"]).read_text().splitlines() == [
        "https://github.com/chauduyphanvu/audible-deals/releases/latest"
    ]
    assert_old_install_works(lib_dir, link)
    assert (lib_dir / "old-only").read_text() == "old payload"
    assert (lib_dir / INSTALL_MARKER_NAME).read_text() == INSTALL_MARKER_CONTENT
    assert_no_temporary_files(tmp_path, lib_dir)


def test_download_failure_preserves_install(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(tmp_path)
    env["TEST_DOWNLOAD_FAIL"] = "1"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "Download failed" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_checksum_mismatch_preserves_install(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(tmp_path)
    Path(env["TEST_CHECKSUM"]).write_text(f"{'0' * 64}  release.tar.gz\n")

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "Checksum mismatch" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_corrupt_archive_preserves_install(tmp_path: Path) -> None:
    lib_dir, install_dir, link, archive, env = install_fixture(tmp_path)
    archive.write_bytes(b"not a tar archive")
    write_checksum(archive, Path(env["TEST_CHECKSUM"]))

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "Could not extract" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_missing_binary_preserves_install(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(
        tmp_path, {"not-deals": b"wrong file\n"}
    )

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "does not contain the expected deals binary" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_invalid_executable_preserves_install(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(
        tmp_path, {"deals": b"\x00corrupt executable bytes\n"}
    )

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "failed its staged --version check" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_successful_upgrade_is_clean(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(tmp_path)

    result = run_installer(tmp_path, env)

    assert result.returncode == 0, result.stderr
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "new"
    )
    assert link.is_symlink()
    assert os.readlink(link) == str(lib_dir / "deals")
    assert not (lib_dir / "old-only").exists()
    assert (lib_dir / "new-only").read_text() == "new payload\n"
    assert (lib_dir / INSTALL_MARKER_NAME).read_text() == INSTALL_MARKER_CONTENT
    assert_no_temporary_files(tmp_path, lib_dir)


def test_regular_command_file_is_rejected_and_preserved(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(tmp_path)
    link.unlink()
    write_executable(link, "user command")

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "unmanaged file already exists" in result.stderr
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "user command"
    )
    assert (lib_dir / "old-only").read_text() == "old payload"
    assert_no_temporary_files(tmp_path, lib_dir)


def test_foreign_command_symlink_is_rejected_and_preserved(tmp_path: Path) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(tmp_path)
    foreign = tmp_path / "foreign" / "deals"
    foreign.parent.mkdir()
    write_executable(foreign, "foreign command")
    link.unlink()
    link.symlink_to(foreign)

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "foreign symlink already exists" in result.stderr
    assert os.readlink(link) == str(foreign)
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "foreign command"
    )
    assert (lib_dir / "old-only").read_text() == "old payload"
    assert_no_temporary_files(tmp_path, lib_dir)


FINAL_VERIFICATION_FAILURE = b"""#!/bin/sh
case "$0" in
    */.deals-install.*/payload/deals) exit 0 ;;
    *) exit 42 ;;
esac
"""


def test_final_link_verification_failure_restores_existing_install(
    tmp_path: Path,
) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(
        tmp_path, {"deals": FINAL_VERIFICATION_FAILURE}
    )

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "failed its final --version check" in result.stderr
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_final_link_verification_failure_removes_fresh_link(
    tmp_path: Path,
) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(
        tmp_path, {"deals": FINAL_VERIFICATION_FAILURE}
    )
    link.unlink()

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert "failed its final --version check" in result.stderr
    assert not link.exists()
    assert not link.is_symlink()
    assert (
        subprocess.run(
            [str(lib_dir / "deals")], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert (lib_dir / "old-only").read_text() == "old payload"
    assert_no_temporary_files(tmp_path, lib_dir)


def assert_transaction_swap_failure_rolls_back(tmp_path: Path, mode: str) -> None:
    lib_dir, install_dir, link, _, env = install_fixture(tmp_path)
    make_mv_shim(tmp_path / "shims")
    env["MV_FAIL_MODE"] = mode

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_failed_library_swap_rolls_back(tmp_path: Path) -> None:
    assert_transaction_swap_failure_rolls_back(tmp_path, "promotion")


def test_backup_post_move_failure_rolls_back(tmp_path: Path) -> None:
    assert_transaction_swap_failure_rolls_back(tmp_path, "backup_post_move_failure")


def test_promotion_post_move_failure_rolls_back(tmp_path: Path) -> None:
    assert_transaction_swap_failure_rolls_back(tmp_path, "promotion_post_move_failure")


def test_direct_link_creation_failure_rolls_back_library(tmp_path: Path) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    link.unlink()
    make_ln_shim(tmp_path / "shims")
    env["LN_FAIL_MODE"] = "failure"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert not link.exists()
    assert not link.is_symlink()
    assert (
        subprocess.run(
            [str(lib_dir / "deals")], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert_no_temporary_files(tmp_path, lib_dir)


def test_link_post_create_failure_preserves_ambiguous_link_and_rolls_back(
    tmp_path: Path,
) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    link.unlink()
    make_ln_shim(tmp_path / "shims")
    env["LN_FAIL_MODE"] = "post_create_failure"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert link.is_symlink()
    assert os.readlink(link) == str(lib_dir / "deals")
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert_no_temporary_files(tmp_path, lib_dir)


def test_concurrent_exact_command_link_is_preserved(tmp_path: Path) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    link.unlink()
    make_ln_shim(tmp_path / "shims")
    env["LN_FAIL_MODE"] = "concurrent_exact_symlink"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert link.is_symlink()
    assert os.readlink(link) == str(lib_dir / "deals")
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert_no_temporary_files(tmp_path, lib_dir)


@pytest.mark.parametrize("path_kind", ["regular", "foreign_symlink"])
def test_concurrent_command_path_creation_is_not_clobbered(
    tmp_path: Path, path_kind: str
) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    link.unlink()
    make_ln_shim(tmp_path / "shims")
    env["LN_FAIL_MODE"] = f"concurrent_{path_kind}"

    if path_kind == "foreign_symlink":
        foreign = tmp_path / "foreign" / "deals"
        foreign.parent.mkdir()
        write_executable(foreign, "concurrent command")
        env["TEST_FOREIGN_COMMAND"] = str(foreign)

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert (
        subprocess.run(
            [str(link)], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "concurrent command"
    )
    if path_kind == "foreign_symlink":
        assert os.readlink(link) == str(foreign)
    else:
        assert not link.is_symlink()
    assert (
        subprocess.run(
            [str(lib_dir / "deals")], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert (lib_dir / "old-only").read_text() == "old payload"
    assert_no_temporary_files(tmp_path, lib_dir)


def test_interrupt_after_backup_move_rolls_back(tmp_path: Path) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    make_mv_shim(tmp_path / "shims")
    env["MV_FAIL_MODE"] = "signal_after_backup"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    assert_old_install_works(lib_dir, link)
    assert_no_temporary_files(tmp_path, lib_dir)


def test_failed_restore_retains_backup(tmp_path: Path) -> None:
    lib_dir, _, link, _, env = install_fixture(tmp_path)
    make_mv_shim(tmp_path / "shims")
    env["MV_FAIL_MODE"] = "promotion_restore"

    result = run_installer(tmp_path, env)

    assert result.returncode != 0
    match = re.search(r"backup was preserved at:\n  (.+/backup)\n", result.stderr)
    assert match, result.stderr
    backup = Path(match.group(1))
    assert backup.is_dir()
    assert (
        subprocess.run(
            [str(backup / "deals")], text=True, capture_output=True, check=True
        ).stdout.strip()
        == "old"
    )
    assert not lib_dir.exists()
    assert link.is_symlink()
    assert backup.parent.is_dir()
