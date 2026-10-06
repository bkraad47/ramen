"""Brief 0.7.0 item 2 (R2): does a tool change reach the clients of a running worker?

On real `ramen-node` processes: the bucket's `mcp/tools` changes on disk (one package added, one removed), the node is
told with `Admin/Reload` (exactly what the console does on deploy), and the SAME MCP session — raw Streamable HTTP
with its `Mcp-Session-Id`, the official SDK client, a running bridge — lists the new set, calls the new tool and is
refused the removed one. In 0.7.0 the node declared no `listChanged` and a client learned of a change only when it
listed again (`reports/tool-propagation-v0.7.0.md`); since 0.7.2 (C11) the notice rides on the session's next POST —
`test_rollout_notify.py` covers that; here the propagation itself."""

import hashlib
import json
import re
import shutil

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import (
    JsonRpcError,
    bridge_command,
    bridge_session,
    http_session,
    sdk_text,
    text_of,
)

pytestmark = pytest.mark.conformance
OLD, NEW = "demo_calculator_tool", "slow_tool"
ARGS = {"var1": 2, "var2": 3, "func": "add"}
IGNORE = shutil.ignore_patterns("__pycache__")


def names(tools) -> set[str]:
    return {t["name"] if isinstance(t, dict) else t.name for t in tools}


def swap_tools(bucket):
    """The deploy a group admin would make: `slow_tool` added, `demo_calculator_tool` deleted."""
    shutil.copytree(E.FIXTURES / "slow_group" / "mcp" / "tools" / NEW, bucket / "mcp" / "tools" / NEW, ignore=IGNORE)
    shutil.rmtree(bucket / "mcp" / "tools" / OLD)


@pytest.fixture
def live(tmp_path):
    """A fresh node on a private copy of the demo bucket (each test mutates it)."""
    bucket = tmp_path / "bucket"
    shutil.copytree(E.FIXTURES / "demo_group", bucket, ignore=IGNORE)
    with LocalNode(bucket=bucket, env={"RAMEN_SESSION_SECRET": "conformance-secret"}) as n:
        n.wait_serving()
        n.bucket = bucket
        yield n


def test_same_http_session_lists_the_new_tool_set_after_reload(live):
    c = live.http
    c.initialize()
    sid = c.session_id
    assert names(c.list_tools()) == {OLD}
    swap_tools(live.bucket)
    assert names(c.list_tools()) == {OLD}, "a change on disk alone is invisible: the runtime serves what it loaded"
    res = live.node.admin_reload()
    assert res["errors"] == [], res["errors"]
    assert c.session_id == sid  # nothing re-initialised; the id minted before the change still rides along
    assert names(c.list_tools()) == {NEW}
    assert text_of(c.call_tool(NEW, {"ms": 10})) == "slept 10 ms"
    with pytest.raises(JsonRpcError) as e:
        c.call_tool(OLD, ARGS)
    assert e.value.code == -32004, e.value  # the runtime's "unknown tool" (unknown METHODS are the node's -32601)
    assert c.ping() == {}


async def test_sdk_client_sees_the_change_only_when_it_lists_again(live):
    async with http_session(live.http) as s:
        caps = s.server_capabilities
        if not (caps.tools and caps.tools.list_changed):
            pytest.xfail("waiting for worker-agent (C11): no tools.listChanged in initialize")
        assert names((await s.list_tools()).tools) == {OLD}
        swap_tools(live.bucket)
        live.node.admin_reload()
        assert names((await s.list_tools()).tools) == {NEW}
        r = await s.call_tool(NEW, {"ms": 10})
        assert not r.is_error and sdk_text(r) == "slept 10 ms"
        with pytest.raises(Exception, match="-32004|not found|unknown"):  # McpError for the JSON-RPC error object
            await s.call_tool(OLD, ARGS)
        assert await s.send_ping()  # the session is alive


@pytest.mark.skipif(not bridge_command(), reason="ramen-mcp-bridge not found")
async def test_running_bridge_sees_the_new_tool_on_its_next_list(live):
    async with bridge_session(live.node) as s:
        assert names((await s.list_tools()).tools) == {OLD}
        swap_tools(live.bucket)
        live.node.admin_reload()
        assert names((await s.list_tools()).tools) == {NEW}, "the bridge is stateless: every list is the node's"
        r = await s.call_tool(NEW, {"ms": 10})
        assert not r.is_error and sdk_text(r) == "slept 10 ms"
        with pytest.raises(Exception, match="-32004|not found|unknown"):
            await s.call_tool(OLD, ARGS)
        assert await s.send_ping()


def test_change_notice_rides_on_the_next_post_not_on_a_stream(live):
    """What an AI client is up against: `initialize` promises `tools.listChanged` (C11), but `GET /mcp` (the
    server→client stream) stays 405 — the notice arrives on the session's next POST, so a client that never calls
    again keeps its cached list."""
    caps = live.http.initialize()["capabilities"]
    if caps.get("tools") == {}:
        pytest.xfail("waiting for worker-agent (C11): no tools.listChanged in initialize")
    assert caps == {"tools": {"listChanged": True}, "resources": {}, "prompts": {}}, caps
    r = live.http.get()
    assert (r.status_code, r.headers.get("Allow")) == (405, "POST, DELETE, OPTIONS")


def canonical_hash(result: dict) -> str:
    """C1: sha256 of the canonical JSON of tools+resources+prompts, prompts without `_meta`."""
    body = {
        "tools": result["tools"],
        "resources": result["resources"],
        "prompts": [{k: v for k, v in p.items() if k != "_meta"} for p in result["prompts"]],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def test_reload_result_and_metrics_carry_the_same_manifest_hash(live):
    """B2/B4: the hash the console stores per deploy is the hash the node reports on Metrics, and it moves with the
    tool set — so two workers of a zone that disagree can be told apart."""
    before = live.node.admin_reload()
    assert re.fullmatch(r"[0-9a-f]{64}", before["hash"]) and before["hash"] == canonical_hash(before)
    assert live.node.admin_metrics()["manifest_hash"] == before["hash"]
    swap_tools(live.bucket)
    after = live.node.admin_reload()
    assert after["hash"] == canonical_hash(after) != before["hash"]
    assert names(after["tools"]) == {NEW}
    assert live.node.admin_metrics()["manifest_hash"] == after["hash"]
