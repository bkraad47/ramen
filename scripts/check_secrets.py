#!/usr/bin/env python3
"""No static secrets in the tree (0.7.6). Every password, Fernet key, session secret, admin key and MCP key that a
running process accepts is generated: `make env` for the compose stack, `deploy/kind/up.sh` for kind, a step in CI,
`secrets` in the test harness. This fails on
  1. a literal that once shipped as a default, anywhere in tracked text;
  2. a RAMEN_* secret variable given a literal value in deploy/, .github/ or scripts/ (an expression such as
     ${VAR:?…}, $(…), {{ … }}, a Terraform reference or an empty value is fine);
  3. a Fernet-shaped literal (43 url-safe base64 chars and '=') outside unit tests.
Run: python3 scripts/check_secrets.py   (also in `make lint` and the CI `versions` job)."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KNOWN = (
    "changeme-ramen",
    "local-admin-key",
    "local-mcp-key",
    "local-session-secret",
    "0VnINh_5EgyBnkUEf44BuR7CTKLrb97GSTjdjTbbkhY=",
    "Kind-Console-1!",
    "Kind-Admin-Key-1!",
    "kind-session-secret",
    "ci-mcp-key",
    "ci-admin-key",
    "Shots-Console-1!",
    "shots-only-secret",
    "console-cookie-signing",
    "conformance-secret",
)
SECRET_VAR = re.compile(
    r"\bRAMEN_(?:ADMIN_PASSWORD|FERNET_KEY|ADMIN_KEY|MCP_KEYS?|SESSION_SECRET|LOCAL_SESSION_SECRET)\b\s*[:=]\s*(.*)$"
)
FERNET = re.compile(r"(?<![A-Za-z0-9_/+-])[A-Za-z0-9_-]{43}=(?![A-Za-z0-9_=-])")
STRICT = ("deploy/", ".github/", "scripts/")
SKIP_SUFFIX = (".png", ".svg", ".avif", ".jpg", ".ico", ".lock", ".woff2")
SKIP_PREFIX = ("site/", "docs/img/", "docs/architecture/")  # architecture pages are history
NOT_FERNET_DIRS = ("console/tests/", "runtime-py/tests/", "tests/", "scripts/check_secrets.py")
REFERENCE = re.compile(r"""^["']?(?:\$|\{\{|<|\(|os\.environ|secrets\.|token_|[a-z_]+(?:\.[a-z_]+)+\b|["']\s*$)""")


def tracked() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    return [ROOT / f for f in out.split("\n") if f and not f.endswith(SKIP_SUFFIX) and not f.startswith(SKIP_PREFIX)]


def literal(value: str) -> bool:
    v = value.strip().rstrip(",").strip()
    if v.startswith("#") or "$(" in v or "${" in v:
        v = ""
    if v in ("", '""', "''"):
        return False
    return not REFERENCE.match(v)


def main() -> int:
    bad: list[str] = []
    for path in tracked():
        rel = path.relative_to(ROOT).as_posix()
        try:
            text = path.read_text()
        except UnicodeDecodeError, OSError:
            continue
        for n, line in enumerate(text.split("\n"), 1):
            for k in KNOWN:
                if k in line:
                    bad.append(f"{rel}:{n}: shipped default {k!r}")
            if (
                rel.startswith(STRICT)
                and not line.lstrip().startswith("#")
                and "$" not in line
                and (m := SECRET_VAR.search(line))
                and literal(m.group(1))
            ):
                bad.append(f"{rel}:{n}: literal secret value: {line.strip()[:90]}")
            if not rel.startswith(NOT_FERNET_DIRS) and FERNET.search(line) and "FERNET" in line.upper():
                bad.append(f"{rel}:{n}: Fernet-shaped literal")
    if bad:
        print("STATIC SECRETS:\n  " + "\n  ".join(bad), file=sys.stderr)
        return 1
    print("no static secrets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
