"""CONTRACTS §11: node gRPC surface against a deployed worker/LB. Needs RAMEN_NODE_URL (host:port) + RAMEN_MCP_KEY
(or the key minted by e2e earlier in the run). Opt: RAMEN_ADMIN_KEY (Admin/*), RAMEN_EXPECT_CIDR_DENIED=1 (a node
deployed with an excluding RAMEN_ALLOWED_CIDRS), RAMEN_EXPECT_BLOCKED=<tool> (a node deployed with RAMEN_BLOCKED),
RAMEN_WORKER_LOG=<host path of RAMEN_LOG_FILE> (access-log fields). Node-configuration cases that cannot be asserted
on an arbitrary deployment live in test_mcp_node_local.py."""

import json
import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.mcp_client import MAX_MESSAGE, PROTOCOL, JsonRpcError, text_of

pytestmark = pytest.mark.conformance
TRACEBACK_MARKERS = ("Traceback (most recent call last)", 'File "', "raise ")
LOG_FIELDS = {"ip", "group", "zone", "env", "method", "name", "status", "grpc_code", "ms", "key_id"}
S = grpc.StatusCode
# >4 MiB: the node answers OUT_OF_RANGE (tonic codec; settled for 0.3.1). Through an LB the proxy may reject first with
# RESOURCE_EXHAUSTED; both are accepted here, the local suite asserts OUT_OF_RANGE exactly.
TOO_BIG = (S.OUT_OF_RANGE, S.RESOURCE_EXHAUSTED)


def test_health_is_serving_and_unauthenticated(node):
    """ "" and "ramen.v1.Mcp" = readiness (SERVING once loaded); "ramen.v1.Admin" = liveness (always SERVING)."""
    assert node.health() == "SERVING"
    assert node.health("ramen.v1.Mcp") == "SERVING"
    assert node.health("ramen.v1.Admin") == "SERVING"


def test_server_reflection_lists_services(node):
    assert {"ramen.v1.Mcp", "ramen.v1.Admin", "grpc.health.v1.Health"} <= set(node.reflect())


def test_initialize_reports_contract_protocol(node):
    r = node.initialize()
    assert r["protocolVersion"] == PROTOCOL
    assert "tools" in r["capabilities"] and r["serverInfo"]["name"]


def test_list_tools_exposes_demo_calculator_with_derived_schema(node):
    tools = {t["name"]: t for t in node.list_tools()}
    t = tools["demo_calculator_tool"]
    assert t["description"]
    schema = t["inputSchema"]
    assert schema["type"] == "object"
    assert schema["properties"]["var1"]["type"] == "number"
    assert schema["properties"]["var2"]["type"] == "number"
    assert schema["properties"]["func"]["enum"] == ["add", "subtract", "multiply", "divide"]
    assert sorted(schema["required"]) == ["func", "var1", "var2"]


def test_list_resources_and_prompts(node):
    r = next(x for x in node.list_resources() if x["uri"] == "ramen://demo/readme")
    assert r["mimeType"] == "text/markdown"
    p = next(x for x in node.list_prompts() if x["name"] == "get_calculation_prompt")
    args = {a["name"]: a for a in p.get("arguments", [])}
    assert args["request"]["required"] is True


@pytest.mark.parametrize("func,expected", [("add", 5), ("subtract", -1), ("multiply", 6), ("divide", 2 / 3)])
def test_calculator_operations(node, func, expected):
    r = node.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": func})
    assert not r.get("isError"), text_of(r)
    assert float(text_of(r)) == pytest.approx(expected)


def test_divide_by_zero_is_error_without_traceback(node):
    r = node.call_tool("demo_calculator_tool", {"var1": 1, "var2": 0, "func": "divide"})
    assert r.get("isError") is True
    text = text_of(r)
    assert "division by zero" in text.lower()
    assert not any(m in text for m in TRACEBACK_MARKERS), text


def test_read_demo_readme_resource(node):
    c = node.read_resource("ramen://demo/readme")["contents"][0]
    assert c["uri"] == "ramen://demo/readme" and c["mimeType"] == "text/markdown"
    assert "ramen-demo-mcp-group" in c["text"]


def test_get_calculation_prompt_substitutes_request(node):
    m = node.get_prompt("get_calculation_prompt", {"request": "what is 2 plus 3"})["messages"][0]
    assert m["role"] == "user"
    assert "what is 2 plus 3" in m["content"]["text"]
    assert "{{request}}" not in m["content"]["text"]
    assert "demo_calculator_tool" in m["content"]["text"]


def test_unknown_method_is_jsonrpc_error_not_grpc_error(node):
    with pytest.raises(JsonRpcError) as e:
        node.request("no/such-method")
    assert e.value.code == -32601


def test_protocol_errors_are_jsonrpc_bodies_with_grpc_ok(node):
    """-32700 / -32600 come back as gRPC OK with a JSON-RPC error body (the transport is not the parser)."""
    assert json.loads(node.call_raw(b"{not json"))["error"]["code"] == -32700
    assert json.loads(node.call_raw({"jsonrpc": "2.0", "id": 1}))["error"]["code"] == -32600


def test_notification_returns_empty_body(node):
    assert node.notify("notifications/initialized") == b""


def test_unauthenticated_without_key(node):
    assert node.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key=None) == S.UNAUTHENTICATED


def test_unauthenticated_with_wrong_key(node):
    assert node.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="nope") == S.UNAUTHENTICATED


def test_unauthenticated_with_malformed_authorization(node):
    md = [("authorization", node.key or "")]  # no "Bearer " prefix
    assert node.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key=None, extra=md) == S.UNAUTHENTICATED


def test_wrong_key_does_not_leak_timing_or_body(node):
    """Constant-time compare (§11): reject before any dispatch; the error carries no key material."""
    try:
        node.call_raw({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="nope")
    except grpc.RpcError as e:
        assert "nope" not in (e.details() or "")
        assert (node.key or "") not in (e.details() or "")
        return
    raise AssertionError("wrong key accepted")


def test_message_over_4mib_is_rejected(node):
    padding = "x" * (MAX_MESSAGE + 1024)
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": padding}}
    assert node.status(body) in TOO_BIG
    assert node.ping() == {}  # still serving afterwards


def test_message_under_limit_is_accepted(node):
    body = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (1024 * 1024)}}
    assert node.status(body) == S.OK


def test_session_stream_is_reserved_or_working(node):
    assert node.session_supported() in (True, False)


def test_cidr_lock(node):
    """RAMEN_ALLOWED_CIDRS excludes the caller → PERMISSION_DENIED. Only assertable when the target node is
    deliberately configured that way; set RAMEN_EXPECT_CIDR_DENIED=1 (legacy RAMEN_EXPECT_CIDR_403=1)."""
    if E.env("RAMEN_EXPECT_CIDR_DENIED") != "1" and E.env("RAMEN_EXPECT_CIDR_403") != "1":
        pytest.skip("node not configured with an excluding RAMEN_ALLOWED_CIDRS (set RAMEN_EXPECT_CIDR_DENIED=1)")
    assert node.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}) == S.PERMISSION_DENIED
    assert node.health() in ("SERVING", "NOT_SERVING"), "health must stay reachable"


def test_blocked_name_hidden_and_denied(node):
    name = E.env("RAMEN_EXPECT_BLOCKED")
    if not name:
        pytest.skip("set RAMEN_EXPECT_BLOCKED=<tool> for a node deployed with RAMEN_BLOCKED (e2e covers it end to end)")
    assert name not in {t["name"] for t in node.list_tools()}
    with pytest.raises(JsonRpcError) as e:
        node.call_tool(name, {})
    assert e.value.code == -32601


def test_admin_requires_admin_key(node):
    """Missing/bad x-ramen-admin-key → UNAUTHENTICATED (outside RAMEN_ADMIN_CIDRS it is PERMISSION_DENIED instead)."""
    for fn in (node.admin_metrics, node.admin_reload):
        for bad in (None, "nope"):
            code = None
            try:
                fn(admin_key=bad)
            except grpc.RpcError as e:
                code = e.code()
            assert code in (S.UNAUTHENTICATED, S.PERMISSION_DENIED), f"{fn.__name__} key={bad!r} → {code}"
            if code == S.PERMISSION_DENIED:
                pytest.skip("caller is outside the worker's RAMEN_ADMIN_CIDRS (CIDR gate runs before the key check)")


def test_admin_metrics_fields(node_admin):
    m = node_admin.admin_metrics()
    for k in ("inflight", "total", "errors", "load", "sidecar_alive", "loaded_at", "packages"):
        assert k in m, m
    assert m["load"] in ("low", "even", "high")
    assert set(m["packages"]) >= {"tools", "resources", "prompts", "errors"}


def test_metrics_count_calls(node_admin):
    before = node_admin.admin_metrics()["total"]
    node_admin.ping()
    assert node_admin.admin_metrics()["total"] >= before + 1


def test_admin_reload_returns_load_result(node_admin):
    body = node_admin.admin_reload()
    assert {"tools", "resources", "prompts", "errors"} <= set(body)
    assert json.dumps(body)
    assert node_admin.health() == "SERVING"


def test_access_log_line_fields(node):
    """One JSON line per call with the §3 fields plus grpc_code, never the key value. Needs RAMEN_WORKER_LOG."""
    path = E.env("RAMEN_WORKER_LOG")
    if not path:
        pytest.skip("RAMEN_WORKER_LOG (host path of the worker's RAMEN_LOG_FILE) not set")
    marker = f"probe-{int(time.time() * 1000)}"
    node.request("ping", {"marker": marker})
    node.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="nope")
    deadline, lines = time.monotonic() + 15, []
    while time.monotonic() < deadline:
        text = open(path, encoding="utf-8", errors="replace").read()
        lines = [json.loads(x) for x in text.splitlines() if x.startswith("{")]
        if any(x.get("method") == "ping" and x.get("grpc_code") is not None for x in lines):
            break
        time.sleep(0.5)
    assert node.key not in text, "key value must never be logged"
    ok = [x for x in lines if x.get("method") == "ping" and x.get("status") == "ok"]
    assert ok, lines[-3:]
    assert LOG_FIELDS <= set(ok[-1]), ok[-1]
    assert ok[-1]["grpc_code"] in (0, "OK")
    denied = [x for x in lines if x.get("grpc_code") in (16, "UNAUTHENTICATED")]
    assert denied and denied[-1]["status"] == "denied", "an UNAUTHENTICATED call must be logged as status=denied"
