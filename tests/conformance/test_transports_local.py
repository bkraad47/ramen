"""CONTRACTS §16.5: every guard on BOTH transports, on real `ramen-node` processes, plus the HTTP-only cases.

The gRPC surface keeps its own module (test_mcp_node_local.py); this one runs the shared guard table through
`outcome()` on each transport so a check that drifts between them fails here first. Skips like its sibling: needs the
binary and an importable ramen_runtime."""

import json

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import (
    MAX_MESSAGE,
    PROTOCOL,
    HttpError,
    JsonRpcError,
    http_session,
    sdk_text,
    text_of,
)

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
ARGS = {"var1": 2, "var2": 3, "func": "add"}


@pytest.fixture(scope="module")
def plain():
    with LocalNode(env={"RAMEN_SESSION_SECRET": "conformance-secret"}) as n:
        n.wait_serving()
        yield n


@pytest.fixture(params=TRANSPORTS, ids=TRANSPORTS)
def client(request, plain):
    """The same node, once per transport."""
    return plain.for_transport(request.param)


# --- the shared guard table -------------------------------------------------------------------------------------------
def test_full_mcp_flow_on_each_transport(client):
    assert client.initialize()["protocolVersion"] == PROTOCOL
    assert "demo_calculator_tool" in {t["name"] for t in client.list_tools()}
    assert float(text_of(client.call_tool("demo_calculator_tool", ARGS))) == 5
    assert "ramen-demo-mcp-group" in client.read_resource("ramen://demo/readme")["contents"][0]["text"]
    assert "2+3" in client.get_prompt("get_calculation_prompt", {"request": "2+3"})["messages"][0]["content"]["text"]


def test_key_guard_is_identical_on_each_transport(client, plain):
    assert client.outcome(PING, key=None) == "UNAUTHENTICATED"
    assert client.outcome(PING, key="nope") == "UNAUTHENTICATED"
    assert client.outcome(PING, key=plain.key + "x") == "UNAUTHENTICATED"
    assert client.outcome(PING, key=plain.key[:-1]) == "UNAUTHENTICATED"
    assert client.outcome(PING) == "OK"


def test_protocol_errors_are_json_rpc_bodies_on_each_transport(client):
    """A JSON-RPC problem is never a transport error: OK on gRPC, 200 on HTTP, with the error object in the body."""
    assert client.outcome(b"not json") == "OK"
    out = json.loads(client.call_raw(b"not json"))
    assert out["error"]["code"] == -32700
    out = json.loads(client.call_raw({"jsonrpc": "2.0", "id": 9, "method": "no/such"}))
    assert out["error"]["code"] == -32601
    # an unknown TOOL is the runtime's -32004 (unknown METHODS and blocked names are the node's -32601): the same
    # code on both transports is what matters here
    with pytest.raises(JsonRpcError) as e:
        client.call_tool("no_such_tool", {})
    assert e.value.code == -32004


def test_notifications_produce_no_body_on_each_transport(client):
    assert client.notify("notifications/initialized") == b""


def test_message_limit_is_the_same_number_on_each_transport(client):
    pad = "x" * (MAX_MESSAGE + 1024)
    too_big = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": pad}}
    assert client.outcome(too_big) == "OUT_OF_RANGE"
    ok = {"jsonrpc": "2.0", "id": 1, "method": "ping", "params": {"pad": "x" * (MAX_MESSAGE // 2)}}
    assert client.outcome(ok) == "OK"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_empty_key_set_denies_all_on_each_transport(transport):
    with LocalNode(env={"RAMEN_MCP_KEYS": ""}) as n:
        n.wait_answering()
        c = n.for_transport(transport)
        assert c.outcome(PING, key="test-key") == "UNAUTHENTICATED"
        assert c.outcome(PING, key="") == "UNAUTHENTICATED"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_cidr_guard_on_each_transport(transport):
    with LocalNode(env={"RAMEN_ALLOWED_CIDRS": "192.0.2.0/24"}) as n:
        n.wait_answering()
        c = n.for_transport(transport)
        assert c.outcome(PING) == "PERMISSION_DENIED"
        assert c.outcome(PING, extra=[("x-forwarded-for", "192.0.2.7")]) == "PERMISSION_DENIED", "proxy trust is off"
    with LocalNode(env={"RAMEN_ALLOWED_CIDRS": "192.0.2.0/24", "RAMEN_TRUST_PROXY_HOPS": "1"}) as n:
        n.wait_serving()
        c = n.for_transport(transport)
        assert c.outcome(PING, extra=[("x-forwarded-for", "192.0.2.7")]) == "OK"
        assert c.outcome(PING, extra=[("x-forwarded-for", "198.51.100.9")]) == "PERMISSION_DENIED"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_blocked_names_are_hidden_and_refused_on_each_transport(transport):
    with LocalNode(env={"RAMEN_BLOCKED": "demo_calculator_tool"}) as n:
        n.wait_serving()
        c = n.for_transport(transport)
        c.initialize()
        assert "demo_calculator_tool" not in {t["name"] for t in c.list_tools()}
        with pytest.raises(JsonRpcError) as e:
            c.call_tool("demo_calculator_tool", ARGS)
        assert e.value.code == -32601


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_inflight_cap_on_each_transport(transport):
    """RAMEN_MAX_INFLIGHT=1: while one call sleeps in the tool, the next is RESOURCE_EXHAUSTED (gRPC) / 429 (HTTP)."""
    import concurrent.futures as cf
    import time

    with LocalNode(bucket=E.FIXTURES / "slow_group", env={"RAMEN_MAX_INFLIGHT": "1"}) as n:
        n.wait_serving()
        a, b = n.for_transport(transport), n.for_transport(transport)
        a.initialize()
        slow = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "slow_tool", "arguments": {"ms": 1500}},
        }
        with cf.ThreadPoolExecutor(2) as ex:
            first = ex.submit(a.outcome, slow)
            time.sleep(0.5)  # well inside the sleeping call
            second = b.outcome(PING)
            assert first.result() == "OK"
        assert second == "RESOURCE_EXHAUSTED"
        assert b.outcome(PING) == "OK"  # the permit came back
        seen = {line.get("transport") for line in n.log_lines() if line.get("msg") == "mcp"}
        assert transport in seen, f"access log must name the transport (§16.1): {seen}"


def test_access_log_names_the_transport_and_the_consumer(plain):
    plain.node.ping()
    plain.http.ping()
    lines = [x for x in plain.log_lines() if x.get("msg") == "mcp" and x.get("method") == "ping"]
    assert {x["transport"] for x in lines} >= {"grpc", "http"}
    assert all(x.get("key_id") and plain.key not in json.dumps(x) for x in lines), (
        "the key itself never reaches the log"
    )


# --- HTTP-only (§16.1 / §16.2) ----------------------------------------------------------------------------------------
def test_http_initialize_mints_a_session_that_only_its_credential_can_use(plain):
    with LocalNode(env={"RAMEN_MCP_KEYS": "test-key,other-key", "RAMEN_SESSION_SECRET": "s"}) as n:
        n.wait_serving()
        c = n.http_client()
        c.initialize()
        assert c.session_id and c.session_id.count(".") == 2
        assert c.ping() == {}  # accepted back
        other = n.http_client(key="other-key")
        other.session_id = c.session_id
        assert other.status(PING) == 404, "a session id must not be reusable under another key"
        c.session_id = "garbage"
        assert c.status(PING) == 404
        # the spec's rule on a 404: start over with a new initialize WITHOUT the old id (a stale id on
        # `initialize` itself is refused too — the node never lets an unverifiable id ride along)
        assert c.status({**PING, "method": "initialize"}) == 404
        c.session_id = None
        c.initialize()
        assert c.end_session() == 204
        assert c.end_session(key="other-key") in (404, 401)


def test_http_origin_validation(plain):
    c = plain.http
    assert c.status(PING, extra=[("Origin", "https://evil.example")]) == 403, "no allowlist → every Origin refused"
    assert c.status(PING) == 200
    with LocalNode(env={"RAMEN_ALLOWED_ORIGINS": "https://app.example"}) as n:
        n.wait_serving()
        h = n.http_client()
        assert h.status(PING, extra=[("Origin", "https://app.example")]) == 200
        assert h.status(PING, extra=[("Origin", "https://app.example.evil")]) == 403
        assert h.status(PING, extra=[("Origin", "http://app.example")]) == 403


def test_http_content_negotiation_and_verbs(plain):
    c = plain.http
    assert c.status(PING, extra=[("Accept", "text/html")]) == 406
    assert c.status(PING, extra=[("Content-Type", "text/plain")]) == 415
    assert c.status(PING, extra=[("MCP-Protocol-Version", "1999-01-01")]) == 400
    r = c.post(PING, extra=[("MCP-Protocol-Version", "2025-03-26")])
    assert (r.status_code, r.headers.get("MCP-Protocol-Version")) == (200, "2025-03-26")
    r = c.get()
    assert (r.status_code, r.headers.get("Allow")) == (405, "POST, DELETE, OPTIONS")
    r = c.post(PING, key=None)
    assert r.status_code == 401 and r.headers.get("WWW-Authenticate", "").startswith("Bearer")


def test_http_protected_resource_metadata_appears_only_with_an_issuer(plain):
    assert plain.http.get("/.well-known/oauth-protected-resource").status_code == 404
    with LocalNode(env={"RAMEN_OAUTH_ISSUER": "https://console.example", "RAMEN_SESSION_SECRET": "s"}) as n:
        n.wait_serving()
        r = n.http_client().get("/.well-known/oauth-protected-resource")
        assert r.status_code == 200
        # RFC 9728 (0.5.93): `resource` is this node's MCP URL (what Claude Code compares with the URL it was given),
        # taken from the request when no public base is configured; the scope rides in scopes_supported
        assert r.json()["resource"].startswith("http://127.0.0.1:") and r.json()["resource"].endswith("/mcp")
        assert r.json()["scopes_supported"] == ["mcp:demo:local"]
        assert r.json()["authorization_servers"] == ["https://console.example"]
        r = n.http_client().post(PING, key=None)
        assert 'resource_metadata="/.well-known/oauth-protected-resource"' in r.headers.get("WWW-Authenticate", "")


def test_http_error_bodies_never_carry_the_key(plain):
    with pytest.raises(HttpError) as e:
        plain.http.call_raw(PING, key="nope-secret-value")
    assert "nope-secret-value" not in e.value.response.text


# --- the official SDK, no bridge (§16.4) -----------------------------------------------------------------------------
async def test_sdk_streamable_http_client_full_flow(plain):
    async with http_session(plain.http) as s:
        assert s.protocol_version == PROTOCOL
        tools = {t.name: t for t in (await s.list_tools()).tools}
        assert tools["demo_calculator_tool"].input_schema["properties"]["func"]["enum"] == [
            "add",
            "subtract",
            "multiply",
            "divide",
        ]
        r = await s.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "multiply"})
        assert not r.is_error and float(sdk_text(r)) == 6
        assert "ramen-demo-mcp-group" in (await s.read_resource("ramen://demo/readme")).contents[0].text


async def test_sdk_client_with_a_bad_key_fails_cleanly(plain):
    with pytest.raises(Exception):  # noqa: B017 - the SDK raises its own error for a 401
        async with http_session(plain.http, key="nope") as s:
            await s.list_tools()


@pytest.mark.skipif(E.env("RAMEN_TEST_TLS", "0") != "1", reason="RAMEN_TEST_TLS=1 runs the node-TLS case")
def test_http_over_node_tls():
    with LocalNode(tls=True) as n:
        n.wait_serving()
        h = n.http_client()
        assert h.url.startswith("https://")
        assert h.initialize()["protocolVersion"] == PROTOCOL
        assert n.node.ping() == {}  # and gRPC on the same TLS port


# --- security review 0.5.0: what the audit asked to see proven on a real node ---------------------------------------
def test_tool_code_never_sees_the_nodes_credentials():
    """H1: the runtime is spawned without the node's secrets. A tool that lists its own environment is the proof."""
    secrets = {
        "RAMEN_SESSION_SECRET": "conformance-secret",
        "RAMEN_OAUTH_ISSUER": "https://console.example",
        "RAMEN_ALLOWED_ORIGINS": "https://app.example",
    }
    with LocalNode(bucket=E.FIXTURES / "slow_group", env=secrets) as n:
        n.wait_serving()
        c = n.for_transport("http")
        c.initialize()
        seen = set(json.loads(text_of(c.call_tool("env_probe", {"prefix": "RAMEN_"}))))
    hidden = {"RAMEN_MCP_KEYS", "RAMEN_ADMIN_KEY", "RAMEN_ALLOWED_CIDRS", "RAMEN_ADMIN_CIDRS", *secrets}
    assert not seen & hidden, seen & hidden
    assert {"RAMEN_GROUP", "RAMEN_ZONE", "RAMEN_BUCKET"} <= seen  # the runtime still knows where it is


def mint_console_token(claims: dict, session_secret: str) -> str:
    """What console/src/ramen_console/oauth_server.py::mint_jwt produces (its own test pins that against this same
    derivation): HS256 with key = HMAC-SHA256(session secret, b"oauth")."""
    import base64
    import hashlib
    import hmac

    def b64(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).rstrip(b"=").decode()

    head = b64(b'{"alg":"HS256","typ":"JWT"}')
    body = b64(json.dumps(claims, separators=(",", ":")).encode())
    key = hmac.new(session_secret.encode(), b"oauth", hashlib.sha256).digest()
    return f"{head}.{body}.{b64(hmac.new(key, f'{head}.{body}'.encode(), hashlib.sha256).digest())}"


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_console_minted_token_is_a_credential_on_each_transport(transport):
    """§16.3 end to end on a real node: the token the console mints (same derivation, same claims) opens the worker,
    the access log names the user, and a token for another zone, another issuer or yesterday does not."""
    import time

    secret, issuer = "conformance-secret", "https://console.example"
    env = {
        "RAMEN_SESSION_SECRET": secret,
        "RAMEN_OAUTH_ISSUER": issuer + "/",
        "RAMEN_PUBLIC_URL": "https://mcp.example",
    }
    with LocalNode(env=env) as n:
        n.wait_serving()
        c = n.for_transport(transport)
        now = int(time.time())
        base = {"iss": issuer, "sub": "u-42", "email": "ada@x", "iat": now, "exp": now + 600, "jti": "j1"}
        good = mint_console_token({**base, "aud": "mcp:demo:local", "scope": "mcp:demo:local"}, secret)
        assert c.outcome(PING, key=good) == "OK"
        assert c.initialize(key=good)["protocolVersion"] == PROTOCOL
        assert float(text_of(c.call_tool("demo_calculator_tool", ARGS, key=good))) == 5
        for bad in (
            mint_console_token({**base, "aud": "mcp:demo:other", "scope": "mcp:demo:other"}, secret),
            mint_console_token({**base, "aud": "mcp:demo:local", "scope": "mcp:demo:local"}, "another-secret"),
            mint_console_token(
                {**base, "iss": "https://evil.example", "aud": "mcp:demo:local", "scope": "mcp:demo:local"}, secret
            ),
            mint_console_token({**base, "exp": now - 1, "aud": "mcp:demo:local", "scope": "mcp:demo:local"}, secret),
        ):
            assert c.outcome(PING, key=bad) == "UNAUTHENTICATED"
        if transport == "http":
            h = n.http_client()
            r = h.get("/.well-known/oauth-protected-resource", headers={"ramen-group": "demo", "ramen-zone": "local"})
            assert r.status_code == 200 and r.json()["authorization_servers"] == [issuer]
            r = h.post(PING, key=None)
            assert (
                'resource_metadata="https://mcp.example/.well-known/oauth-protected-resource"'
                in r.headers["WWW-Authenticate"]
            )
        text = n.log.read_text()
    assert '"user:u-42"' in text and "rmk_" not in text
