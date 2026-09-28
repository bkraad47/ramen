"""Demo resource: returns this group's README as text."""

from pathlib import Path


def read_readme() -> str:
    root = Path(__file__).resolve().parents[3]
    return (root / "README.md").read_text(encoding="utf-8")
