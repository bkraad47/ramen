"""CONTRACTS §24.3 (v1.0.0), export half: what a worker with `RAMEN_OTEL_ENDPOINT` sends to an OTLP/HTTP collector.

A real node + runtime and a stdlib collector that keeps every `/v1/traces` and `/v1/metrics` body. One `SERVER` span
per MCP call with the attributes the contract lists and a child span around the runtime round trip; the caller's
`traceparent` is the parent; a guardrail block names its stage and engine; two counters. And what must never leave the
pod: argument values, result text, the MCP key, a person's token — each sent as a distinctive canary and then searched
for in everything the collector received. A collector that is down costs a call nothing."""

import json
import re
import secrets
import shutil
import socket
import statistics
import threading
import time

import pytest

from ramen_tests import env as E
from ramen_tests.fakes import Collector
from ramen_tests.localconsole import SESSION_SECRET
from ramen_tests.localnode import LocalNode, free_port
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import ALL_INPUT, MODERN, Modern
from ramen_tests.tokens import user_token

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
IGNORE = shutil.ignore_patterns("__pycache__")
ISSUER = "https://console.example"
KEY = "rmk_" + secrets.token_hex(12)
ADMIN = secrets.token_hex(16)
OTLP_TOKEN = "otlp-" + secrets.token_hex(12)  # what Ramen sends the collector: a header, never a body
SERVER_KINDS = (2, "SPAN_KIND_SERVER")
INTERNAL_OR_CLIENT = (1, 3, "SPAN_KIND_INTERNAL", "SPAN_KIND_CLIENT")
CALLS, ERRORS = "ramen.mcp.calls", "ramen.mcp.errors"
EXPORT_WARNING = re.compile(r"otel|otlp|telemetry|collector", re.I)  # the wording is the node's own


def canary(kind: str) -> str:
    return f"canary-{kind}-{secrets.token_hex(8)}"


def bucket_copy(root):
    dst = root / "bucket"
    shutil.copytree(E.FIXTURES / "policy_group", dst, ignore=IGNORE)
    return dst


def node_env(endpoint: str) -> dict:
    return {
        "RAMEN_OTEL_ENDPOINT": endpoint,
        "RAMEN_OTEL_HEADERS": json.dumps({"x-otlp-token": OTLP_TOKEN}),
        "RAMEN_SESSION_SECRET": SESSION_SECRET,
        "RAMEN_OAUTH_ISSUER": ISSUER,
    }


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    with Collector() as collector:
        bucket = bucket_copy(tmp_path_factory.mktemp("otel"))
        with LocalNode(bucket=bucket, key=KEY, admin_key=ADMIN, env=node_env(collector.url)) as n:
            n.wait_serving()
            yield n, collector


def named(tool: str, marker: str | None = None):
    """Predicate: the SERVER span of a `tools/call` of `tool` (and, with `marker`, of that trace id)."""

    def pred(s: dict) -> bool:
        return (
            s.get("kind") in SERVER_KINDS
            and s["attrs"].get("mcp.name") == tool
            and (marker is None or s.get("traceId") == marker)
        )

    return pred


def trace() -> tuple[str, str, str]:
    trace_id, parent_id = secrets.token_hex(16), secrets.token_hex(8)
    return trace_id, parent_id, f"00-{trace_id}-{parent_id}-01"


def call_traced(node, transport: str, tool: str, arguments: dict) -> tuple[dict, str, str]:
    """A modern `tools/call` carrying a fresh traceparent → (message, trace id, parent span id). §23.2: the HTTP
    header over HTTP; over gRPC, where there is none, the `_meta` key."""
    trace_id, parent_id, header = trace()
    m = Modern(node.for_transport(transport), ALL_INPUT)
    if transport == "http":
        reply = m.call_raw(tool, arguments, headers={"traceparent": header})
    else:
        reply = m.call_raw(tool, arguments, meta_extra={"traceparent": header})
    return reply.message, trace_id, parent_id


def log_line(node, tool: str, transport: str) -> dict:
    lines = [
        x
        for x in node.log_lines()
        if x.get("msg") == "mcp" and x.get("name") == tool and x.get("transport") == transport
    ]
    assert lines, f"no access-log line for {tool} over {transport}"
    return lines[-1]


# --- the SERVER span --------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_call_produces_a_server_span_with_the_listed_attributes(world, transport):
    node, collector = world
    message, trace_id, _ = call_traced(node, transport, "plain_tool", {"text": "span me"})
    assert text_of(message["result"]) == "span me"
    span = collector.wait_spans(named("plain_tool", trace_id))[0]
    assert span["name"] == "mcp tools/call", span["name"]
    a, line = span["attrs"], log_line(node, "plain_tool", transport)
    assert (a["mcp.method"], a["mcp.name"], a["mcp.protocol"]) == ("tools/call", "plain_tool", MODERN), a
    assert (a["ramen.group"], a["ramen.zone"], a["ramen.env"]) == ("demo", "local", "dev"), a
    assert a["ramen.transport"] == transport
    assert a["ramen.status"] == line["status"] == "ok", (a, line)
    assert a["ramen.key_id"] == line["key_id"] and a["ramen.key_id"], "the key id as the access log has it"
    assert a.get("ramen.reason", "") in ("", None), "no reason on a good call"
    assert "ramen.guardrail.stage" not in a and "ramen.guardrail.engine" not in a
    assert span["resource"]["service.name"] == "ramen-node"
    assert span["resource"].get("service.version"), span["resource"]
    start, end = int(span["startTimeUnixNano"]), int(span["endTimeUnixNano"])
    assert 0 < start <= end and abs(end / 1e9 - time.time()) < 120, (start, end)
    assert len(span["traceId"]) == 32 and len(span["spanId"]) == 16


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_the_runtime_round_trip_is_a_child_span(world, transport):
    node, collector = world
    _, trace_id, _ = call_traced(node, transport, "plain_tool", {"text": "child"})
    server = collector.wait_spans(named("plain_tool", trace_id))[0]
    children = collector.wait_spans(
        lambda s: s.get("traceId") == trace_id and s.get("parentSpanId") == server["spanId"]
    )
    assert len(children) == 1, [c["name"] for c in children]
    child = children[0]
    assert child["name"].startswith("runtime "), child["name"]
    assert child.get("kind") in INTERNAL_OR_CLIENT, child.get("kind")
    assert int(server["startTimeUnixNano"]) <= int(child["startTimeUnixNano"])
    assert int(child["endTimeUnixNano"]) <= int(server["endTimeUnixNano"])


def test_a_list_is_a_span_too_and_has_no_runtime_child(world):
    """`tools/list` is answered from the manifest the node holds: one span, no sidecar round trip."""
    node, collector = world
    trace_id, _, header = trace()
    Modern(node.http).raw("tools/list", headers={"traceparent": header})
    spans = collector.wait_spans(lambda s: s.get("traceId") == trace_id)
    time.sleep(1.5)
    spans = [s for s in collector.spans() if s.get("traceId") == trace_id]
    assert [s["name"] for s in spans] == ["mcp tools/list"], [s["name"] for s in spans]
    assert spans[0]["attrs"]["mcp.method"] == "tools/list" and not spans[0]["attrs"].get("mcp.name")


def test_a_legacy_call_is_exported_with_its_protocol(world):
    node, collector = world
    c = node.http_client()
    c.initialize()
    assert text_of(c.call_tool("plain_tool", {"text": "legacy"})) == "legacy"
    spans = collector.wait_spans(lambda s: named("plain_tool")(s) and s["attrs"].get("mcp.protocol") == "2025-06-18")
    assert spans[0]["name"] == "mcp tools/call"


# --- the parent -------------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_an_inbound_traceparent_becomes_the_parent(world, transport):
    node, collector = world
    _, trace_id, parent_id = call_traced(node, transport, "plain_tool", {"text": "parented"})
    span = collector.wait_spans(named("plain_tool", trace_id))[0]
    assert span["traceId"] == trace_id, "the caller's trace continues"
    assert span.get("parentSpanId") == parent_id, "the caller's span is the parent"
    assert span["spanId"] != parent_id


def test_traceparent_in_meta_is_the_parent_when_no_header_carries_it(world):
    node, collector = world
    trace_id, parent_id, header = trace()
    Modern(node.http).call("plain_tool", {"text": "meta"}, meta_extra={"traceparent": header})
    span = collector.wait_spans(named("plain_tool", trace_id))[0]
    assert span.get("parentSpanId") == parent_id


def test_without_a_traceparent_the_span_is_a_root_with_a_fresh_trace(world):
    node, collector = world
    before = {s["spanId"] for s in collector.spans()}
    Modern(node.http).call("plain_tool", {"text": "root-a"})
    Modern(node.http).call("plain_tool", {"text": "root-b"})
    deadline, fresh = time.monotonic() + 15, []
    while time.monotonic() < deadline and len(fresh) < 2:
        fresh = [s for s in collector.spans() if named("plain_tool")(s) and s["spanId"] not in before]
        fresh = [s for s in fresh if not s.get("parentSpanId")]
        time.sleep(0.2)
    assert len(fresh) >= 2, "two root spans expected"
    assert len({s["traceId"] for s in fresh}) == len(fresh), "each call its own trace"
    assert all(s["traceId"] != "0" * 32 for s in fresh)


def test_a_malformed_traceparent_is_ignored_not_fatal(world):
    node, collector = world
    r = Modern(node.http).call_raw("plain_tool", {"text": "bad-parent"}, headers={"traceparent": "zz-not-a-trace"})
    assert text_of(r.result) == "bad-parent"


# --- guardrails, errors -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "stage"), [("please drop table users", "pre"), ("the secret word", "post")], ids=["pre", "post"]
)
def test_a_guardrail_block_sets_the_guardrail_attributes(world, text, stage):
    node, collector = world
    message, trace_id, _ = call_traced(node, "http", "echo_tool", {"text": text})
    assert message["result"]["isError"] is True and f"guardrail blocked: {stage}" in text_of(message["result"])
    span = collector.wait_spans(named("echo_tool", trace_id))[0]
    a, line = span["attrs"], log_line(node, "echo_tool", "http")
    assert (a.get("ramen.guardrail.stage"), a.get("ramen.guardrail.engine")) == (stage, "policy"), a
    assert a["ramen.status"] == line["status"] and a["ramen.reason"] == line["reason"] == f"guardrail_{stage}", (
        a,
        line,
    )


def test_an_error_is_a_span_with_the_logs_status(world):
    node, collector = world
    trace_id, _, header = trace()
    r = Modern(node.http).call_raw("no_such_tool", {}, headers={"traceparent": header})
    assert r.code == -32602
    span = collector.wait_spans(named("no_such_tool", trace_id))[0]
    assert span["attrs"]["ramen.status"] == log_line(node, "no_such_tool", "http")["status"] == "error"


# --- counters ---------------------------------------------------------------------------------------------------------
def has(point: dict, *values: str) -> bool:
    return set(values) <= {str(v) for v in point["attrs"].values()}


def test_the_two_counters_arrive_by_method_name_and_status(world):
    node, collector = world
    m = Modern(node.http)
    for _ in range(3):
        m.call("plain_tool", {"text": "count"})
    m.call_raw("counted_missing_tool", {})
    calls = collector.wait_points(CALLS, lambda p: has(p, "tools/call", "plain_tool", "ok"), timeout=75)
    assert max(p["value"] for p in calls) >= 3, calls
    assert calls[0]["resource"]["service.name"] == "ramen-node"
    errors = collector.wait_points(ERRORS, lambda p: has(p, "tools/call", "counted_missing_tool"), timeout=75)
    assert max(p["value"] for p in errors) >= 1
    assert not [p for p in collector.metric_points(ERRORS) if has(p, "plain_tool", "ok")], "a good call is no error"


# --- what never leaves the pod ---------------------------------------------------------------------------------------
def test_no_argument_result_key_or_token_reaches_the_collector(world):
    node, collector = world
    arg, result_only, answer, blocked = canary("argument"), canary("result"), canary("answer"), canary("blocked")
    token, _ = user_token(SESSION_SECRET, ISSUER, 600, sub="u-otel", jti=canary("jti"))
    marks = []
    for transport in TRANSPORTS:
        m = Modern(node.for_transport(transport), ALL_INPUT)
        assert text_of(m.call("echo_tool", {"text": arg})) == arg  # the argument is also the result text
        assert text_of(m.call("plain_tool", {"text": result_only})) == result_only
        m.call("echo_tool", {"text": f"drop table {blocked}"})  # blocked: the guardrail's verdict names nothing
        m.call_raw("ask_nothing_missing", {"text": arg})  # an error: its message and arguments stay home
        person = m.with_key(token)
        assert text_of(person.call("plain_tool", {"text": answer})) == answer
        m.request("resources/list")
        marks.append(transport)
    c = node.http_client()
    c.initialize()
    c.call_tool("echo_tool", {"text": arg})
    # wait until the last of those calls has been exported, then one more batch interval
    collector.wait_spans(lambda s: s["attrs"].get("ramen.key_id") == "user:u-otel", n=2, timeout=30)
    collector.wait_points(CALLS, lambda p: has(p, "echo_tool"), timeout=75)
    time.sleep(2)
    bodies = "\n".join(b.decode(errors="replace") for b in collector.bodies())
    assert bodies, "nothing was exported"
    leaked = {
        "argument value": arg,
        "result text": result_only,
        "result text of a person's call": answer,
        "blocked argument": blocked,
        "MCP key": KEY,
        "person's token": token,
        "token signature": token.rsplit(".", 1)[1],
        "admin key": ADMIN,
        "session secret": SESSION_SECRET,
        "collector credential (belongs in a header)": OTLP_TOKEN,
    }
    found = [what for what, value in leaked.items() if value in bodies]
    assert found == [], f"exported to the collector: {found}"
    assert "Bearer " not in bodies and "authorization" not in bodies.lower()


def test_the_collector_gets_ramens_own_headers_and_json(world):
    node, collector = world
    Modern(node.http).call("plain_tool", {"text": "headers"})
    collector.wait_spans(named("plain_tool"))
    posts = [p for p in collector.posts if p["path"] == "/v1/traces"]
    assert all(p["headers"].get("x-otlp-token") == OTLP_TOKEN for p in posts), "RAMEN_OTEL_HEADERS on every export"
    assert all(p["headers"].get("content-type", "").startswith("application/json") for p in posts), posts[0]["headers"]
    assert {p["path"] for p in collector.posts} <= {"/v1/traces", "/v1/metrics"}, {p["path"] for p in collector.posts}
    assert not any(KEY in json.dumps(p["headers"]) for p in collector.posts)


def test_the_runtime_never_sees_the_collector_credential(world):
    """`RAMEN_OTEL_HEADERS` is the node's: hidden from the Python runtime like every node secret."""
    node, _ = world
    text = node.log.read_text() + node.output()
    assert OTLP_TOKEN not in text, "the collector credential is in the worker's log"


# --- a collector that is not there ------------------------------------------------------------------------------------
def timings(node, n: int = 20) -> list[float]:
    m, out = Modern(node.http), []
    for i in range(n):
        began = time.monotonic()
        assert text_of(m.call("plain_tool", {"text": f"t{i}"})) == f"t{i}"
        out.append(time.monotonic() - began)
    return out


def blackhole():
    """A listener that accepts and never answers: the slowest kind of dead collector."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(64)
    held, stop = [], threading.Event()

    def accept():
        sock.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(sock.accept()[0])
            except OSError:
                continue

    threading.Thread(target=accept, daemon=True).start()
    return sock, stop


@pytest.mark.parametrize("kind", ["refused", "blackhole", "http-500"])
def test_a_dead_collector_neither_fails_nor_slows_a_call(world, tmp_path, kind):
    healthy, _ = world
    baseline = statistics.median(timings(healthy, 10))
    sock = stop = failing = None
    if kind == "refused":
        endpoint = f"http://127.0.0.1:{free_port()}"
    elif kind == "blackhole":
        sock, stop = blackhole()
        endpoint = f"http://127.0.0.1:{sock.getsockname()[1]}"
    else:
        failing = Collector(status=500).__enter__()
        endpoint = failing.url
    try:
        with LocalNode(bucket=bucket_copy(tmp_path), key=KEY, env=node_env(endpoint)) as n:
            n.wait_serving()
            took = timings(n, 25)
            time.sleep(6)  # at least one export attempt has failed by now
            took += timings(n, 25)
            assert max(took) < 2.0, f"a call waited {max(took):.2f}s on a dead collector"
            assert statistics.median(took) < max(0.25, baseline * 5), (statistics.median(took), baseline)
            assert n.node.health() == "SERVING"
            deadline, warned = time.monotonic() + 20, []
            while time.monotonic() < deadline and not warned:
                warned = [
                    x
                    for x in n.log_lines()
                    if x.get("level") in ("warn", "warning") and EXPORT_WARNING.search(json.dumps(x))
                ]
                time.sleep(0.5)
            assert warned, "an unreachable collector is logged"
            assert len(warned) <= 2, f"one warning a minute, saw {len(warned)} in under a minute"
            assert OTLP_TOKEN not in json.dumps(warned)
    finally:
        if stop:
            stop.set()
            sock.close()
        if failing:
            failing.__exit__(None, None, None)


def test_no_endpoint_means_nothing_is_exported(tmp_path):
    with Collector() as idle:
        env = {k: v for k, v in node_env(idle.url).items() if k != "RAMEN_OTEL_ENDPOINT"}
        with LocalNode(bucket=bucket_copy(tmp_path), key=KEY, env=env) as n:
            n.wait_serving()
            Modern(n.http).call("plain_tool", {"text": "quiet"})
            time.sleep(3)
        assert idle.posts == []
