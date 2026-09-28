import os
import uuid

import pytest

from ramen_tests import env as E  # ramen_tests before grpc: sets GRPC_ENABLE_FORK_SUPPORT
from ramen_tests import state
from ramen_tests.console import Console
from ramen_tests.mcp_client import Node, code_of


@pytest.fixture(scope="session")
def node_url() -> str:
    """gRPC target `host:port` (CONTRACTS §11) parsed from RAMEN_NODE_URL."""
    return E.node_target(E.require("RAMEN_NODE_URL"))[0]


@pytest.fixture(scope="session")
def mcp_key() -> str:
    """Key minted by the e2e deploy in this process wins; else RAMEN_MCP_KEY; else skip."""
    k = state.MCP_KEY or E.env("RAMEN_MCP_KEY")
    if not k:
        pytest.skip("RAMEN_MCP_KEY not set and no key minted by e2e")
    return k


@pytest.fixture(scope="session")
def node(node_url, mcp_key) -> Node:
    """Authenticated gRPC client (bearer + ramen-group/ramen-zone metadata) against RAMEN_NODE_URL."""
    with Node.from_env(mcp_key) as n:
        yield n


@pytest.fixture(scope="session")
def node_admin(node) -> Node:
    """The same client when the Admin surface is reachable and RAMEN_ADMIN_KEY is set; skips otherwise
    (through an LB the worker's RAMEN_ADMIN_CIDRS usually exclude external callers → PERMISSION_DENIED)."""
    if not node.admin_key:
        pytest.skip("RAMEN_ADMIN_KEY not set")
    code = code_of(node.admin_metrics)
    if code.name != "OK":
        pytest.skip(f"Admin/Metrics not reachable with RAMEN_ADMIN_KEY from here: {code.name}")
    return node


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
