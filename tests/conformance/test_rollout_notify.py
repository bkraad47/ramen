"""C11 (0.7.2, B1-lite): a running client learns that the tool set changed, on real node processes.

The session id minted at `initialize` is `<nonce>.<expiry>.<hash12>.<mac>` with `hash12` = the manifest hash the node
served. After `mcp/tools` changes on disk and `Admin/Reload` (what a deploy does), the next POST of that session that
accepts `text/event-stream` is answered as SSE: event 1 `notifications/tools/list_changed`, event 2 the JSON-RPC
response. The POST after that is plain JSON again; a JSON-only client never sees SSE; a 0.7.1 three-part id is still
accepted; the official SDK client completes a flow across the reload. Findings: `reports/tool-access-v0.7.2.md`."""

import re
import shutil

import pytest
from mcp import types

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import http_session, sdk_text, sse_events, text_of
from ramen_tests.tokens import legacy_session_id

pytestmark = pytest.mark.conformance
SECRET = "conformance-secret"
OLD, NEW = "demo_calculator_tool", "slow_tool"
IGNORE = shutil.ignore_patterns("__pycache__")
BOTH = [("Accept", "application/json, text/event-stream")]
JSON_ONLY = [("Accept", "application/json")]
LIST_CHANGED = {"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}


def names(tools) -> set[str]:
    return {t["name"] if isinstance(t, dict) else t.name for t in tools}


def swap_tools(bucket):
    shutil.copytree(E.FIXTURES / "slow_group" / "mcp" / "tools" / NEW, bucket / "mcp" / "tools" / NEW, ignore=IGNORE)
    shutil.rmtree(bucket / "mcp" / "tools" / OLD)


def rollout(live):
    """What a deploy does to a running worker: a new tool set on disk, then `Admin/Reload`."""
    swap_tools(live.bucket)
    res = live.node.admin_reload()
    assert res["errors"] == [], res["errors"]
    return res["hash"]


def list_body(rid: int) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/list"}


@pytest.fixture
def live(tmp_path):
    bucket = tmp_path / "bucket"
    shutil.copytree(E.FIXTURES / "demo_group", bucket, ignore=IGNORE)
    with LocalNode(bucket=bucket, env={"RAMEN_SESSION_SECRET": SECRET}) as n:
        n.wait_serving()
        n.bucket = bucket
        n.http.initialize()
        if n.http.session_id.count(".") != 3:  # the 4-part id is what carries the manifest hash the notice keys on
            pytest.xfail("waiting for worker-agent (C11): session ids are still 3-part, no rollout notice yet")
        n.http.session_id = None  # each test initializes its own session
        yield n


def test_initialize_declares_list_changed_and_binds_the_session_to_the_manifest(live):
    c = live.http
    caps = c.initialize()["capabilities"]
    assert caps["tools"] == {"listChanged": True}, caps
    parts = c.session_id.split(".")
    assert len(parts) == 4, c.session_id
    assert re.fullmatch(r"[0-9a-f]{12}", parts[2]) and parts[1].isdigit()
    assert parts[2] == live.node.admin_metrics()["manifest_hash"][:12]
    assert c.ping() == {} and c.last_response.headers["content-type"].startswith("application/json")


def test_first_post_after_a_rollout_is_sse_with_list_changed_then_the_response(live):
    c = live.http
    c.initialize()
    sid = c.session_id
    assert names(c.list_tools()) == {OLD}
    new_hash = rollout(live)
    r = c.post(list_body(7), extra=BOTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream"), r.headers
    events = sse_events(r.text)
    assert events[0] == LIST_CHANGED, events
    assert events[1]["id"] == 7 and names(events[1]["result"]["tools"]) == {NEW}, events
    assert len(events) == 2
    # once per session: the next POST is plain JSON again, and the session id never changed
    r2 = c.post(list_body(8), extra=BOTH)
    assert r2.headers["content-type"].startswith("application/json") and names(r2.json()["result"]["tools"]) == {NEW}
    assert c.session_id == sid
    assert text_of(c.call_tool(NEW, {"ms": 1})) == "slept 1 ms"
    # a session started after the rollout carries the new hash and is never notified
    fresh = live.http_client()
    fresh.initialize()
    assert fresh.session_id.split(".")[2] == new_hash[:12]
    r3 = fresh.post(list_body(9), extra=BOTH)
    assert r3.headers["content-type"].startswith("application/json")


def test_json_only_client_gets_plain_json_after_a_rollout(live):
    c = live.http
    c.initialize()
    rollout(live)
    r = c.post(list_body(5), extra=JSON_ONLY)
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/json"), r.headers
    assert names(r.json()["result"]["tools"]) == {NEW}, "the current set is served; the notice is simply not sent"


def test_legacy_three_part_session_id_is_still_accepted(live):
    """A session minted by a 0.7.1 pod survives the rollout to 0.7.2 (`hash12 = ""`), on the same key."""
    c = live.http
    c.session_id = legacy_session_id(SECRET, live.key)
    assert c.ping() == {}, c.last_response.text
    assert c.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}, key="other") in (401, 404)
    c.session_id = legacy_session_id(SECRET, "another-key")
    assert c.status({"jsonrpc": "2.0", "id": 1, "method": "ping"}) == 404, "still bound to its credential"


async def test_sdk_client_is_notified_and_completes_a_flow_after_a_rollout(live):
    """The official SDK sends both Accept types: it receives the notice on its next call and keeps working."""
    seen = []

    async def handler(message):
        seen.append(message)

    async with http_session(live.http, message_handler=handler) as s:
        assert s.server_capabilities.tools.list_changed is True
        assert names((await s.list_tools()).tools) == {OLD}
        rollout(live)
        assert names((await s.list_tools()).tools) == {NEW}
        r = await s.call_tool(NEW, {"ms": 1})
        assert not r.is_error and sdk_text(r) == "slept 1 ms"
        assert await s.send_ping()
    kinds = [type(m).__name__ for m in seen]
    assert any(isinstance(m, types.ToolListChangedNotification) for m in seen), kinds
