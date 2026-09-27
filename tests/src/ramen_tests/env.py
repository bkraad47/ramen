"""Environment lookup. Missing env → pytest.skip, never a failure."""
import os
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parents[2]
RAMEN_DIR = TESTS_DIR.parent
FIXTURES = TESTS_DIR / "fixtures"
DEMO_REPO = "https://github.com/bkraad47/ramen-demo-mcp-group"


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def require(name: str) -> str:
    v = env(name)
    if v is None:
        pytest.skip(f"{name} not set")
    return v


def tls_verify() -> bool:
    return env("RAMEN_TLS_INSECURE", "0") != "1"


def strip(url: str) -> str:
    return url.rstrip("/")
