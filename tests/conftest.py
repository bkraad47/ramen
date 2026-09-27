import os
import uuid

import pytest

from ramen_tests import env as E
from ramen_tests import state
from ramen_tests.console import Console


@pytest.fixture(scope="session")
def node_url() -> str:
    return E.strip(E.require("RAMEN_NODE_URL"))


@pytest.fixture(scope="session")
def node_admin_url(node_url) -> str:
    """Bare node URL with /healthz, /readyz, /metrics, /admin. Skips when RAMEN_NODE_URL is an MCP-only LB route."""
    if not E.node_admin(node_url):
        pytest.skip(f"{node_url} is an MCP-only route (LB); health/metrics/admin are not exposed there")
    return node_url


@pytest.fixture(scope="session")
def mcp_key() -> str:
    """Key minted by the e2e deploy in this process wins; else RAMEN_MCP_KEY; else skip."""
    k = state.MCP_KEY or E.env("RAMEN_MCP_KEY")
    if not k:
        pytest.skip("RAMEN_MCP_KEY not set and no key minted by e2e")
    return k


@pytest.fixture(scope="session")
def console_url() -> str:
    return E.strip(E.require("RAMEN_CONSOLE_URL"))


@pytest.fixture(scope="session")
def admin_creds() -> tuple[str, str]:
    return E.env("RAMEN_ADMIN_EMAIL", "admin@ramen.local"), E.env("RAMEN_ADMIN_PASSWORD", "ramen-admin")


@pytest.fixture(scope="session")
def admin(console_url, admin_creds) -> Console:
    c = Console(console_url)
    r = c.login(*admin_creds)
    assert r.status_code in (200, 303), f"bootstrap login failed: {r.status_code} {r.text[:300]}"
    assert c.me().status_code == 200
    yield c
    c.close()


@pytest.fixture(scope="session")
def suffix() -> str:
    return os.environ.get("RAMEN_TEST_SUFFIX") or uuid.uuid4().hex[:6]


@pytest.fixture(scope="session")
def demo_repo() -> str:
    return E.env("RAMEN_DEMO_REPO", E.DEMO_REPO)
