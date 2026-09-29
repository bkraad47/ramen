"""Lists environment variable NAMES (never values) visible to tool code (tests/fixtures/slow_group)."""

import json
import os


def probe(prefix: str) -> str:
    return json.dumps(sorted(k for k in os.environ if k.startswith(prefix)))
