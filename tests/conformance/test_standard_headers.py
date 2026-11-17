"""CONTRACTS §23.2 (v1.0.0): the standard request headers of the modern era, over HTTP, on a real node.

`Mcp-Method` and `Mcp-Name` must repeat the body; a tool parameter that declares `"header": "<Name>"` is mirrored in
`Mcp-Param-<Name>` and must match; values may use the `=?base64?…?=` sentinel. The body stays authoritative: a
mismatch is refused with `-32020` (HTTP 400) and logged `header_mismatch`, never guessed. Legacy requests and gRPC are
not subject to any of it. W3C trace context is copied onto the access-log line."""

import base64
import json
import secrets
import shutil
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import MODERN, WITH_TASKS, Modern, b64

pytestmark = pytest.mark.conformance
IGNORE = shutil.ignore_patterns("__pycache__")
ARGS = {"region": "eu-west", "count": 7, "flag": True, "mode": "fast", "note": "body only"}
PARAM = {"Mcp-Param-Region": "eu-west", "Mcp-Param-Count": "7", "Mcp-Param-Flag": "true", "Mcp-Param-Mode": "fast"}
ORIGIN = "https://app.example"


@pytest.fixture(scope="module")
def node():
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", env={"RAMEN_ALLOWED_ORIGINS": ORIGIN}) as n:
        n.wait_serving()
        yield n


@pytest.fixture
def m(node) -> Modern:
    return Modern(node.http)


def refused(reply) -> bool:
    return (reply.status, reply.code) == (400, -32020)


def route(m: Modern, args=None, headers=None):
    return m.call_raw("route_tool", ARGS if args is None else args, headers=PARAM if headers is None else headers)


# --- Mcp-Method -------------------------------------------------------------------------------------------------------
def test_mcp_method_is_required_and_must_equal_the_body_method(m):
    assert m.raw("tools/list").status == 200
    assert refused(m.raw("tools/list", headers={"Mcp-Method": None})), "missing"
    assert refused(m.raw("tools/list", headers={"Mcp-Method": "prompts/list"})), "another method"
    assert refused(m.raw("tools/list", headers={"Mcp-Method": "TOOLS/LIST"})), "values are case-sensitive"
    assert refused(m.raw("tools/list", headers={"Mcp-Method": ""})), "empty"


def test_header_names_are_case_insensitive(m):
    for name in ("mcp-method", "MCP-METHOD"):
        r = m.raw("tools/list", headers={"Mcp-Method": None, name: "tools/list"})
        assert r.status == 200 and r.error is None, (name, r.message)


def test_a_method_mismatch_is_refused_before_the_method_is_looked_up(m):
    """Check 4 runs before check 5: `ping` (removed, 404) with a wrong Mcp-Method is the header error, 400."""
    assert refused(m.raw("ping", headers={"Mcp-Method": "tools/list"}))
    r = m.raw("tools/call", {"name": "plain_tool", "arguments": {"text": "x"}}, headers={"Mcp-Method": "tasks/get"})
    assert refused(r), r.message


# --- Mcp-Name ---------------------------------------------------------------------------------------------------------
NAMED = [
    ("tools/call", {"name": "plain_tool", "arguments": {"text": "hi"}}, "plain_tool"),
    ("prompts/get", {"name": "pick_prompt", "arguments": {"flavour": "miso", "size": "large"}}, "pick_prompt"),
    ("resources/read", {"uri": "ramen://bowl/logo"}, "ramen://bowl/logo"),
    ("resources/read", {"uri": "ramen://bowl/miso/recipe"}, "ramen://bowl/miso/recipe"),
]


@pytest.mark.parametrize("method,params,name", NAMED, ids=[n[2] for n in NAMED])
def test_mcp_name_is_required_and_must_equal_the_name_or_uri(m, method, params, name):
    ok = m.raw(method, params)
    assert ok.status == 200 and ok.error is None, ok.message
    assert refused(m.raw(method, params, headers={"Mcp-Name": None})), "missing"
    assert refused(m.raw(method, params, headers={"Mcp-Name": name + "x"})), "mismatch"
    assert refused(m.raw(method, params, headers={"Mcp-Name": name.upper()})), "case-sensitive"
    sentinel = m.raw(method, params, headers={"Mcp-Name": b64(name)})
    assert sentinel.status == 200 and sentinel.error is None, f"the base64 sentinel is decoded: {sentinel.message}"
    assert refused(m.raw(method, params, headers={"Mcp-Name": b64(name + "x")})), "a sentinel naming something else"
    assert refused(m.raw(method, params, headers={"Mcp-Name": "=?base64?!!!not-base64!!!?="})), "undecodable"
    not_utf8 = "=?base64?" + base64.b64encode(b"\xff\xfe\xfd").decode() + "?="
    assert refused(m.raw(method, params, headers={"Mcp-Name": not_utf8})), "not UTF-8"


def test_mcp_name_is_not_expected_on_methods_without_a_name(m):
    for method in ("tools/list", "prompts/list", "resources/list", "server/discover"):
        assert m.raw(method).status == 200
    extra = m.raw("tools/list", headers={"Mcp-Name": "anything"})
    assert extra.status == 200, "a stray Mcp-Name on a list is not an error"


@pytest.mark.parametrize("method", ["tasks/get", "tasks/update", "tasks/cancel"])
def test_task_methods_carry_the_task_id_in_mcp_name(m, method):
    t = m.with_caps(WITH_TASKS)
    created = t.call("slow_job", {"seconds": 5, "label": "hdr"})
    assert created["resultType"] == "task", created
    params = {"taskId": created["taskId"]}
    if method == "tasks/update":
        params["inputResponses"] = {}
    try:
        assert refused(t.raw(method, params, headers={"Mcp-Name": None})), "missing"
        wrong = "sep-2663-wrong-task-id-for-routing-header-mismatch"
        assert refused(t.raw(method, params, headers={"Mcp-Name": wrong})), "another id"
        ok = t.raw(method, params)
        assert ok.status == 200 and ok.error is None, ok.message
    finally:
        t.raw("tasks/cancel", params)


# --- Mcp-Param-<Name> -------------------------------------------------------------------------------------------------
def test_tools_list_marks_the_mirrored_parameters_in_both_eras(node, m):
    for tools in (m.request("tools/list")["tools"], node.http_client().list_tools()):
        props = next(t for t in tools if t["name"] == "route_tool")["inputSchema"]["properties"]
        marks = {k: v.get("x-mcp-header") for k, v in props.items()}
        assert marks == {"region": "Region", "count": "Count", "flag": "Flag", "mode": "Mode", "note": None}, marks
        assert props["count"]["type"] == "integer" and props["mode"]["enum"] == ["fast", "safe"]


def test_matching_parameter_headers_are_accepted(m):
    r = route(m)
    assert r.status == 200 and json.loads(text_of(r.result)) == ARGS, r.message


@pytest.mark.parametrize("header", sorted(PARAM))
def test_each_declared_parameter_header_is_required_when_the_argument_is_present(m, header):
    r = route(m, headers={k: v for k, v in PARAM.items() if k != header})
    assert refused(r), f"{header} missing: {r.message}"


@pytest.mark.parametrize(
    "header,value",
    [
        ("Mcp-Param-Region", "us-east"),
        ("Mcp-Param-Region", "EU-WEST"),
        ("Mcp-Param-Count", "8"),
        ("Mcp-Param-Count", "seven"),
        ("Mcp-Param-Flag", "false"),
        ("Mcp-Param-Flag", "1"),
        ("Mcp-Param-Mode", "safe"),
        ("Mcp-Param-Region", "=?base64?!!!?="),
        ("Mcp-Param-Region", "=?base64?" + base64.b64encode(b"us-east").decode() + "?="),
    ],
)
def test_a_parameter_header_that_disagrees_with_the_body_is_refused(m, header, value):
    r = route(m, headers={**PARAM, header: value})
    assert refused(r), f"{header}: {value}: {r.message}"
    assert r.result is None, "the body is not run with either value"


def test_parameter_header_names_are_case_insensitive_and_unknown_ones_are_ignored(m):
    lower = {k.lower(): v for k, v in PARAM.items()}
    r = route(m, headers={**{k: None for k in PARAM}, **lower, "Mcp-Param-Unknown": "x", "Mcp-Param-Note": "nope"})
    assert r.status == 200 and json.loads(text_of(r.result)) == ARGS, r.message


def test_typed_values_compare_by_value_not_by_spelling(m):
    for args, headers in (
        ({**ARGS, "flag": False}, {**PARAM, "Mcp-Param-Flag": "false"}),
        ({**ARGS, "count": -3}, {**PARAM, "Mcp-Param-Count": "-3"}),
        ({**ARGS, "count": 0}, {**PARAM, "Mcp-Param-Count": "0"}),
        ({**ARGS, "mode": "safe"}, {**PARAM, "Mcp-Param-Mode": "safe"}),
    ):
        r = route(m, args, headers)
        assert r.status == 200 and json.loads(text_of(r.result)) == args, (args, r.message)


def test_the_base64_sentinel_carries_values_a_header_cannot(m):
    for region in ("Tōkyō 東京", " padded ", "line\nbreak"):
        args = {**ARGS, "region": region}
        r = route(m, args, {**PARAM, "Mcp-Param-Region": b64(region)})
        assert r.status == 200 and json.loads(text_of(r.result))["region"] == region, (region, r.message)
    literal = "=?base64?SGVsbG8="  # no closing "?=": not a sentinel, compared as written
    r = route(m, {**ARGS, "region": literal}, {**PARAM, "Mcp-Param-Region": literal})
    assert r.status == 200 and json.loads(text_of(r.result))["region"] == literal, r.message
    r = route(m, {**ARGS, "region": "Hello"}, {**PARAM, "Mcp-Param-Region": "=?base64?SGVsbG8=?="})
    assert r.status == 200, "an ASCII value may use the sentinel too"
    r = route(m, {**ARGS, "region": "Hello"}, {**PARAM, "Mcp-Param-Region": "=?base64?SGVsbG8?="})
    assert refused(r), "bad padding is undecodable"


@pytest.mark.parametrize("absent", [{}, {"region": None}], ids=["absent", "null"])
def test_an_absent_or_null_argument_expects_no_header(m, absent):
    """The header rule does not fire; what answers is the ordinary argument validation (a tool error result)."""
    args = {k: v for k, v in ARGS.items() if k != "region"} | absent
    r = route(m, args, {k: v for k, v in PARAM.items() if k != "Mcp-Param-Region"})
    assert r.code != -32020 and r.status == 200, r.message
    assert r.result["isError"] is True and "region" in text_of(r.result), r.result


def test_a_mismatch_is_logged_as_header_mismatch_and_never_reaches_the_tool(node, m):
    marker = "mismatch-" + secrets.token_hex(4)
    r = route(m, {**ARGS, "note": marker}, {**PARAM, "Mcp-Param-Region": "us-east"})
    assert refused(r)
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        lines = [x for x in node.log_lines() if x.get("reason") == "header_mismatch"]
        if lines:
            break
        time.sleep(0.2)
    assert lines, node.log_lines()[-3:]
    assert (lines[-1]["method"], lines[-1]["name"], lines[-1]["status"]) == ("tools/call", "route_tool", "denied")
    assert marker not in node.log.read_text() and "us-east" not in node.log.read_text(), "values are never logged"


def test_legacy_requests_and_grpc_are_not_subject_to_header_rules(node):
    legacy = node.http_client()
    legacy.initialize()
    assert json.loads(text_of(legacy.call_tool("route_tool", ARGS))) == ARGS, "no Mcp-* header on a legacy call"
    assert json.loads(text_of(Modern(node.node).call("route_tool", ARGS))) == ARGS, "gRPC: the body alone"


# --- packages that declare a bad header -------------------------------------------------------------------------------
def test_a_package_with_an_invalid_header_declaration_fails_to_load(tmp_path):
    bucket = tmp_path / "bucket"
    shutil.copytree(E.FIXTURES / "mrtr_group", bucket, ignore=IGNORE)
    bad = {
        "dup_header": {"a": {"type": "string", "header": "Region"}, "b": {"type": "string", "header": "region"}},
        "number_header": {"amount": {"type": "number", "header": "Amount"}},
        "token_header": {"a": {"type": "string", "header": "Not A Token"}},
    }
    for name, params in bad.items():
        d = bucket / "mcp" / "tools" / name
        d.mkdir()
        proto = {
            "type": "tool",
            "name": name,
            "description": "bad header declaration",
            "callable": "run",
            "input": params,
        }
        (d / f"{name}.json").write_text(json.dumps(proto))
        (d / f"{name}.py").write_text("def run(**kw):\n    return 'never'\n")
    with LocalNode(bucket=bucket) as n:
        n.wait_serving()
        errors = {e["package"].rsplit("/", 1)[-1] for e in n.node.admin_reload()["errors"]}  # "tools/<name>"
        assert set(bad) <= errors, errors
        listed = {t["name"] for t in Modern(n.http).request("tools/list")["tools"]}
        assert "route_tool" in listed and not set(bad) & listed, listed


# --- trace context ----------------------------------------------------------------------------------------------------
def trace_line(node: LocalNode, marker: str) -> dict:
    deadline = time.monotonic() + 5
    while True:
        for line in node.log_lines():
            if line.get("msg") == "mcp" and marker in json.dumps(line.get("trace", {})):
                return line
        if time.monotonic() > deadline:
            raise AssertionError(f"no log line with trace {marker}: {node.log_lines()[-3:]}")
        time.sleep(0.2)


def test_trace_context_headers_are_copied_onto_the_log_line(node, m):
    trace_id = secrets.token_hex(16)
    parent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    m.raw("tools/list", headers={"traceparent": parent, "tracestate": "vendor=opaque", "baggage": "tenant=noodle"})
    line = trace_line(node, trace_id)
    assert line["trace"] == {"traceparent": parent, "tracestate": "vendor=opaque", "baggage": "tenant=noodle"}, line
    assert line["protocol"] == MODERN


def test_trace_context_in_meta_is_used_when_no_header_carries_it(node, m):
    trace_id = secrets.token_hex(16)
    parent = f"00-{trace_id}-{secrets.token_hex(8)}-00"
    m.raw("tools/list", meta_extra={"traceparent": parent, "tracestate": "k=v"})
    assert trace_line(node, trace_id)["trace"] == {"traceparent": parent, "tracestate": "k=v"}
    grpc_id = secrets.token_hex(16)
    Modern(node.node).raw("tools/list", meta_extra={"traceparent": f"00-{grpc_id}-{secrets.token_hex(8)}-01"})
    assert trace_line(node, grpc_id)["transport"] == "grpc"
    both = secrets.token_hex(16)
    header = f"00-{both}-{secrets.token_hex(8)}-01"
    m.raw("tools/list", headers={"traceparent": header}, meta_extra={"traceparent": parent})
    assert trace_line(node, both)["trace"]["traceparent"] == header, "the header wins over _meta"


@pytest.mark.parametrize("bad", ["garbage", "00-xyz-abc-01", "00-" + "0" * 31 + "-" + "1" * 16 + "-01"])
def test_a_malformed_traceparent_is_not_logged(node, m, bad):
    marker = "state-" + secrets.token_hex(6)
    m.raw("prompts/list", headers={"traceparent": bad, "tracestate": marker})
    time.sleep(0.5)
    assert marker not in node.log.read_text(), "tracestate and baggage are only copied beside a valid traceparent"
    lines = [x for x in node.log_lines() if x.get("method") == "prompts/list"]
    assert lines and "trace" not in lines[-1], lines[-1]


def test_trace_context_is_never_handed_to_the_tool(node, m):
    trace_id = secrets.token_hex(16)
    parent = f"00-{trace_id}-{secrets.token_hex(8)}-01"
    r = m.call_raw("caps_echo", headers={"traceparent": parent}, meta_extra={"traceparent": parent, "baggage": "a=b"})
    assert r.status == 200 and trace_id not in json.dumps(r.result), r.result


# --- CORS -------------------------------------------------------------------------------------------------------------
def test_cors_allows_and_exposes_the_standard_headers(node, m):
    asked = "authorization, content-type, mcp-protocol-version, mcp-method, mcp-name, mcp-param-region"
    preflight = node.http._client.request(  # noqa: SLF001 - a browser preflight, which HttpNode has no verb for
        "OPTIONS",
        node.url,
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": asked},
    )
    assert preflight.status_code in (200, 204), preflight.status_code
    allowed = preflight.headers.get("access-control-allow-headers", "").lower()
    assert allowed == "*" or all(h in allowed for h in ("mcp-method", "mcp-name", "mcp-protocol-version")), allowed
    assert allowed == "*" or "mcp-param-" in allowed, f"Mcp-Param-* must be allowed: {allowed}"
    r = m.raw("tools/list", headers={"Origin": ORIGIN})
    assert r.status == 200 and r.headers.get("access-control-allow-origin") == ORIGIN, r.headers
    exposed = r.headers.get("access-control-expose-headers", "").lower()
    assert "mcp-protocol-version" in exposed, exposed
