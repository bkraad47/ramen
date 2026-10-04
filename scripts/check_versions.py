#!/usr/bin/env python3
"""CONTRACTS §6: VERSION is the single source; Cargo.toml, every pyproject, the Helm charts (version/appVersion and
image tags), mkdocs `version_current`, the Dockerfile ARG defaults and the compose/.env defaults must equal it.
Usage: check_versions.py [--root <ramen>] [--tag vX.Y.Z]   exit 1 on any mismatch."""

import argparse
import re
import sys
import tomllib
from pathlib import Path

REQUIRED = {
    "node-rs/Cargo.toml": ("package", "version"),
    "console/pyproject.toml": ("project", "version"),
    "runtime-py/pyproject.toml": ("project", "version"),
}
OPTIONAL = {"tests/pyproject.toml": ("project", "version")}
# Non-TOML places (regex, all matches must equal VERSION); missing files are reported and skipped.
TEXT = {
    "deploy/helm/ramen/Chart.yaml": r"^(?:version|appVersion):\s*\"?([^\s\"]+)",
    "deploy/helm/ramen-worker/Chart.yaml": r"^(?:version|appVersion):\s*\"?([^\s\"]+)",
    "deploy/helm/ramen/values.yaml": r"^\s*tag:\s*\"?([^\s\"#]+)",
    "deploy/helm/ramen-worker/values.yaml": r"^\s*tag:\s*\"?([^\s\"#]+)",
    "mkdocs.yml": r"^\s*version_current:\s*\"?([^\s\"]+)",
    # the kind console's image tag and the worker image it hands zones: a 0.5.1 bump missed it and every kind
    # deploy then pulled a tag that no longer existed (ImagePullBackOff, CI kind job red)
    "deploy/kind/values.yaml": r"(?:^\s*tag:\s*|ramen-worker-kind:)\"?([^\s\"#]+)",
    "node-rs/Dockerfile": r"^ARG VERSION=(\S+)",
    "console/Dockerfile": r"^ARG RAMEN_VERSION=(\S+)",
    "deploy/local/.env.example": r"^VERSION=(\S+)",
    "deploy/local/docker-compose.yml": r"\$\{VERSION:-([^}]+)\}",
    # what LLM agents and search engines read: llms.txt summary line and the landing page's JSON-LD
    "docs/llms.txt": r"\bVersion (\d+\.\d+\.\d+\S*?)\.?$",
    "docs/index.md": r"\"(?:softwareVersion|version)\":\s*\"([^\"]+)\"",
    # the official MCP registry entry (io.github.bkraad47/ramen): a published version is immutable, so a release that
    # forgets this keeps the old listing as "latest"
    "server.json": r"^\s*\"version\":\s*\"([^\"]+)\"",
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")
# §14 W9: these derive __version__ from distribution metadata. A literal here is drift waiting to happen.
DERIVED = ("console/src/ramen_console/__init__.py", "runtime-py/src/ramen_runtime/__init__.py")
HARDCODED = re.compile(r'^__version__\s*=\s*[\'"]\d', re.M)


def read(path: Path, keys) -> str | None:
    if not path.exists():
        return None
    node = tomllib.loads(path.read_text())
    for k in keys:
        node = node.get(k, {})
    return node or None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    ap.add_argument("--tag", help="git tag to compare, e.g. v0.1.0")
    a = ap.parse_args()
    version = (a.root / "VERSION").read_text().strip()
    rows, bad = [("VERSION", version)], []
    if not SEMVER.match(version):
        bad.append(f"VERSION {version!r} is not semver")
    for rel, keys in {**REQUIRED, **OPTIONAL}.items():
        v = read(a.root / rel, keys)
        if v is None:
            if rel in REQUIRED:
                bad.append(f"{rel}: missing")
            rows.append((rel, "(missing)"))
            continue
        rows.append((rel, v))
        if v != version:
            bad.append(f"{rel}: {v} != {version}")
    for rel, pat in TEXT.items():
        path = a.root / rel
        if not path.exists():
            rows.append((rel, "(missing)"))
            continue
        found = sorted(set(re.findall(pat, path.read_text(), re.M)))
        rows.append((rel, ", ".join(found) or "(none)"))
        for v in found:
            if v != version:
                bad.append(f"{rel}: {v} != {version}")
    for rel in DERIVED:
        path = a.root / rel
        if not path.exists():
            bad.append(f"{rel}: missing")
            continue
        literal = HARDCODED.search(path.read_text())
        rows.append((rel, "(hard-coded)" if literal else "(from metadata)"))
        if literal:
            bad.append(f"{rel}: __version__ is hard-coded; derive it from the installed distribution")
    if a.tag:
        t = a.tag.removeprefix("v")
        rows.append(("tag", t))
        if t != version:
            bad.append(f"tag {t} != VERSION {version}")
    w = max(len(r[0]) for r in rows)
    for name, v in rows:
        print(f"{name:<{w}}  {v}")
    if bad:
        print("\nVERSION MISMATCH:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print(f"\nall versions == {version}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
