"""Environment lookup. Missing env → pytest.skip, never a failure."""
import os
from pathlib import Path
from urllib.parse import urlsplit

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


def mcp_url(base: str) -> str:
    """Full MCP endpoint. A node URL whose path already contains /mcp (e.g. an LB route
    https://<ip>/mcp/<group>/<zone>) is the endpoint itself; a bare node URL gets /mcp appended."""
    base = strip(base)
    return base if "/mcp" in urlsplit(base).path else base + "/mcp"


def node_admin(base: str) -> bool:
    """True when RAMEN_NODE_URL is a bare node (health/metrics/admin reachable); False for an MCP-only LB route."""
    return urlsplit(strip(base)).path in ("", "/")
