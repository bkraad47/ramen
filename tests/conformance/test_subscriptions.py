"""CONTRACTS §23.6 (v1.0.0): `subscriptions/listen` — a POST answered as an event stream, modern era, HTTP only.

First the acknowledgement naming the honoured notification kinds, then one `list_changed` per requested kind after
each load whose manifest hash differs, every notification tagged with the subscription id (the request id). A stream
holds no inflight permit, is capped per pod, and is closed with a result when the node shuts down. Supersedes the
"no push channel" note of §20 for modern clients; the legacy SSE answer (C11, `test_rollout_notify.py`) is unchanged."""

import json
import shutil
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import text_of
from ramen_tests.modern import SUB_ID, WITH_TASKS, Modern

pytestmark = pytest.mark.conformance
IGNORE = shutil.ignore_patterns("__pycache__")
ACK = "notifications/subscriptions/acknowledged"
TOOLS, PROMPTS, RESOURCES = (f"notifications/{k}/list_changed" for k in ("tools", "prompts", "resources"))
ALL_KINDS = {"toolsListChanged": True, "promptsListChanged": True, "resourcesListChanged": True}


@pytest.fixture
def live(tmp_path):
    """A fresh node on a private copy of the demo bucket: each test deploys into it."""
    bucket = tmp_path / "bucket"
    shutil.copytree(E.FIXTURES / "demo_group", bucket, ignore=IGNORE)
    with LocalNode(bucket=bucket) as n:
        n.wait_serving()
        n.bucket = bucket
        n.m = Modern(n.http)
        yield n


def add_tool(node, name: str = "slow_tool") -> None:
    """The deploy a group admin would make: one more tool, then `Admin/Reload` (what the console sends)."""
    shutil.copytree(
        E.FIXTURES / "slow_group" / "mcp" / "tools" / name, node.bucket / "mcp" / "tools" / name, ignore=IGNORE
    )
    assert node.node.admin_reload()["errors"] == []


def remove_prompt(node) -> None:
    shutil.rmtree(node.bucket / "mcp" / "prompts" / "get_calculation_prompt")
    assert node.node.admin_reload()["errors"] == []


def ack_of(stream) -> dict:
    assert stream.status == 200, (stream.status, stream.body)
    first = stream.next(5)
    assert first is not None and first.get("method") == ACK, f"the acknowledgement comes first: {first}"
    return first


def collect(stream, seconds: float) -> list[dict]:
    out, deadline = [], time.monotonic() + seconds
    while (left := deadline - time.monotonic()) > 0:
        try:
            event = stream.next(left)
        except TimeoutError:
            break
        if event is None:
            break
        out.append(event)
    return out


# --- the acknowledgement ---------------------------------------------------------------------------------------------
def test_the_stream_opens_with_the_acknowledgement(live):
    with live.m.listen({"toolsListChanged": True}, rid=4242) as s:
        assert s.headers["content-type"].startswith("text/event-stream"), s.headers
        assert s.headers.get("x-accel-buffering") == "no"
        assert "mcp-session-id" not in s.headers
        ack = ack_of(s)
        assert ack["jsonrpc"] == "2.0" and "id" not in ack, "a notification, not the response"
        assert ack["params"]["_meta"][SUB_ID] == 4242, "the subscription id is the request id"
        assert ack["params"]["notifications"] == {"toolsListChanged": True}, ack
        assert s.quiet(1), "nothing follows until something changes"


def test_the_ack_names_only_the_honoured_kinds(live):
    asked = {**ALL_KINDS, "resourceSubscriptions": ["ramen://demo/readme"], "madeUp": True}
    with live.m.listen(asked) as s:
        assert ack_of(s)["params"]["notifications"] == ALL_KINDS, "resourceSubscriptions is never honoured"
    # the tasks extension: asking for task notifications without declaring it MUST be -32021, not a narrower ack
    with live.m.listen({**ALL_KINDS, "taskIds": ["0" * 32]}) as s:
        assert s.status == 400 and json.loads(s.body)["error"]["code"] == -32021, (s.status, s.body)
    with live.m.listen({"promptsListChanged": True, "toolsListChanged": False}) as s:
        assert ack_of(s)["params"]["notifications"] == {"promptsListChanged": True}
    with live.m.listen({}) as s:
        assert ack_of(s)["params"]["notifications"] == {}


def test_a_string_request_id_tags_the_stream_as_given(live):
    with live.m.listen({"toolsListChanged": True}, rid="sub-a") as s:
        assert ack_of(s)["params"]["_meta"][SUB_ID] == "sub-a"
        add_tool(live)
        assert s.next(5)["params"]["_meta"][SUB_ID] == "sub-a"


# --- list_changed -----------------------------------------------------------------------------------------------------
def test_a_reload_that_changes_the_manifest_is_announced_once_per_requested_kind(live):
    with live.m.listen(ALL_KINDS, rid=77) as s:
        ack_of(s)
        add_tool(live)
        events = collect(s, 3)
        assert sorted(e["method"] for e in events) == sorted([TOOLS, PROMPTS, RESOURCES]), events
        assert all(e["params"]["_meta"][SUB_ID] == 77 and "id" not in e for e in events), events
        assert {t["name"] for t in live.m.request("tools/list")["tools"]} == {"demo_calculator_tool", "slow_tool"}
        assert text_of(live.m.call("slow_tool", {"ms": 5})) == "slept 5 ms", "the pod serves the new version at once"


def test_the_filter_is_honoured(live):
    """A prompts-only stream never carries a tools notification, a tools-only stream never a prompts one."""
    with (
        live.m.listen({"promptsListChanged": True}, rid=1) as prompts,
        live.m.listen({"toolsListChanged": True}, rid=2) as tools,
    ):
        ack_of(prompts)
        ack_of(tools)
        add_tool(live)
        on_tools, on_prompts = collect(tools, 3), collect(prompts, 0.5)
        assert [e["method"] for e in on_tools] == [TOOLS], on_tools
        assert all(e["method"] == PROMPTS for e in on_prompts), f"only what was asked for: {on_prompts}"
        assert on_tools[0]["params"]["_meta"][SUB_ID] == 2
        remove_prompt(live)
        assert [e["method"] for e in collect(prompts, 3)] == [PROMPTS]
        assert [e["method"] for e in collect(tools, 0.5)] in ([], [TOOLS])


def test_a_reload_with_the_same_manifest_is_silent(live):
    with live.m.listen(ALL_KINDS) as s:
        ack_of(s)
        before = live.node.admin_metrics()["manifest_hash"]
        assert live.node.admin_reload()["hash"] == before
        assert s.quiet(2), "the hash did not move: nothing to announce"
        add_tool(live)
        assert live.node.admin_metrics()["manifest_hash"] != before
        assert len(collect(s, 3)) == 3
        add_tool(live, "env_probe")
        assert len(collect(s, 3)) == 3, "a second deploy is announced again"


def test_every_open_stream_is_told(live):
    with live.m.listen({"toolsListChanged": True}, rid=10) as a, live.m.listen({"toolsListChanged": True}, rid=11) as b:
        ack_of(a)
        ack_of(b)
        add_tool(live)
        assert a.next(5)["params"]["_meta"][SUB_ID] == 10
        assert b.next(5)["params"]["_meta"][SUB_ID] == 11


# --- limits -----------------------------------------------------------------------------------------------------------
def test_subscriptions_are_capped_per_pod(tmp_path):
    with LocalNode(env={"RAMEN_MAX_SUBSCRIPTIONS": "2"}) as n:
        n.wait_serving()
        m = Modern(n.http)
        with m.listen({"toolsListChanged": True}) as a, m.listen({"toolsListChanged": True}) as b:
            ack_of(a)
            ack_of(b)
            assert n.node.admin_metrics()["subscriptions"] == 2
            with m.listen({"toolsListChanged": True}) as c:
                assert c.status == 429, c.status
            assert m.request("tools/list")["resultType"] == "complete", "ordinary calls are not affected by the cap"
        # a client that just closes its socket is noticed when a write fails: at the latest on the second
        # keep-alive comment (15 s apart) — Linux reports the reset one write later than macOS does
        deadline = time.monotonic() + 60
        while n.node.admin_metrics()["subscriptions"] and time.monotonic() < deadline:
            time.sleep(0.3)
        assert n.node.admin_metrics()["subscriptions"] == 0, "closed streams give their slots back"
        with m.listen({"toolsListChanged": True}) as again:
            ack_of(again)


def test_a_stream_holds_no_inflight_permit():
    with LocalNode(env={"RAMEN_MAX_INFLIGHT": "1"}) as n:
        n.wait_serving()
        m = Modern(n.http)
        with m.listen({"toolsListChanged": True}) as a, m.listen({"promptsListChanged": True}) as b:
            ack_of(a)
            ack_of(b)
            for _ in range(3):
                assert m.request("tools/list")["resultType"] == "complete"
            assert n.node.admin_metrics()["inflight"] == 0


# --- where it does not exist ------------------------------------------------------------------------------------------
def test_listen_is_http_and_modern_only(live):
    r = Modern(live.node).raw("subscriptions/listen", {"notifications": {"toolsListChanged": True}})
    assert r.code == -32601, f"gRPC is unary: {r.message}"
    legacy = live.http_client()
    legacy.initialize()
    body = {"jsonrpc": "2.0", "id": 5, "method": "subscriptions/listen", "params": {"notifications": ALL_KINDS}}
    r = legacy.post(body)
    assert r.headers["content-type"].startswith("application/json") and r.json()["error"]["code"] == -32601, r.text


def test_listen_goes_through_the_modern_checks_like_any_request(live):
    m = live.m
    body = m.body("subscriptions/listen", {"notifications": ALL_KINDS})
    assert (
        m.send(body, headers={"Mcp-Method": "tools/list"}).status,
        m.send(body, headers={"Mcp-Method": "tools/list"}).code,
    ) == (400, -32020)
    assert Modern(live.http, key=None).send(body).status == 401


# --- keep-alive and shutdown ------------------------------------------------------------------------------------------
def test_an_idle_stream_gets_a_comment_every_15_seconds(live):
    with live.m.listen({"toolsListChanged": True}) as s:
        ack_of(s)
        deadline = time.monotonic() + 17
        while s.comments == 0 and time.monotonic() < deadline:
            time.sleep(0.25)
        assert s.comments >= 1, "an SSE comment keeps proxies from closing an idle stream"
        assert s.events.empty(), "a comment is not an event"


def test_shutdown_closes_the_stream_with_a_result(live):
    with live.m.listen({"toolsListChanged": True}, rid=909) as s:
        ack_of(s)
        live.proc.terminate()
        final = s.next(10)
        assert final is not None and final.get("id") == 909 and "method" not in final, final
        assert final["result"]["resultType"] == "complete" and final["result"]["_meta"][SUB_ID] == 909, final
        assert s.next(5) is None, "then the stream ends"


# --- watched tasks ----------------------------------------------------------------------------------------------------
def test_a_watched_task_is_reported_on_the_stream():
    with LocalNode(bucket=E.FIXTURES / "mrtr_group") as n:
        n.wait_serving()
        m = Modern(n.http, WITH_TASKS)
        created = m.call("slow_job", {"seconds": 1, "label": "watched"})
        with m.listen({"taskIds": [created["taskId"]]}, rid=31) as s:
            ack = ack_of(s)
            assert ack["params"]["notifications"] == {"taskIds": [created["taskId"]]}, ack
            events = collect(s, 6)
            tasks = [e for e in events if e["method"] == "notifications/tasks"]
            assert tasks, events
            assert all(e["params"]["taskId"] == created["taskId"] and e["params"]["_meta"][SUB_ID] == 31 for e in tasks)
            assert tasks[-1]["params"]["status"] == "completed", tasks
        with Modern(n.http).listen({"taskIds": [created["taskId"]], "toolsListChanged": True}) as plain:
            assert plain.status == 400 and json.loads(plain.body)["error"]["code"] == -32021, "needs the extension"
