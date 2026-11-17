"""CONTRACTS §23 (v1.0.0, MCP 2026-07-28): MRTR golden cases, header promotion in the compatibility gate, and the
protocol / subscriptions / tasks a worker reports, on the pages that already show its calls and metrics."""

import asyncio
import json

import pytest

from ramen_console import __version__, compat, golden
from ramen_console.logview import parse_log
from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures
from tests.test_deploy_gates import DEPLOY, deploy, manifest, write_cases, ws  # noqa: F401 - pytest fixtures
from tests.test_logs_page import line, logfile, page  # noqa: F401 - pytest fixtures

PV = "io.modelcontextprotocol/protocolVersion"
SERVER = "io.modelcontextprotocol/serverInfo"
META = {
    PV: "2026-07-28",
    "io.modelcontextprotocol/clientInfo": {"name": "ramen-console", "version": __version__},
    "io.modelcontextprotocol/clientCapabilities": {"elicitation": {"form": {}, "url": {}}, "sampling": {}, "roots": {}},
}
ASK = {
    "resultType": "input_required",
    "inputRequests": {"confirm": {"method": "elicitation/create", "params": {"message": "Sure?"}}},
    "requestState": "abc.def",
}
DONE = {
    "resultType": "complete",
    "_meta": {SERVER: {"name": "ramen-node", "version": "1.0.0"}},
    "content": [{"type": "text", "text": "deleted"}],
}
YAML = """
cases:
  - tool: wipe
    args: {path: /tmp/x}
    respond: {confirm: {action: accept, content: {ok: true}}}
    expect: {exact: {content: [{type: text, text: deleted}]}}
  - tool: wipe
    name: asks
    expect: {input_required: [confirm]}
  - tool: calc
    expect: {text_contains: "4"}
"""


def script(*results):
    """An async `call` answering the given results in order, recording every request."""
    sent, left = [], list(results)

    async def call(req):
        sent.append(req)
        r = left.pop(0)
        return {"jsonrpc": "2.0", "id": req["id"], **(r if "error" in r else {"result": r})}

    return call, sent


def run(case, *results):
    call, sent = script(*results)
    return asyncio.run(golden.run(case, call, 7)), sent


# 1. golden cases --------------------------------------------------------------------------------------------------


def test_respond_and_input_required_are_parsed_and_old_cases_keep_their_shape():
    wipe, asks, calc = golden.parse(YAML)
    assert wipe["respond"] == {"confirm": {"action": "accept", "content": {"ok": True}}}
    assert asks["expect"] == {"input_required": ["confirm"]} and "respond" not in asks
    assert calc == {"name": "calc", "tool": "calc", "args": {}, "expect": {"text_contains": "4"}}
    assert golden.modern(wipe) and golden.modern(asks) and not golden.modern(calc)


@pytest.mark.parametrize(
    "text, msg",
    [
        ("cases: [{tool: t, respond: 5, expect: {text_contains: x}}]", "case t: `respond` must be a mapping"),
        (
            "cases: [{tool: t, respond: {k: yes}, expect: {text_contains: x}}]",
            "case t: `respond.k` must be a mapping",
        ),
        ("cases: [{tool: t, expect: {input_required: confirm}}]", "case t: `input_required` must be a list of keys"),
        ("cases: [{tool: t, expect: {input_required: []}}]", "case t: `input_required` must be a list of keys"),
        (
            "cases: [{tool: t, expect: {input_required: [k], subset: {}}}]",
            "case t: `expect` needs exactly one of subset, exact, text_contains, input_required",
        ),
        ("cases: [{tool: t, expect: {}}]", "case t: `expect` needs subset, exact or text_contains"),
    ],
)
def test_a_bad_mrtr_case_is_refused(text, msg):
    with pytest.raises(golden.GoldenError, match=msg.replace("`", ".")):
        golden.parse(text)


def test_a_case_without_the_new_keys_is_sent_exactly_as_before():
    case = golden.parse(YAML)[2]
    why, sent = run(case, {"content": [{"type": "text", "text": "42"}]})
    assert why is None
    assert sent == [{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "calc", "arguments": {}}}]
    assert golden.request(case, 7) == sent[0]


def test_a_respond_case_calls_in_the_modern_era_and_retries_once_with_the_responses_and_state():
    case = golden.parse(YAML)[0]
    why, sent = run(case, ASK, DONE)
    assert why is None
    assert sent[0]["params"] == {"name": "wipe", "arguments": {"path": "/tmp/x"}, "_meta": META}
    assert sent[1]["params"] == {
        "name": "wipe",
        "arguments": {"path": "/tmp/x"},
        "_meta": META,
        "inputResponses": case["respond"],
        "requestState": "abc.def",
    }


def test_the_request_state_is_left_out_when_none_came_back():
    case = golden.parse(YAML)[0]
    why, sent = run(case, {k: v for k, v in ASK.items() if k != "requestState"}, DONE)
    assert why is None and "requestState" not in sent[1]["params"] and "inputResponses" in sent[1]["params"]


def test_a_respond_case_whose_tool_never_asks_is_checked_on_the_first_result():
    why, sent = run(golden.parse(YAML)[0], DONE)
    assert why is None and len(sent) == 1


def test_expect_applies_to_the_final_result_and_ignores_the_envelope():
    case = golden.parse(YAML)[0]
    why, _ = run(case, ASK, {**DONE, "content": [{"type": "text", "text": "kept"}]})
    assert why.startswith("exact: expected") and "resultType" not in why and "serverInfo" not in why
    extra = {**DONE, "_meta": {**DONE["_meta"], "ramen": {"x": 1}}}
    why, _ = run(case, ASK, extra)  # a `_meta` key of the tool's own is still part of the result
    assert "'ramen'" in why
    sub = {**case, "expect": {"subset": {"content": [{"text": "deleted"}]}}}
    assert run(sub, ASK, DONE)[0] is None


def test_a_second_input_required_after_the_retry_fails_naming_the_keys():
    why, sent = run(golden.parse(YAML)[0], ASK, {**ASK, "inputRequests": {"again": {}, "more": {}}})
    assert why == "the tool asked for input again after the responses were sent (again, more)" and len(sent) == 2


def test_expect_input_required_asserts_the_first_round_only():
    case = golden.parse(YAML)[1]
    why, sent = run(case, ASK)
    assert why is None and len(sent) == 1 and sent[0]["params"]["_meta"] == META
    why, _ = run({**case, "expect": {"input_required": ["confirm", "pin"]}}, ASK)
    assert why == "input_required: expected the tool to ask for confirm, pin; it asked for confirm"
    why, _ = run(case, DONE)
    assert why == "input_required: expected the tool to ask for confirm; it answered without asking"
    why, _ = run(case, {"error": {"code": -32021, "message": "missing capability"}})
    assert why == "error -32021: missing capability"


def test_a_case_without_respond_whose_tool_asks_fails_naming_the_keys():
    case = golden.parse(YAML)[2]
    why, sent = run(case, ASK)  # a node answering input_required to a case that sent no responses
    assert why == "the tool asked for input (confirm) and the case has no `respond`" and len(sent) == 1
    legacy = {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": "this tool needs more input (confirm); it can only ask a client that speaks MCP 2026-07-28",
            }
        ],
    }
    why, _ = run(case, legacy)  # what a 1.0 node really answers on the legacy era (§23.4)
    assert why.startswith("tool error: this tool needs more input (confirm)")


def test_an_error_on_the_retry_is_the_failure():
    why, _ = run(
        golden.parse(YAML)[0], ASK, {"error": {"code": -32602, "message": "requestState integrity check failed"}}
    )
    assert why == "error -32602: requestState integrity check failed"


def test_the_deploy_gate_runs_an_mrtr_case_against_the_canary(demo, ws, tmp_path):  # noqa: F811
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    for w in ws.values():
        w.tool_results["wipe"] = lambda params: DONE if "inputResponses" in params else ASK
        w.tool_results["calc"] = {"content": [{"type": "text", "text": "42"}]}
    write_cases(tmp_path, YAML)
    job = deploy(demo)
    assert job["status"] == "ok", job
    assert job["result"]["zone-a"]["golden"] == {"passed": 3, "failed": []}
    calls = [c[2]["params"] for c in ws["cold"].calls if c[0] == "Mcp/Call" and c[2].get("method") == "tools/call"]
    assert [sorted(p) for p in calls] == [
        ["_meta", "arguments", "name"],
        ["_meta", "arguments", "inputResponses", "name", "requestState"],
        ["_meta", "arguments", "name"],
        ["arguments", "name"],
    ]
    for w in ws.values():
        w.tool_results["wipe"] = ASK
    job = deploy(demo)
    assert job["status"] == "error" and "wipe: the tool asked for input again" in job["error"], job


# 2. compatibility gate --------------------------------------------------------------------------------------------


def tool(header=None, **extra):
    region = {"type": "string", **({"x-mcp-header": header} if header else {})}
    return {"name": "sql", "inputSchema": {"type": "object", "properties": {"region": region}}, **extra}


@pytest.mark.parametrize(
    "old, new, msg",
    [
        (None, "Region", "tool sql: property region header added: none -> Region"),
        ("Region", None, "tool sql: property region header removed: Region -> none"),
        ("Region", "Area", "tool sql: property region header renamed: Region -> Area"),
    ],
)
def test_a_header_promotion_change_is_breaking(old, new, msg):
    d = compat.diff({"tools": [tool(old)]}, {"tools": [tool(new)]})
    assert d == {"breaking": [msg], "additive": []} and compat.is_breaking(d)


def test_an_unchanged_header_and_a_new_optional_header_parameter_do_not_break():
    assert compat.diff({"tools": [tool("Region")]}, {"tools": [tool("Region")]}) == {"breaking": [], "additive": []}
    new = tool("Region")
    new["inputSchema"]["properties"]["tenant"] = {"type": "string", "x-mcp-header": "Tenant"}
    d = compat.diff({"tools": [tool("Region")]}, {"tools": [new]})
    assert d == {"breaking": [], "additive": ["tool sql: new optional property tenant"]}


@pytest.mark.parametrize(
    "marker", [{"task": True}, {"execution": {"taskSupport": "optional"}}, {"_meta": {compat.TASKS: {}}}]
)
def test_gaining_or_losing_task_is_additive(marker):
    d = compat.diff({"tools": [tool()]}, {"tools": [tool(**marker)]})
    assert d == {"breaking": [], "additive": ["tool sql: now runs as a task"]}
    d = compat.diff({"tools": [tool(**marker)]}, {"tools": [tool()]})
    assert d == {"breaking": [], "additive": ["tool sql: no longer runs as a task"]}
    assert compat.is_task(tool(**marker)) and not compat.is_task(tool()) and not compat.is_task(tool(task=False))
    assert compat.header_params(tool("Region")) == {"region": "Region"} and compat.header_params(tool()) == {}
    assert compat.header_params({"name": "x"}) == {} and not compat.is_task({"execution": "x", "_meta": "y"})


def test_the_deploy_gate_refuses_a_header_change_unless_breaking(demo, ws):  # noqa: F811
    for w in ws.values():
        w.load_result = manifest(tools=(tool(),))
    assert deploy(demo)["status"] == "ok"
    for w in ws.values():
        w.load_result = manifest(tools=(tool("Region", task=True),))
    job = deploy(demo)
    assert job["status"] == "error" and "property region header added: none -> Region" in job["error"], job
    job = deploy(demo, breaking=True)
    assert job["status"] == "ok" and "tool sql: now runs as a task" in job["result"]["zone-a"]["compat"]["additive"]


# 3. protocol visibility -------------------------------------------------------------------------------------------


def test_a_log_entry_carries_its_protocol():
    new = json.dumps({"ts": "t", "msg": "mcp", "method": "tools/call", "status": "ok", "protocol": "2026-07-28"})
    old = json.dumps({"ts": "t", "msg": "mcp", "method": "tools/call", "status": "ok"})
    assert [e["protocol"] for e in parse_log("\n".join([new, old, "not json"]))] == [None, None, "2026-07-28"]


def test_the_logs_page_shows_the_protocol_of_each_line(demo, logfile):  # noqa: F811
    doc = json.loads(line(2))
    logfile([line(1), json.dumps({**doc, "protocol": "2026-07-28"})])
    html = page(demo)
    assert html.count('<span class="badge proto" title="MCP protocol version served">') == 2
    assert 'served">2026-07-28</span>' in html and 'served">—</span>' in html


def workers_html(c):
    return c.get("/ui/groups/demo/zones/zone-a/workers").text


def test_the_workers_partial_shows_calls_by_protocol_subscriptions_and_tasks(demo, workers):  # noqa: F811
    before = workers_html(demo)
    assert "Calls by protocol" not in before and "subscription" not in before and "task" not in before
    workers["cold"].metrics.update(
        calls_by_protocol={"2025-06-18": 3, "2026-07-28": 12, "1<b>": 1}, subscriptions=2, tasks=1
    )
    html = workers_html(demo)
    assert "Calls by protocol: 2026-07-28: 12 · 2025-06-18: 3 · 1&lt;b&gt;: 1" in html
    assert "2 open subscriptions" in html and "1 live task<" in html
    workers["cold"].metrics.update(calls_by_protocol={}, subscriptions=0, tasks=0)
    html = workers_html(demo)  # a 1.0 worker that served nothing yet still says so
    assert "Calls by protocol: <span" in html and "0 open subscriptions" in html and "0 live tasks" in html
    for k in ("calls_by_protocol", "subscriptions", "tasks"):
        del workers["cold"].metrics[k]
    assert workers_html(demo) == before  # a 0.8 worker renders exactly as it did


def test_protocol_counts_are_summed_over_the_workers_of_a_zone():
    from ramen_console.web.pages import protocol_stats

    live = [
        {"metrics": {"calls_by_protocol": {"2026-07-28": 2, "2025-06-18": 1}, "subscriptions": 1, "tasks": 0}},
        {"metrics": {"calls_by_protocol": {"2026-07-28": 5, "bad": "x"}, "tasks": 3}},
        {"metrics": {"calls_by_protocol": "junk", "subscriptions": "junk"}},
        {"id": "?"},
    ]
    assert protocol_stats(live) == {
        "calls_by_protocol": [("2026-07-28", 7), ("2025-06-18", 1)],
        "subscriptions": 1,
        "tasks": 3,
    }
    assert protocol_stats([{"metrics": {"inflight": 1}}]) == {
        "calls_by_protocol": None,
        "subscriptions": None,
        "tasks": None,
    }


# 4. group page badges ---------------------------------------------------------------------------------------------


def test_packages_per_zone_marks_task_tools_and_header_promoted_parameters(demo, ws):  # noqa: F811
    assert deploy(demo)["status"] == "ok"
    html = demo.get("/groups/demo").text
    assert ">task</span>" not in html and ">headers</span>" not in html
    for w in ws.values():
        w.load_result = manifest(tools=(tool("Region", task=True), tool() | {"name": "plain"}))
    assert deploy(demo, breaking=True)["status"] == "ok"
    html = demo.get("/groups/demo").text
    assert html.count(">task</span>") == 2 and html.count(">headers</span>") == 2  # once per zone, not on `plain`
    assert 'title="Sent as HTTP headers too (Mcp-Param-*): region → Region"' in html
