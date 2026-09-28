"""CONTRACTS §9 tool blocking end to end: block demo_calculator_tool on the deployed env → deploy → the node hides it
from tools/list and answers -32601 → unblock → deploy → back. Runs after test_demo_flow (same group/env/zone, same key).
Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL (+ key from the e2e deploy or RAMEN_MCP_KEY)."""

import pytest

from ramen_tests import env as E
from ramen_tests.mcp_client import session

pytestmark = pytest.mark.e2e
GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
TOOL = "demo_calculator_tool"


def ok(r, *codes):
    assert r.status_code in codes, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    return r


async def tool_names(node_url, key) -> list[str]:
    async with session(node_url, key) as s:
        return sorted(t.name for t in (await s.list_tools()).tools)


async def call_blocked(node_url, key) -> str:
    async with session(node_url, key) as s:
        try:
            r = await s.call_tool(TOOL, {"var1": 2, "var2": 3, "func": "add"})
        except Exception as e:  # noqa: BLE001 - the SDK raises McpError for a JSON-RPC error
            return f"{type(e).__name__}: {e}"
        return "isError" if r.is_error else "ok"


def deploy(admin, blocked):
    ok(admin.put("env_blocked", {"blocked": blocked}, group=GROUP, env=ENV), 200)
    job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=False), 202).json()["id"])
    assert job["status"] == "ok", job


async def test_block_hides_and_denies_then_unblock_restores(admin, node_url, mcp_key):
    assert TOOL in await tool_names(node_url, mcp_key), "run e2e/test_demo_flow first (deploys the demo group)"
    deploy(admin, [TOOL])
    try:
        assert TOOL not in await tool_names(node_url, mcp_key)
        out = await call_blocked(node_url, mcp_key)
        assert "32601" in out or "not found" in out or out == "isError", out
        envs = [e for e in admin.get("environments_all", params={"group": GROUP}).json() if e["name"] == ENV]
        assert envs[0]["blocked"] == [TOOL]
    finally:
        deploy(admin, [])
    assert TOOL in await tool_names(node_url, mcp_key)
    assert await call_blocked(node_url, mcp_key) == "ok"
