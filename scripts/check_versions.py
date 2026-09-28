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
    "node-rs/Dockerfile": r"^ARG VERSION=(\S+)",
    "console/Dockerfile": r"^ARG RAMEN_VERSION=(\S+)",
    "deploy/local/.env.example": r"^VERSION=(\S+)",
    "deploy/local/docker-compose.yml": r"\$\{VERSION:-([^}]+)\}",
}
SEMVER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?$")


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
