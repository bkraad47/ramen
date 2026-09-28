#!/usr/bin/env python3
"""Aggregate per-component coverage into one markdown table with a >=90% verdict (CLAUDE.md gate).
Reads Cobertura XML (pytest-cov, cargo llvm-cov --cobertura, tarpaulin) or lcov .info (cargo llvm-cov --lcov).
Usage: coverage_report.py [--root <ramen>] [--artifacts <dir>] [--min 90] [--out report.md]
       [--allow-missing] [--no-fail]
Extra files: --file name=path. Also appends to $GITHUB_STEP_SUMMARY when set."""

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

CANDIDATES = {
    "console": ["console/coverage.xml"],
    "runtime-py": ["runtime-py/coverage.xml"],
    "node-rs": [
        "node-rs/coverage.xml",
        "node-rs/cobertura.xml",
        "node-rs/lcov.info",
        "node-rs/target/llvm-cov/lcov.info",
    ],
    "tests": ["tests/coverage.xml"],
}


def parse(path: Path) -> tuple[int, int]:
    """→ (covered, valid) lines."""
    if path.suffix == ".info":
        lf = lh = 0
        for line in path.read_text().splitlines():
            if line.startswith("LF:"):
                lf += int(line[3:])
            elif line.startswith("LH:"):
                lh += int(line[3:])
        return lh, lf
    root = ET.parse(path).getroot()
    if root.get("lines-valid") is not None:
        return int(root.get("lines-covered", 0)), int(root.get("lines-valid", 0))
    seen: dict[tuple[str, str], bool] = {}
    for cls in root.iter("class"):
        for ln in cls.iter("line"):
            key = (cls.get("filename", ""), ln.get("number", ""))
            seen[key] = seen.get(key, False) or int(ln.get("hits", 0)) > 0
    return sum(seen.values()), len(seen)


def find(name: str, rels: list[str], root: Path, artifacts: Path | None) -> Path | None:
    for rel in rels:
        p = root / rel
        if p.exists():
            return p
    if artifacts and artifacts.exists():
        for d in sorted(artifacts.glob(f"*{name}*")):
            for pat in ("**/*.xml", "**/*.info"):
                hits = sorted(d.glob(pat))
                if hits:
                    return hits[0]
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    ap.add_argument("--artifacts", type=Path, help="dir of downloaded CI artifacts (coverage-<component>/...)")
    ap.add_argument("--min", type=float, default=90.0)
    ap.add_argument("--out", type=Path)
    ap.add_argument("--file", action="append", default=[], help="name=path")
    ap.add_argument("--allow-missing", action="store_true", help="missing report → skip instead of fail")
    ap.add_argument("--no-fail", action="store_true", help="always exit 0")
    ap.add_argument(
        "--info", action="append", default=["tests"], help="component reported but not gated (harness helper code)"
    )
    a = ap.parse_args()

    sources = {k: find(k, v, a.root, a.artifacts) for k, v in CANDIDATES.items()}
    for spec in a.file:
        n, _, p = spec.partition("=")
        sources[n] = Path(p)

    rows, failed = [], []
    for name, path in sources.items():
        if path is None or not path.exists():
            rows.append((name, "-", "-", "-", "MISSING" if not a.allow_missing else "skipped", "no report"))
            if not a.allow_missing:
                failed.append(name)
            continue
        cov, valid = parse(path)
        pct = 100.0 * cov / valid if valid else 0.0
        verdict = "INFO" if name in a.info else ("PASS" if pct >= a.min else "FAIL")
        if verdict == "FAIL":
            failed.append(name)
        rows.append(
            (
                name,
                str(cov),
                str(valid),
                f"{pct:.1f}%",
                verdict,
                str(path.relative_to(a.root) if path.is_relative_to(a.root) else path),
            )
        )

    overall = "PASS" if not failed else "FAIL"
    md = [
        f"## Coverage report (gate >= {a.min:g}%) — **{overall}**",
        "",
        "| Component | Covered | Lines | % | Verdict | Source |",
        "|---|---:|---:|---:|---|---|",
    ]
    md += [f"| {r[0]} | {r[1]} | {r[2]} | {r[3]} | {r[4]} | `{r[5]}` |" for r in rows]
    if failed:
        md += ["", f"Below gate or missing: {', '.join(failed)}"]
    text = "\n".join(md) + "\n"
    print(text)
    if a.out:
        a.out.write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write(text)
    return 0 if (a.no_fail or overall == "PASS") else 1


if __name__ == "__main__":
    sys.exit(main())
