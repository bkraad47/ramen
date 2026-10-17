#!/usr/bin/env python3
"""Write deploy/local/.env from .env.example with fresh secrets (`make env`, run by `make up`).

Generated: RAMEN_ADMIN_PASSWORD, RAMEN_FERNET_KEY, RAMEN_MCP_KEYS, RAMEN_ADMIN_KEY, RAMEN_SESSION_SECRET. The example
carries no values for them, so a copied example never starts a console with a known password. An existing .env is
kept unless one of those is empty, shorter than a generated value or the Fernet key that shipped before 0.7.6; then
just those keys are rotated and the old file is left in .env.bak. Checked by scripts/check_secrets.py.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXAMPLE, ENV, BAK = HERE / ".env.example", HERE / ".env", HERE / ".env.bak"
KEYS = ("RAMEN_ADMIN_PASSWORD", "RAMEN_FERNET_KEY", "RAMEN_MCP_KEYS", "RAMEN_ADMIN_KEY", "RAMEN_SESSION_SECRET")
LEGACY_FERNET_SHA256 = "b03096c60becdd08c0abddb84c186a16e3ba8818d6e0dd5fc31b56f555b222cc"


def generate() -> dict[str, str]:
    return {
        "RAMEN_ADMIN_PASSWORD": secrets.token_hex(10) + "Aa1!",  # the console's policy: 12+, upper/lower/digit/special
        "RAMEN_FERNET_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "RAMEN_MCP_KEYS": "rmk_" + secrets.token_hex(16),
        "RAMEN_ADMIN_KEY": secrets.token_hex(24),
        "RAMEN_SESSION_SECRET": secrets.token_hex(32),
    }


def stale(value: str) -> bool:
    return len(value) < 24 or hashlib.sha256(value.encode()).hexdigest() == LEGACY_FERNET_SHA256


def fill(text: str, values: dict[str, str]) -> str:
    out = []
    for line in text.splitlines():
        key = line.split("=", 1)[0]
        if not line.startswith("#") and key in values:
            line = f"{key}={values[key]}"
        out.append(line)
    return "\n".join(out) + "\n"


def main() -> int:
    if ENV.exists():
        current = dict(
            line.split("=", 1) for line in ENV.read_text().splitlines() if "=" in line and not line.startswith("#")
        )
        rotate = [k for k in KEYS if stale(current.get(k, ""))]
        if not rotate:
            if ENV.stat().st_mtime < EXAMPLE.stat().st_mtime:
                print("note: deploy/local/.env is older than .env.example; new keys may be missing (diff them)")
            return 0
        shutil.copy(ENV, BAK)
        fresh = generate()
        ENV.write_text(fill(ENV.read_text(), {k: fresh[k] for k in rotate}))
        what = f"rotated {', '.join(rotate)} (old file: deploy/local/.env.bak)"
    else:
        ENV.write_text(fill(EXAMPLE.read_text(), generate()))
        what = "generated"
    os.chmod(ENV, 0o600)
    print(f"deploy/local/.env: {what}. Admin password: grep RAMEN_ADMIN_PASSWORD deploy/local/.env")
    return 0


if __name__ == "__main__":
    sys.exit(main())
