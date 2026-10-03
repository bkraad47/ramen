#!/usr/bin/env python3
"""Every site URL (https://bkraad47.github.io/ramen/...) in README.md, docs/llms.txt and docs/index.md must map to a
page under docs/, and every relative link in README.md must exist in the repo. Offline; pages were deleted in the
0.6.0 docs rebuild and llms.txt is what LLM agents read first.
Usage: check_docs_links.py [--root <ramen>]   exit 1 on any dead link."""

import argparse
import re
import sys
from pathlib import Path

SITE = "https://bkraad47.github.io/ramen/"
FILES = ("README.md", "docs/llms.txt", "docs/index.md")
LINK = re.compile(r"\]\(([^)\s]+)\)|(?:href|src)=\"([^\"]+)\"")


def site_page(docs: Path, url: str) -> bool:
    path = url.removeprefix(SITE).split("#")[0].strip("/")
    if not path:
        return (docs / "index.md").exists()
    if "." in path.rsplit("/", 1)[-1]:  # a file such as llms.txt or img/logo.png
        return (docs / path).exists()
    return (docs / f"{path}.md").exists() or (docs / path / "index.md").exists()


def check(root: Path) -> list[str]:
    bad = []
    for rel in FILES:
        f = root / rel
        if not f.exists():
            bad.append(f"{rel}: missing")
            continue
        for m in LINK.finditer(f.read_text()):
            url = m.group(1) or m.group(2)
            if url.startswith(SITE):
                if not site_page(root / "docs", url):
                    bad.append(f"{rel}: {url} has no page under docs/")
            elif rel == "README.md" and not re.match(r"^[a-z]+:|#|mailto:", url):
                if not (root / url.split("#")[0]).exists():
                    bad.append(f"{rel}: {url} does not exist")
    return bad


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=Path(__file__).resolve().parents[1], type=Path)
    bad = check(ap.parse_args().root)
    if bad:
        print("DEAD LINKS:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print("docs links ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
