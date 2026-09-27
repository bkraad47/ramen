from pathlib import Path

from ramen_runtime import __version__


def test_version_matches_repo_version_file():
    assert __version__ == (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
