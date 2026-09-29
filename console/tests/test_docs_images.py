"""Docs images: every one the docs point at exists, and no screenshot is older than the UI it claims to show.

A stale screenshot is a documentation defect that nothing used to catch — `api-keys.png` showed a control that had
been replaced two releases earlier. `docs/img/shots.json` records which release each screenshot was captured for and
which UI contract it must match; a round that changes the console raises `ui_contract`, and this test then demands
fresh captures before the release can go out.
"""

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DOCS, IMG = ROOT / "docs", ROOT / "docs/img"
SHOTS = IMG / "shots.json"
REF = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)|<img[^>]+src=\"([^\"]+)\"")


def version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) if x.isdigit() else 0 for x in str(v).split(".")[:3])


def referenced() -> set[str]:
    """Every image path the docs and the README point at, normalised to a repo-relative path."""
    out = set()
    for md in [*DOCS.rglob("*.md"), ROOT / "README.md"]:
        for m in REF.finditer(md.read_text()):
            target = m.group(1) or m.group(2)
            if not target or target.startswith(("http://", "https://", "data:")):
                continue
            path = (md.parent / target).resolve()
            if path.is_relative_to(ROOT):
                out.add(str(path.relative_to(ROOT)))
    return out


def test_every_image_the_docs_point_at_exists():
    missing = sorted(p for p in referenced() if not (ROOT / p).exists())
    assert missing == []


def test_shots_json_covers_every_screenshot_the_docs_show():
    shots = json.loads(SHOTS.read_text())
    recorded = set(shots["screenshots"])
    shown = {Path(p).name for p in referenced() if p.startswith("docs/img/") and p.endswith(".png")}
    shown -= {"logo.png", "logo-mark.png", "favicon.png"}  # brand assets, not captures of the console
    assert shown - recorded == set(), "screenshots in the docs with no entry in shots.json"
    for name in recorded:
        assert (IMG / name).exists(), name


@pytest.mark.parametrize("name", sorted(json.loads(SHOTS.read_text())["screenshots"]) if SHOTS.exists() else [])
def test_no_screenshot_is_older_than_the_ui_it_shows(name):
    shots = json.loads(SHOTS.read_text())
    contract = shots["ui_contract"]
    entry = shots["screenshots"][name]
    assert version_tuple(entry["captured_for"]) >= version_tuple(contract), (
        f"{name} was captured for {entry['captured_for']}, but the console UI changed in {contract}: recapture it"
    )
    assert entry["page"], f"{name} does not say which page it shows"


def test_the_ui_contract_is_a_release_that_exists():
    shots = json.loads(SHOTS.read_text())
    version = (ROOT / "VERSION").read_text().strip()
    assert version_tuple(shots["ui_contract"]) <= version_tuple(version)
