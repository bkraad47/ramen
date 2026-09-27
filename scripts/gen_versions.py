#!/usr/bin/env python3
"""Generate docs/versions.md (version tracker) from CHANGELOG.md.

Standalone: `python3 scripts/gen_versions.py` (writes docs/versions.md).
MkDocs hook (mkdocs.yml `hooks:`): regenerates the page on every build.
Tag dates come from `git log -1 --format=%as v<version>` when the tag exists locally.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
OUT = ROOT / "docs" / "versions.md"
REPO = "https://github.com/bkraad47/ramen"
HEAD_RE = re.compile(r"^## \[(?P<ver>[^\]]+)\](?:\s*[—–-]\s*(?P<title>.*))?$")


def tag_date(version: str) -> str:
    try:
        out = subprocess.run(
            ["git", "log", "-1", "--format=%as", f"v{version}"],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout.strip()
        return out or "—"
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "—"


def parse(text: str) -> list[dict]:
    entries: list[dict] = []
    cur: dict | None = None
    for line in text.splitlines():
        m = HEAD_RE.match(line.strip())
        if m:
            cur = {"version": m["ver"], "title": (m["title"] or "").strip(), "items": []}
            entries.append(cur)
        elif cur is not None and line.strip().startswith("- "):
            cur["items"].append(line.strip()[2:])
    return entries


def render(entries: list[dict], current: str) -> str:
    rows = ["| Version | Date | Theme | Links |", "|---|---|---|---|"]
    for e in entries:
        v = e["version"]
        if v.lower() == "unreleased":
            continue
        arch = f"architecture/v{v}.md"
        links = [f"[release]({REPO}/releases/tag/v{v})"]
        if (ROOT / "docs" / arch).exists():
            links.append(f"[architecture]({arch})")
        status = " **(current)**" if v == current else ""
        rows.append(f"| `{v}`{status} | {tag_date(v)} | {e['title'] or '—'} | {' · '.join(links)} |")
    body = ["# Versions", "",
            "Generated from [`CHANGELOG.md`](https://github.com/bkraad47/ramen/blob/main/CHANGELOG.md) by "
            "`scripts/gen_versions.py`; do not edit by hand. Semver, `0.x` is pre-stable; each release is tagged "
            "`v<version>` and GitHub Actions attaches downloadable zips.", "", *rows, ""]
    for e in entries:
        if e["version"].lower() == "unreleased" and not e["items"]:
            continue
        body += [f"## {e['version']}" + (f" — {e['title']}" if e["title"] else ""), ""]
        body += [f"- {i}" for i in e["items"]] or ["- (nothing yet)"]
        body.append("")
    return "\n".join(body)


def generate() -> Path:
    current = (ROOT / "VERSION").read_text().strip()
    OUT.write_text(render(parse(CHANGELOG.read_text()), current))
    return OUT


def on_pre_build(config, **kwargs):  # mkdocs hook entry point
    generate()


if __name__ == "__main__":
    print(f"wrote {generate().relative_to(ROOT)}")
