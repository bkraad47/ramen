"""CONTRACTS §9 tool blocking end to end: block demo_calculator_tool on the deployed env → deploy → the node hides it
from tools/list and answers -32601 → unblock → deploy → back. Runs after test_demo_flow (same group/env/zone, same key).
Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL (+ key from the e2e deploy or RAMEN_MCP_KEY)."""

import pytest

from ramen_tests import env as E
from ramen_tests.mcp_client import JsonRpcError

pytestmark = pytest.mark.e2e
GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
TOOL = "demo_calculator_tool"


def ok(r, *codes):
    assert r.status_code in codes, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    return r


def tool_names(node) -> list[str]:
    return sorted(t["name"] for t in node.list_tools())


def call_blocked(node) -> str:
    try:
        r = node.call_tool(TOOL, {"var1": 2, "var2": 3, "func": "add"})
    except JsonRpcError as e:
        return f"jsonrpc {e.code}"
    return "isError" if r.get("isError") else "ok"


def deploy(admin, blocked):
    ok(admin.put("env_blocked", {"blocked": blocked}, group=GROUP, env=ENV), 200)
    job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=False), 202).json()["id"])
    assert job["status"] == "ok", job


def test_block_hides_and_denies_then_unblock_restores(admin, node):
    assert TOOL in tool_names(node), "run e2e/test_demo_flow first (deploys the demo group)"
    deploy(admin, [TOOL])
    try:
        assert TOOL not in tool_names(node)
        assert call_blocked(node) == "jsonrpc -32601"
        envs = [e for e in admin.get("environments_all", params={"group": GROUP}).json() if e["name"] == ENV]
        assert envs[0]["blocked"] == [TOOL]
    finally:
        deploy(admin, [])
    assert TOOL in tool_names(node)
    assert call_blocked(node) == "ok"
