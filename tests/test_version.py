"""The release version lives in exactly one place: ``audible_deals.__version__``."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from click.testing import CliRunner

import audible_deals
from audible_deals.cli import cli

ROOT = Path(__file__).resolve().parents[1]
INIT_FILE = ROOT / "src" / "audible_deals" / "__init__.py"
RELEASE_WORKFLOW = ROOT / ".github" / "workflows" / "release.yml"
VERSION_LINE = re.compile(r'^__version__ = "[^"\n]+"$', re.MULTILINE)


def test_version_flag_prints_the_package_version() -> None:
    result = CliRunner().invoke(cli, ["--version"])

    assert result.exit_code == 0
    assert result.output == f"deals, version {audible_deals.__version__}\n"


def test_version_is_a_release_version() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+(-[A-Za-z0-9.]+)?", audible_deals.__version__)


def test_init_has_exactly_one_version_line() -> None:
    assert len(VERSION_LINE.findall(INIT_FILE.read_text(encoding="utf-8"))) == 1


def test_pyproject_takes_its_version_from_the_package() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    assert "version" not in config["project"]
    assert "version" in config["project"]["dynamic"]
    assert config["tool"]["hatch"]["version"]["path"] == (
        "src/audible_deals/__init__.py"
    )


def test_no_second_version_literal_ships_with_the_package() -> None:
    """A stale copy of the version is how ``--version`` and the wheel drift apart."""
    literal = re.escape(audible_deals.__version__)
    pattern = re.compile(rf"""["']{literal}["']""")
    offenders = [
        str(path.relative_to(ROOT))
        for path in sorted((ROOT / "src" / "audible_deals").rglob("*.py"))
        if path != INIT_FILE and pattern.search(path.read_text(encoding="utf-8"))
    ]

    assert offenders == []


def test_release_workflow_only_rewrites_the_package_version() -> None:
    workflow = RELEASE_WORKFLOW.read_text(encoding="utf-8")

    assert "src/audible_deals/__init__.py" in workflow
    assert "git add src/audible_deals/__init__.py" in workflow
    commands = [
        line.strip()
        for line in workflow.splitlines()
        if not line.strip().startswith("#")
    ]
    assert not [c for c in commands if "pyproject.toml" in c]
    assert not [c for c in commands if re.search(r"sed -i .*version = ", c)]


@pytest.mark.skipif(
    sys.platform != "linux" or shutil.which("bash") is None,
    reason="the release workflow runs GNU sed under bash on ubuntu-latest",
)
def test_release_workflow_version_step_updates_the_version_line(
    tmp_path: Path,
) -> None:
    lines = RELEASE_WORKFLOW.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if "Update version in src/" in line)
    run_at = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    script = []
    for line in lines[run_at + 1 :]:
        if line.strip() and not line.startswith(" " * 10):
            break
        script.append(line.strip())
    command = "\n".join(script).replace("src/audible_deals/__init__.py", "init.py")

    copy = tmp_path / "init.py"
    shutil.copy(INIT_FILE, copy)
    before = copy.read_text()
    subprocess.run(
        ["bash", "-euo", "pipefail", "-c", command],
        cwd=tmp_path,
        env={"VERSION": "9.8.7", "PATH": "/usr/bin:/bin"},
        check=True,
        capture_output=True,
    )

    after = copy.read_text()
    assert VERSION_LINE.sub('__version__ = "9.8.7"', before) == after
    assert '__version__ = "9.8.7"' in after
