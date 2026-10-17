#!/usr/bin/env python3
"""No static secrets in the tree (0.7.6). Every password, Fernet key, session secret, admin key and MCP key that a
running process accepts is generated: `make env` for the compose stack, `deploy/kind/up.sh` for kind, a step in CI,
`secrets` in the test harness. This fails on
  1. a literal that once shipped as a default, anywhere in tracked text (matched by sha256, so this file holds none);
  2. a RAMEN_* secret variable given a literal value in deploy/, .github/ or scripts/ (an expression such as
     ${VAR:?…}, $(…), {{ … }}, a Terraform reference or an empty value is fine);
  3. a Fernet-shaped literal (43 url-safe base64 chars and '=') outside unit tests.
Run: python3 scripts/check_secrets.py   (also in `make lint` and the CI `versions` job)."""

from __future__ import annotations

import hashlib
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# sha256 of each literal that once shipped as a default (the literals themselves must not appear here either);
# a line is split into tokens and each token is hashed. Add a hash with: python3 -c 'import hashlib,sys;print(hashlib.sha256(sys.argv[1].rstrip("=").encode()).hexdigest())' <literal>
KNOWN_SHA256 = frozenset(
    {
        "0a1e78ac529bb5ba79e173bf8d0c8386e314c51b5a0879cac4a9c7da15736993",
        "110d89f59698dace289ac7f0c1bc1ccc590800841011460cb0186d0fc12f1f56",
        "238eacfdb048eeeea5019f9a93d0a6c98ca287c883d6cc418d4c95cc9c8c5d5f",
        "3ea8f116156aeb81bc97881fb3ddf72fd09abfe763b658abc185626b9ef09c1a",
        "4ab7b7cd7a009307f975da639ffcb2f104e371d271e936dab005ee993474b81d",
        "4bc31557303bd14b0302bbc7880b2a710f682aba159bae7f4fb815b8cd068925",
        "58c9cdc58b80389bc95b81dde1aca7be2b95e424424efe88da7e876365c6dd3f",
        "8b74399c6be427799ebdde304b9fef5e55b21ab583347265132ba6e0d0ec7bf7",
        "98b53a07bcc0229b20bcb7200f6f4d259d92710fffa7de2793a76340c84ba6f5",
        "98e5a7549ae74ed8b755a82a3caf7e42afe3ec7ac8439e1e4312e47c61008a83",
        "a5e2ad0a9ecc1d4d43818d61f3adb9854d46fd4fa61d272acc1c0624c6691ffa",
        "a6452aaf264992a9ad37015a4fd5375ae17e4ed9df20beb631a85ece637d0578",
        "b4c19dae089a9893b2bedebb8e3cc401415f4af23cff36644253b8be774c524a",
        "ba33cf6349ad4f856f62d52f344e1e1bb741aa2d773dea8512e34b3c6eeaa28d",
        "d0d18cc0999e75e555481aad963ba46c6b8c8643a4f2cc8909d0a7dbcaa3f8d1",
    }
)
TOKEN = re.compile(r"""[\s"'=:,;()\[\]{}|<>`]+""")
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
            for tok in TOKEN.split(line):
                if tok and hashlib.sha256(tok.encode()).hexdigest() in KNOWN_SHA256:
                    bad.append(f"{rel}:{n}: a default that once shipped: {tok!r}")
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
