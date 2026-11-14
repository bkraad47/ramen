"""CONTRACTS §23.1 (v1.0.0): era selection and the modern envelope, on real `ramen-node` processes.

The node is dual-era. A request is modern when `MCP-Protocol-Version` is not a legacy version or the body carries
`_meta["io.modelcontextprotocol/protocolVersion"]`; everything else is served exactly as 0.8.0 served it
(`test_golden_regression.py` holds that half to the byte). Here: the numbered modern checks and their order, the
removed methods, `server/discover`, caching fields, the modern error codes, `completion/complete`, the access log and
metrics additions, and the modern era over gRPC, where the body alone decides."""

import json
import secrets
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import CLIENT_INFO, JsonRpcError, text_of
from ramen_tests.modern import CC, CI, LEGACY, MODERN, PV, TASKS, Modern, assert_envelope, meta

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
REMOVED = ("initialize", "ping", "logging/setLevel", "resources/subscribe", "resources/unsubscribe")
LISTS = {
    "tools/list": "tools",
    "prompts/list": "prompts",
    "resources/list": "resources",
    "resources/templates/list": "resourceTemplates",
}


@pytest.fixture(scope="module")
def suite():
    """The official suite's fixture group: content blocks, templates, callable prompts, task tools."""
    with LocalNode(bucket=E.FIXTURES / "conformance_group") as n:
        n.wait_serving()
        yield n


@pytest.fixture(scope="module")
def bowl():
    """`mrtr_group`: enum arguments (completion) and header parameters."""
    with LocalNode(bucket=E.FIXTURES / "mrtr_group") as n:
        n.wait_serving()
        yield n


@pytest.fixture
def http(suite) -> Modern:
    return Modern(suite.http)


@pytest.fixture(params=TRANSPORTS, ids=TRANSPORTS)
def modern(request, suite) -> Modern:
    return Modern(suite.for_transport(request.param))


def discover_body(rid, params_meta) -> dict:
    params = {} if params_meta is None else {"_meta": params_meta}
    return {"jsonrpc": "2.0", "id": rid, "method": "server/discover", "params": params}


def log_line(node: LocalNode, timeout: float = 5, **match) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        for line in reversed(node.log_lines()):
            if line.get("msg") == "mcp" and all(line.get(k) == v for k, v in match.items()):
                return line
        if time.monotonic() > deadline:
            raise AssertionError(f"no mcp log line matching {match}: {node.log_lines()[-5:]}")
        time.sleep(0.2)


# --- check 1: _meta ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "case,params_meta",
    [
        ("no _meta", None),
        ("no protocolVersion", {CI: CLIENT_INFO, CC: {}}),
        ("no clientCapabilities", {PV: MODERN, CI: CLIENT_INFO}),
        ("clientCapabilities is a string", {PV: MODERN, CC: "all"}),
        ("clientCapabilities is a list", {PV: MODERN, CC: []}),
        ("clientCapabilities is null", {PV: MODERN, CC: None}),
    ],
)
def test_a_modern_request_without_a_complete_meta_is_32602_and_http_400(http, case, params_meta):
    """The valid header makes the request modern even when the body names no version, so this is -32602, not -32020."""
    r = http.send(discover_body(101, params_meta), headers={"MCP-Protocol-Version": MODERN})
    assert (r.status, r.code) == (400, -32602), (case, r.message)
    assert r.message["id"] == 101, "the error carries the request id"


def test_client_info_is_optional(http):
    r = http.send(discover_body(105, {PV: MODERN, CC: {}}))
    assert r.status == 200 and r.error is None and r.result["supportedVersions"], r.message


def test_a_string_request_id_is_echoed_on_an_error(http):
    r = http.send(discover_body("req-a", None), headers={"MCP-Protocol-Version": MODERN})
    assert r.code == -32602 and r.message["id"] == "req-a", r.message


# --- checks 2 and 3: header vs body, then the version -----------------------------------------------------------------
def test_a_missing_version_header_is_32020(http):
    r = http.raw("server/discover", headers={"MCP-Protocol-Version": None})
    assert (r.status, r.code) == (400, -32020), r.message


@pytest.mark.parametrize("header", ["2025-06-18", "v999.0.0", "2026-07-27"])
def test_a_header_that_disagrees_with_the_body_is_32020(http, header):
    r = http.raw("server/discover", headers={"MCP-Protocol-Version": header})
    assert (r.status, r.code) == (400, -32020), r.message


def test_an_unsupported_version_is_32022_with_the_supported_list(http):
    r = http.raw("server/discover", version="v999.0.0", rid=301)
    assert (r.status, r.code) == (400, -32022), r.message
    assert r.error["data"] == {"supported": [MODERN, *LEGACY], "requested": "v999.0.0"}, r.error
    assert r.message["id"] == 301


def test_the_mismatch_is_reported_before_the_unsupported_version(http):
    """Body v999, header 2026-07-28: check 2 (-32020) runs before check 3 (-32022)."""
    body = http.body("server/discover", version="v999.0.0", rid=302)
    r = http.send(body, headers={"MCP-Protocol-Version": MODERN})
    assert (r.status, r.code) == (400, -32020), r.message


def test_the_meta_check_runs_before_the_version_checks(http):
    """Header v999 and no `_meta` at all: check 1 (-32602) answers, not -32020 or -32022."""
    r = http.send(discover_body(303, None), headers={"MCP-Protocol-Version": "v999.0.0"})
    assert (r.status, r.code) == (400, -32602), r.message


def test_a_legacy_version_named_in_a_modern_body_is_unsupported_not_served(http):
    """A body that carries `_meta.protocolVersion` is modern; 2025-06-18 there (header agreeing) is not MODERN."""
    r = http.raw("server/discover", version="2025-06-18")
    assert (r.status, r.code) == (400, -32022), r.message
    assert r.error["data"]["requested"] == "2025-06-18"


def test_the_credential_is_checked_before_anything_modern(suite):
    """§23.1 order: … credential → body → era → modern checks. A broken modern request without a key is a 401."""
    anon = Modern(suite.http, key=None)
    r = anon.send(discover_body(1, None), headers={"MCP-Protocol-Version": "v999.0.0"})
    assert r.status == 401, r.message
    r = Modern(suite.http).send(
        discover_body(1, None), headers={"MCP-Protocol-Version": MODERN, "Origin": "https://evil.example"}
    )
    assert r.status == 403, "Origin is refused before the era is looked at"


# --- check 5: removed and unknown methods -----------------------------------------------------------------------------
@pytest.mark.parametrize("method", [*REMOVED, "unknown/method", "tasks/result", "tasks/list", "roots/list"])
def test_removed_and_unknown_methods_are_32601_and_http_404(modern, method):
    r = modern.raw(method, rid=500)
    assert r.code == -32601, r.message
    assert r.message["id"] == 500
    if modern.transport == "http":
        assert r.status == 404, r.status


def test_modern_notifications_are_accepted_and_ignored(http, suite):
    body = {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {"_meta": meta()}}
    r = http.send(body)
    assert r.status == 202 and r.message is None, (r.status, r.message)
    body["method"] = "notifications/cancelled"
    assert http.send(body).status == 202
    assert Modern(suite.node).send(body).message is None, "gRPC: an empty body, as for any notification"


# --- sessions ---------------------------------------------------------------------------------------------------------
def test_modern_requests_ignore_a_session_id_and_never_get_one(http, suite):
    r = http.raw("tools/list", headers={"Mcp-Session-Id": "not.a.real.session"})
    assert r.status == 200 and "tools" in r.result, "a legacy request with this id would be a 404"
    assert "mcp-session-id" not in r.headers
    assert "mcp-session-id" not in http.raw("server/discover").headers
    assert suite.http.get().status_code == 405, "GET /mcp stays 405"


# --- the envelope -----------------------------------------------------------------------------------------------------
def test_every_modern_result_carries_result_type_and_server_info(modern):
    calls = [
        ("server/discover", None),
        ("tools/list", None),
        ("prompts/list", None),
        ("resources/list", None),
        ("resources/templates/list", None),
        ("resources/read", {"uri": "test://static-text"}),
        ("prompts/get", {"name": "test_simple_prompt", "arguments": {}}),
        ("tools/call", {"name": "test_simple_text", "arguments": {}}),
        ("tools/call", {"name": "test_error_handling", "arguments": {}}),
        ("completion/complete", {"ref": {"type": "ref/prompt", "name": "test_prompt_with_arguments"},
                                 "argument": {"name": "arg1", "value": "t"}}),
    ]  # fmt: skip
    for method, params in calls:
        result = modern.request(method, params)
        assert_envelope(result)
        assert (
            result["_meta"]["io.modelcontextprotocol/serverInfo"]["version"]
            == (E.RAMEN_DIR / "VERSION").read_text().strip()
        )


def test_server_info_is_merged_into_an_existing_meta(bowl):
    """A guardrail block already has `_meta.ramen.guardrail`; the envelope adds to it."""
    result = Modern(bowl.http).call("ask_confirm", {"item": "drop table users"})
    assert_envelope(result)
    assert result["_meta"]["ramen"]["guardrail"] == {"stage": "pre", "engine": "policy"}, result


def test_list_results_and_reads_carry_caching_fields(modern):
    for method, key in LISTS.items():
        result = modern.request(method)
        assert isinstance(result[key], list), (method, result)
        assert (result["ttlMs"], result["cacheScope"]) == (60000, "private"), (method, result)
    read = modern.request("resources/read", {"uri": "test://static-text"})
    assert (read["ttlMs"], read["cacheScope"]) == (0, "private"), read
    discover = modern.discover()
    assert (discover["ttlMs"], discover["cacheScope"]) == (60000, "private"), discover
    for method, params in (
        ("tools/call", {"name": "test_simple_text", "arguments": {}}),
        ("prompts/get", {"name": "test_simple_prompt", "arguments": {}}),
    ):
        result = modern.request(method, params)
        assert "ttlMs" not in result and "cacheScope" not in result, (method, result)


def test_list_ttl_follows_ramen_list_ttl_ms_and_reads_stay_zero():
    with LocalNode(bucket=E.FIXTURES / "conformance_group", env={"RAMEN_LIST_TTL_MS": "1234"}) as n:
        n.wait_serving()
        m = Modern(n.http)
        assert {m.request(x)["ttlMs"] for x in LISTS} == {1234}
        assert m.discover()["ttlMs"] == 1234
        assert m.request("resources/read", {"uri": "test://static-text"})["ttlMs"] == 0


def test_templates_are_listed_apart_from_resources(modern):
    templates = modern.request("resources/templates/list")["resourceTemplates"]
    assert [t["uriTemplate"] for t in templates] == ["test://template/{id}/data"], templates
    assert templates[0]["name"] == "template_data" and templates[0]["mimeType"] == "application/json"
    uris = [r["uri"] for r in modern.request("resources/list")["resources"]]
    assert "test://template/{id}/data" not in uris and "test://static-text" in uris, uris
    read = modern.request("resources/read", {"uri": "test://template/123/data"})["contents"][0]
    assert read["uri"] == "test://template/123/data" and json.loads(read["text"])["id"] == "123", read


def test_tools_list_is_stable_and_never_input_required(modern):
    names = [[t["name"] for t in modern.request("tools/list")["tools"]] for _ in range(3)]
    assert names[0] == names[1] == names[2] and len(names[0]) > 20
    assert modern.request("tools/list")["resultType"] == "complete"


# --- server/discover --------------------------------------------------------------------------------------------------
def test_discover_is_exactly_the_contract(modern):
    d = modern.discover()
    assert d["resultType"] == "complete"
    assert d["supportedVersions"] == [MODERN, *LEGACY]
    assert d["capabilities"] == {
        "tools": {"listChanged": True},
        "prompts": {"listChanged": True},
        "resources": {"listChanged": True},
        "completions": {},
        "extensions": {TASKS: {}},
    }, d["capabilities"]
    assert set(d) == {"resultType", "supportedVersions", "capabilities", "ttlMs", "cacheScope", "_meta"}, sorted(d)


def test_discover_names_the_tasks_extension_only_when_a_task_tool_is_loaded():
    with LocalNode() as n:  # demo_group: no tool has `task: true`
        n.wait_serving()
        caps = Modern(n.http).discover()["capabilities"]
        assert "extensions" not in caps and "logging" not in caps, caps
        assert caps["resources"] == {"listChanged": True}, "no resources.subscribe"


# --- modern error codes -----------------------------------------------------------------------------------------------
def test_unknown_names_are_32602_with_http_200(modern):
    r = modern.call_raw("no_such_tool")
    assert (r.code, r.error["message"]) == (-32602, "Unknown tool: no_such_tool"), r.message
    uri = "test://nonexistent-resource-for-conformance-testing"
    read = modern.raw("resources/read", {"uri": uri})
    assert (read.code, read.error["message"], read.error["data"]) == (-32602, "Resource not found", {"uri": uri})
    prompt = modern.raw("prompts/get", {"name": "no_such_prompt", "arguments": {}})
    assert prompt.code == -32602, prompt.message
    if modern.transport == "http":
        assert (r.status, read.status, prompt.status) == (200, 200, 200)


def test_a_tool_error_is_a_result_not_a_json_rpc_error(modern):
    result = modern.call("test_error_handling")
    assert result["isError"] is True and "intentionally returns an error" in text_of(result), result


@pytest.fixture(scope="module")
def restricted():
    """One blocked tool, one tool a key may list but not call, one a key may not even list (§9, §20)."""
    access = {"greet": {"list": ["key"], "call": []}, "header_echo": {"list": [], "call": []}}
    env = {
        "RAMEN_BLOCKED": "test_simple_text,test://static-text,test_simple_prompt",
        "RAMEN_TOOL_ACCESS": json.dumps(access),
    }
    with LocalNode(bucket=E.FIXTURES / "conformance_group", env=env) as n:
        n.wait_serving()
        yield n


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_blocked_hidden_and_denied_names_use_32602_and_keep_their_log_reasons(restricted, transport):
    m = Modern(restricted.for_transport(transport))
    listed = {t["name"] for t in m.request("tools/list")["tools"]}
    assert "greet" in listed and not {"test_simple_text", "header_echo"} & listed, listed
    blocked = m.call_raw("test_simple_text")
    assert (blocked.code, blocked.error["message"]) == (-32602, "Unknown tool: test_simple_text"), blocked.message
    hidden = m.call_raw("header_echo", {"region": "eu"}, headers={"Mcp-Param-Region": "eu"})
    assert (hidden.code, hidden.error["message"]) == (-32602, "Unknown tool: header_echo"), hidden.message
    denied = m.call_raw("greet", {"name": "x"})
    assert (denied.code, denied.error["message"]) == (-32602, "forbidden: greet is not callable for key"), (
        denied.message
    )
    resource = m.raw("resources/read", {"uri": "test://static-text"})
    assert (resource.code, resource.error["message"]) == (-32602, "Resource not found"), resource.message
    assert resource.error["data"] == {"uri": "test://static-text"}
    assert m.raw("prompts/get", {"name": "test_simple_prompt", "arguments": {}}).code == -32602
    for r in (blocked, hidden, denied, resource):
        assert not -32019 <= r.code <= -32000
        assert r.status in (None, 200)
    for name, reason in (
        ("test_simple_text", "blocked_name"),
        ("header_echo", "tool_hidden"),
        ("greet", "tool_denied"),
    ):
        assert log_line(restricted, name=name, reason=reason, transport=transport)["protocol"] == MODERN


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_the_same_refusals_keep_their_0_8_codes_for_a_legacy_client(restricted, transport):
    c = restricted.for_transport(transport)
    c.initialize()
    for name, args, code in (
        ("test_simple_text", {}, -32601),
        ("header_echo", {"region": "eu"}, -32601),
        ("greet", {"name": "x"}, -32003),
    ):
        with pytest.raises(JsonRpcError) as e:
            c.call_tool(name, args)
        assert e.value.code == code, (name, e.value)
    with pytest.raises(JsonRpcError) as e:
        c.call_tool("no_such_tool", {})
    assert e.value.code == -32004


# --- completion/complete (both eras) ----------------------------------------------------------------------------------
COMPLETIONS = [
    ({"type": "ref/prompt", "name": "pick_prompt"}, "flavour", "sh", ["shoyu", "shio"]),
    ({"type": "ref/prompt", "name": "pick_prompt"}, "flavour", "", ["shoyu", "shio", "miso", "tonkotsu"]),
    ({"type": "ref/prompt", "name": "pick_prompt"}, "flavour", "x", []),
    ({"type": "ref/prompt", "name": "pick_prompt"}, "size", "l", []),  # not an enum
    ({"type": "ref/prompt", "name": "pick_prompt"}, "nope", "", []),
    ({"type": "ref/prompt", "name": "no_such_prompt"}, "flavour", "", []),
    ({"type": "ref/resource", "uri": "ramen://bowl/{flavour}/recipe"}, "flavour", "m", ["miso"]),
    ({"type": "ref/resource", "uri": "ramen://bowl/logo"}, "flavour", "", []),
]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_completion_offers_the_matching_enum_entries_in_both_eras(bowl, transport):
    legacy, m = bowl.for_transport(transport), Modern(bowl.for_transport(transport))
    legacy.initialize()
    for ref, name, value, values in COMPLETIONS:
        params = {"ref": ref, "argument": {"name": name, "value": value}}
        want = {"completion": {"values": values, "total": len(values), "hasMore": False}}
        assert legacy.request("completion/complete", params) == want, (ref, name, value)
        got = m.request("completion/complete", params)
        assert got["completion"] == want["completion"] and got["resultType"] == "complete", got


# --- legacy untouched (the golden file is the full proof; these are the §23.1 sentences) ------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_bare_request_is_legacy_and_gets_no_modern_member(suite, transport):
    c = suite.for_transport(transport)
    init = c.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": CLIENT_INFO})
    assert init["protocolVersion"] == "2025-06-18", "a requested 2025-11-25 is answered with 2025-06-18"
    assert "resultType" not in init and "extensions" not in init["capabilities"], init
    assert c.ping() == {}
    for method in ("tools/list", "prompts/list", "resources/list"):
        result = c.request(method)
        assert not {"resultType", "ttlMs", "cacheScope", "_meta"} & set(result), (method, sorted(result))
    call = c.call_tool("test_simple_text")
    assert "resultType" not in call and "_meta" not in call, call
    sync = c.call_tool("slow_compute", {"seconds": 0, "label": "legacy"})
    assert "taskId" not in sync and "computed legacy" in text_of(sync), "a legacy caller gets the synchronous call"


def test_a_legacy_version_header_alone_keeps_the_request_legacy(suite):
    c = suite.http_client()
    c.initialize()
    r = c.post({"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert (r.status_code, r.json()) == (200, {"jsonrpc": "2.0", "id": 7, "result": {}})
    assert r.headers.get("MCP-Protocol-Version") == "2025-06-18"


# --- gRPC: the body alone decides -------------------------------------------------------------------------------------
def test_grpc_is_modern_by_meta_alone_and_applies_no_header_rule(suite):
    g = Modern(suite.node)
    assert_envelope(g.request("tools/list"))
    assert text_of(g.call("header_echo", {"region": "eu-west"})) == "eu-west", "no Mcp-Param-Region on gRPC"
    assert g.request("resources/read", {"uri": "test://static-text"})["contents"][0]["text"].startswith("This is")
    bad = g.raw("server/discover", version="v999.0.0")
    assert bad.code == -32022 and bad.error["data"]["requested"] == "v999.0.0", bad.message
    no_caps = g.send({"jsonrpc": "2.0", "id": 9, "method": "tools/list", "params": {"_meta": {PV: MODERN}}})
    assert no_caps.code == -32602, "check 1 still applies: clientCapabilities is required"
    legacy = g.send({"jsonrpc": "2.0", "id": 10, "method": "tools/list", "params": {"_meta": {CC: {}}}})
    assert "resultType" not in legacy.result, "without protocolVersion in the body a gRPC request is legacy"
    assert g.send({"jsonrpc": "2.0", "id": 11, "method": "ping"}).result == {}


# --- access log and metrics -------------------------------------------------------------------------------------------
def test_the_access_log_names_the_protocol_and_metrics_count_by_it():
    key = "rmk_" + secrets.token_hex(8)
    with LocalNode(bucket=E.FIXTURES / "conformance_group", key=key) as n:
        n.wait_serving()
        legacy = n.http_client()
        legacy.initialize()
        legacy.list_tools()
        Modern(n.http).request("tools/list")
        Modern(n.node).request("prompts/list")
        assert log_line(n, method="tools/list", transport="http", protocol=MODERN)
        assert log_line(n, method="prompts/list", transport="grpc", protocol=MODERN)
        assert log_line(n, method="tools/list", protocol="2025-06-18")
        assert all("protocol" in x for x in n.log_lines() if x.get("msg") == "mcp"), "every mcp line names the protocol"
        metrics = n.node.admin_metrics()
        by = metrics["calls_by_protocol"]
        assert by[MODERN] >= 2 and by["2025-06-18"] >= 2, by
        assert metrics["subscriptions"] == 0 and metrics["tasks"] == 0, metrics
        assert key not in n.log.read_text()
