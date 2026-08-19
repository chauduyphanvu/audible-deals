"""Tests that current user guidance uses the canonical personalized command."""

from pathlib import Path


def test_current_guidance_uses_for_me():
    root = Path(__file__).parent.parent
    guidance = [
        root / "README.md",
        root / "docs" / "index.md",
        root / "docs" / "commands.md",
        root / "docs" / "automation.md",
    ]
    for path in guidance:
        assert "for-you" not in path.read_text(), path
