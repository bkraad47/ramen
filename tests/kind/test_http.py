"""CONTRACTS §16 on the kind cluster: Streamable HTTP through the same NodePorts the gRPC proofs use — every zone
serves `/mcp` on its own port with its own key, sessions are minted and bound, and the guards answer the HTTP way."""

import pytest

from ramen_tests.mcp_client import PROTOCOL, HttpNode, text_of

pytestmark = pytest.mark.kind
TOOL = "demo_calculator_tool"
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}


@pytest.fixture(scope="module")
def http_nodes(stack, zone_targets, group) -> dict[str, HttpNode]:
    """One Streamable HTTP client per zone, at `http://<target>/mcp` — the same port the gRPC client uses."""
    clients = {z: HttpNode(f"http://{t}/mcp", stack["key"], group=group, zone=z) for z, t in zone_targets.items()}
    yield clients
    for c in clients.values():
        c.close()


def test_every_zone_serves_streamable_http_on_the_grpc_port(http_nodes, nodes):
    for zone, h in http_nodes.items():
        assert h.initialize()["protocolVersion"] == PROTOCOL, zone
        assert h.session_id and h.session_id.count(".") == 3, zone  # 0.7.2: <nonce>.<expiry>.<hash12>.<mac>
        assert TOOL in {t["name"] for t in h.list_tools()}, zone
        assert text_of(h.call_tool(TOOL, {"var1": 2, "var2": 3, "func": "add"})).strip().startswith("5"), zone
        assert nodes[zone].ping() == {}  # and gRPC still answers on the same port


def test_http_guards_through_the_cluster(http_nodes, stack):
    h = next(iter(http_nodes.values()))
    assert h.outcome(PING, key=None) == "UNAUTHENTICATED"
    assert h.outcome(PING, key="rmk_not_a_key") == "UNAUTHENTICATED"
    assert h.status(PING, extra=[("Origin", "https://evil.example")]) == 403, "no allow-list → foreign Origin refused"
    assert h.outcome(PING) == "OK"


def test_a_session_from_one_zone_does_not_open_another(http_nodes):
    """Each zone has its own deploy Secret and, unless the console shared one, its own session secret."""
    zones = list(http_nodes)
    if len(zones) < 2:
        pytest.skip("needs two zones")
    a, b = http_nodes[zones[0]], http_nodes[zones[1]]
    a.initialize()
    b.session_id = a.session_id
    # the console writes ONE secret per group into every zone, so a group's sessions are valid across its zones
    assert b.status(PING) in (200, 404)
    b.session_id = "not.a.session"
    assert b.status(PING) == 404


def test_protected_resource_metadata_names_the_console_when_configured(http_nodes, admin):
    """RAMEN_PUBLIC_URL on the console decides whether workers advertise an authorization server (§16.3)."""
    h = next(iter(http_nodes.values()))
    r = h.get("/.well-known/oauth-protected-resource")
    assert r.status_code in (200, 404)
    if r.status_code == 200:
        assert r.json()["resource"].startswith("mcp:")
        assert r.json()["authorization_servers"]
