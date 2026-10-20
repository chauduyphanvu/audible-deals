"""Guards on what the documentation tells people to install."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL_SCRIPT = ROOT / "install.sh"


def test_docs_do_not_recommend_the_unpublished_pypi_package() -> None:
    documents = [
        ROOT / "README.md",
        ROOT / "CONTRIBUTING.md",
        INSTALL_SCRIPT,
        *sorted((ROOT / "docs").glob("*.md")),
    ]
    offenders = [
        str(path.relative_to(ROOT))
        for path in documents
        if re.search(
            r"(?:pip|pipx|uv tool|uv pip) install\s+audible-deals\b",
            path.read_text(encoding="utf-8"),
        )
    ]

    assert offenders == []
