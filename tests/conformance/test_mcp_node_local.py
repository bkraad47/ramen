"""CONTRACTS §11 guards that need a specific node configuration, asserted on real `ramen-node` processes started on
free ports against tests/fixtures/demo_group (see ramen_tests.localnode). Skips unless the binary is built
(../node-rs/target/release/ramen-node or RAMEN_NODE_BIN) and ramen_runtime is importable. TLS case: RAMEN_TEST_TLS=1."""

import concurrent.futures as cf
import json
import shutil
import subprocess
import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, bridge_command, bridge_session, sdk_text, text_of

pytestmark = pytest.mark.conformance
S = grpc.StatusCode
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
ARGS = {"var1": 1, "var2": 2, "func": "add"}
LOG_FIELDS = {"ip", "group", "zone", "env", "method", "name", "status", "grpc_code", "ms", "key_id"}


@pytest.fixture(scope="module")
def plain():
    with LocalNode() as n:
        n.wait_serving()
        yield n


def test_health_not_serving_before_load_then_serving_after_reload(tmp_path):
    bucket = tmp_path / "bucket"
    with LocalNode(bucket=bucket) as n:  # bucket does not exist → runtime.load fails → NOT_SERVING
        assert n.wait_answering() == "NOT_SERVING", n.output()
        assert n.node.health("ramen.v1.Mcp") == "NOT_SERVING"
        assert n.node.health("ramen.v1.Admin") == "SERVING", "Admin health is liveness: always SERVING"
        shutil.copytree(E.FIXTURES / "demo_group", bucket)
        res = n.node.admin_reload()
        assert res["tools"] >= 1 if isinstance(res["tools"], int) else res["tools"], res
        n.wait_serving(60)
        assert n.node.health("ramen.v1.Mcp") == "SERVING"


def test_serving_node_answers_full_mcp_flow(plain):
    n = plain.node
    assert n.initialize()["protocolVersion"] == "2025-06-18"
    assert "demo_calculator_tool" in {t["name"] for t in n.list_tools()}
    assert float(text_of(n.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"}))) == 5
    assert "ramen-demo-mcp-group" in n.read_resource("ramen://demo/readme")["contents"][0]["text"]
    assert "2+3" in n.get_prompt("get_calculation_prompt", {"request": "2+3"})["messages"][0]["content"]["text"]


def test_auth_guards(plain):
    n = plain.node
    assert n.status(PING, key=None) == S.UNAUTHENTICATED
    assert n.status(PING, key="nope") == S.UNAUTHENTICATED
    assert n.status(PING, key=n.key + "x") == S.UNAUTHENTICATED  # prefix match is not a match
    assert n.status(PING, key=n.key[:-1]) == S.UNAUTHENTICATED
    assert n.status(PING) == S.OK


def test_empty_key_set_denies_all():
    with LocalNode(env={"RAMEN_MCP_KEYS": ""}) as n:
        n.wait_answering()
        assert n.node.status(PING, key="test-key") == S.UNAUTHENTICATED
        assert n.node.status(PING, key="") == S.UNAUTHENTICATED


def test_cidr_denied_outside_allowed_range():
    with LocalNode(env={"RAMEN_ALLOWED_CIDRS": "192.0.2.0/24"}) as n:
        n.wait_answering()
        assert n.node.status(PING) == S.PERMISSION_DENIED
        assert n.node.health() in ("SERVING", "NOT_SERVING"), "health is unauthenticated and not CIDR-gated"
        spoof = [("x-forwarded-for", "192.0.2.7")]
        assert n.node.status(PING, extra=spoof) == S.PERMISSION_DENIED, "x-forwarded-for ignored w/o RAMEN_TRUST_PROXY"


def test_trust_proxy_uses_forwarded_address():
    with LocalNode(env={"RAMEN_ALLOWED_CIDRS": "192.0.2.0/24", "RAMEN_TRUST_PROXY": "1"}) as n:
        n.wait_serving()
        assert n.node.status(PING, extra=[("x-forwarded-for", "192.0.2.7")]) == S.OK
        assert n.node.status(PING, extra=[("x-forwarded-for", "198.51.100.9")]) == S.PERMISSION_DENIED
        assert n.node.status(PING) == S.PERMISSION_DENIED  # no header → peer address (127.0.0.1) → denied


def test_admin_cidr_and_key():
    with LocalNode(env={"RAMEN_ADMIN_CIDRS": "192.0.2.0/24"}) as n:
        n.wait_serving()
        code = None
        try:
            n.node.admin_metrics()
        except grpc.RpcError as e:
            code = e.code()
        assert code == S.PERMISSION_DENIED
    with LocalNode() as n:
        n.wait_serving()
        for bad in (None, "nope"):
            try:
                n.node.admin_reload(admin_key=bad)
                raise AssertionError(f"Admin/Reload accepted key {bad!r}")
            except grpc.RpcError as e:
                assert e.code() == S.UNAUTHENTICATED
        m = n.node.admin_metrics()
        assert m["sidecar_alive"] is True and m["packages"]["tools"] >= 1
        assert {"tools", "resources", "prompts", "errors"} <= set(n.node.admin_reload())


def test_blocked_names_hidden_and_32601():
    with LocalNode(env={"RAMEN_BLOCKED": "demo_calculator_tool,get_calculation_prompt"}) as n:
        n.wait_serving()
        assert "demo_calculator_tool" not in {t["name"] for t in n.node.list_tools()}
        assert "get_calculation_prompt" not in {p["name"] for p in n.node.list_prompts()}
        with pytest.raises(JsonRpcError) as e:
            n.node.call_tool("demo_calculator_tool", {"var1": 1, "var2": 1, "func": "add"})
        assert e.value.code == -32601
        with pytest.raises(JsonRpcError) as e:
            n.node.get_prompt("get_calculation_prompt", {"request": "x"})
        assert e.value.code == -32601
        assert "ramen://demo/readme" in {r["uri"] for r in n.node.list_resources()}  # unrelated names untouched


def test_message_size_limit(plain):
    n = plain.node
    big = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (4 * 1024 * 1024 + 100)}}
    assert n.status(big) == S.OUT_OF_RANGE  # tonic codec limit; RESOURCE_EXHAUSTED is reserved for RAMEN_MAX_INFLIGHT
    assert n.status(PING) == S.OK


def test_notification_empty_body_and_invalid_json(plain):
    n = plain.node
    assert n.notify("notifications/initialized") == b""
    assert n.notify("notifications/cancelled", {"requestId": 1}) == b""
    # protocol errors are JSON-RPC error bodies under gRPC OK
    assert json.loads(n.call_raw(b"\xff\xfe not json"))["error"]["code"] == -32700
    assert json.loads(n.call_raw(json.dumps({"jsonrpc": "2.0", "id": 1}).encode()))["error"]["code"] == -32600


def test_max_inflight_yields_resource_exhausted_not_failure():
    with LocalNode(env={"RAMEN_MAX_INFLIGHT": "1"}) as n:
        n.wait_serving()
        clients = [n.client() for _ in range(12)]
        try:
            with cf.ThreadPoolExecutor(12) as ex:
                codes = list(
                    ex.map(
                        lambda c: c.status(
                            {
                                "jsonrpc": "2.0",
                                "id": 1,
                                "method": "tools/call",
                                "params": {"name": "demo_calculator_tool", "arguments": ARGS},
                            }
                        ),
                        clients,
                    )
                )
        finally:
            for c in clients:
                c.close()
        assert set(codes) <= {S.OK, S.RESOURCE_EXHAUSTED}, codes
        assert S.OK in codes
        assert n.node.health() == "SERVING"


def test_access_log_fields_and_no_key_values(plain):
    n = plain.node
    n.ping()
    n.status(PING, key="nope")
    time.sleep(0.3)
    text = plain.log.read_text()
    assert n.key not in text and "nope" not in text
    lines = plain.log_lines()
    ok = [x for x in lines if x.get("method") == "ping" and x.get("status") == "ok"]
    assert ok, lines[-3:]
    assert LOG_FIELDS <= set(ok[-1]), ok[-1]
    assert ok[-1]["grpc_code"] in (0, "OK") and ok[-1]["group"] == "demo" and ok[-1]["zone"] == "local"
    denied = [x for x in lines if x.get("grpc_code") in (16, "UNAUTHENTICATED")]
    assert denied and denied[-1]["status"] == "denied", "denied calls are logged with status=denied + grpc_code"


def test_verbose_logs_request_and_response():
    with LocalNode(env={"RAMEN_VERBOSE": "1"}) as n:
        n.wait_serving()
        n.node.request("ping", {"marker": "verbose-probe"})
        time.sleep(0.3)
        lines = [x for x in n.log_lines() if x.get("method") == "ping"]
        assert lines and "request" in lines[-1] and "response" in lines[-1], lines[-1:]
        assert "verbose-probe" in json.dumps(lines[-1])


def test_tls_mode_when_ramen_tls_set():
    if E.env("RAMEN_TEST_TLS") != "1":
        pytest.skip("set RAMEN_TEST_TLS=1 to start a node with RAMEN_TLS_CERT/KEY (self-signed via openssl)")
    with LocalNode(tls=True) as n:
        n.wait_serving()
        assert n.node.status(PING) == S.OK
        plain_client = n.client(tls=False, ca=None)
        try:
            assert plain_client.status(PING) in (S.UNAVAILABLE, S.INTERNAL, S.UNKNOWN), "plaintext must fail"
        finally:
            plain_client.close()


def test_reflection_lists_services(plain):
    assert {"ramen.v1.Mcp", "ramen.v1.Admin", "grpc.health.v1.Health"} <= set(plain.node.reflect())


def test_bridge_health_mode(plain):
    """`ramen-mcp-bridge --target … --health [SERVICE]` exits 0 on SERVING (used as the worker image HEALTHCHECK)."""
    cmd = bridge_command()
    if not cmd:
        pytest.skip("ramen-mcp-bridge not found")
    ok = subprocess.run([*cmd, "--target", plain.target, "--insecure", "--health"], capture_output=True, timeout=30)
    assert ok.returncode == 0, ok.stderr[-500:]
    admin = subprocess.run(
        [*cmd, "--target", plain.target, "--insecure", "--health", "ramen.v1.Admin"], capture_output=True, timeout=30
    )
    assert admin.returncode == 0, admin.stderr[-500:]
    dead = subprocess.run([*cmd, "--target", "127.0.0.1:1", "--insecure", "--health"], capture_output=True, timeout=30)
    assert dead.returncode != 0


async def test_bridge_end_to_end_on_local_node(plain):
    if not bridge_command():
        pytest.skip("ramen-mcp-bridge not found (RAMEN_BRIDGE_CMD / PATH / ../runtime-py/.venv)")
    async with bridge_session(plain.node) as s:
        assert "demo_calculator_tool" in {t.name for t in (await s.list_tools()).tools}
        r = await s.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
        assert not r.is_error and float(sdk_text(r)) == 5
