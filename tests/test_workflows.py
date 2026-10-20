"""The CI and release workflows keep the promises the docs make about them."""

from __future__ import annotations

import os
import re
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"
SMOKE_SCRIPT = ROOT / ".github" / "scripts" / "smoke-bundle.sh"
SHA = re.compile(r"[0-9a-f]{40}")
USES = re.compile(r"^\s*(?:- )?uses:\s*(?P<action>\S+)@(?P<ref>\S+)(?P<rest>.*)$")


def text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def action_lines():
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(text(workflow).splitlines(), 1):
            match = USES.match(line)
            if match:
                yield workflow.name, number, match


def test_every_action_is_pinned_to_a_commit_except_the_pypi_placeholder() -> None:
    unpinned = []
    for name, number, match in action_lines():
        if SHA.fullmatch(match["ref"]):
            assert re.search(r"#\s*v\d", match["rest"]), (
                f"{name}:{number} needs the release tag in a comment"
            )
        else:
            unpinned.append((name, match["action"], "placeholder" in match["rest"]))

    # The one action whose commit could not be checked offline is spelled as an
    # obvious placeholder, and is the only thing allowed to be unpinned. Once
    # the owner pins it (CONTRIBUTING.md, "Publishing to PyPI") nothing is.
    assert unpinned in (
        [],
        [("release.yml", "pypa/gh-action-pypi-publish", True)],
    )


def test_the_pypi_job_uses_trusted_publishing_and_publishes_nothing_by_default() -> (
    None
):
    release = text(WORKFLOWS / "release.yml")
    job = release[release.index("\n  pypi:") :]

    assert "id-token: write" in job
    assert "name: pypi" in job
    assert "vars.PUBLISH_TO_PYPI == 'true'" in job
    assert "needs: [validate-and-tag, publish]" in job
    assert "password:" not in job and "secrets." not in job  # no stored token


def test_ci_covers_the_supported_pythons_and_the_other_platforms() -> None:
    ci = text(WORKFLOWS / "ci.yml")
    matrix = re.findall(r'os: (\S+), python-version: "([\d.]+)"', ci)

    assert [(os_, py) for os_, py in matrix if os_ == "ubuntu-latest"] == [
        ("ubuntu-latest", "3.11"),
        ("ubuntu-latest", "3.12"),
        ("ubuntu-latest", "3.13"),
        ("ubuntu-latest", "3.14"),
    ]
    assert ("macos-latest", "3.13") in matrix
    assert ("windows-latest", "3.13") in matrix
    assert "uv sync --locked" in ci and "uv sync --extra" not in ci


def test_every_tested_python_has_a_classifier() -> None:
    with (ROOT / "pyproject.toml").open("rb") as handle:
        classifiers = tomllib.load(handle)["project"]["classifiers"]
    versions = set(
        re.findall(r'python-version: "(3\.\d+)"', text(WORKFLOWS / "ci.yml"))
    )

    assert versions >= {"3.11", "3.12", "3.13", "3.14"}
    for version in versions:
        assert f"Programming Language :: Python :: {version}" in classifiers


def test_binaries_are_built_from_the_lock_with_the_tui_extra_and_smoke_tested() -> None:
    for name in ("release.yml", "ci.yml"):
        workflow = text(WORKFLOWS / name)
        build = workflow.index("pyinstaller deals.spec")
        before = workflow[:build]

        assert "uv sync --locked --extra tui" in before, name
        assert "pip install -e" not in workflow, name
        assert ".github/scripts/smoke-bundle.sh" in workflow[build:], name


def test_the_release_builds_all_three_binaries_with_a_smoke_test_each() -> None:
    release = text(WORKFLOWS / "release.yml")
    job = release[release.index("\n  build-binaries:") : release.index("\n  publish:")]

    for artifact in ("deals-linux-x64", "deals-macos-arm64", "deals-windows-x64"):
        assert f"artifact: {artifact}" in job
    # The smoke step runs on the matrix's every platform, before packaging.
    assert job.index("smoke-bundle.sh") < job.index("Package archive")
    assert "if:" not in job[: job.index("Package archive")]


def test_pyinstaller_is_a_build_tool_not_a_dependency() -> None:
    pyproject = text(ROOT / "pyproject.toml")
    lock = text(ROOT / "uv.lock")

    assert "pyinstaller" not in pyproject.lower()
    assert 'name = "pyinstaller"' not in lock


def test_the_smoke_script_runs_what_the_docs_promise() -> None:
    script = text(SMOKE_SCRIPT)

    for command in ("--version", "find --genre sci-fi --dry-run", "cache info"):
        assert re.search(rf"\brun {re.escape(command)}\b", script), command
    assert "textual" in script
    assert script.startswith("#!/usr/bin/env bash\n")


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX execute bit")
def test_the_smoke_script_is_executable() -> None:
    assert os.access(SMOKE_SCRIPT, os.X_OK)
