"""CONTRACTS §9 tool blocking end to end: block demo_calculator_tool on the deployed env → deploy → the node hides it
from tools/list and answers -32601 → unblock → deploy → back. Runs after test_demo_flow (same group/env/zone, same key).
Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL (+ key from the e2e deploy or RAMEN_MCP_KEY)."""

import time

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


def fresh_calls(node, n: int = 8) -> list[str]:
    """`call_blocked` over `n` brand-new connections.

    One gRPC channel is one TCP connection and a Service in front of a zone selects **both** tracks
    (`app=worker`, CONTRACTS §7), so a single reused channel only ever reaches one pod. A blocked name has to be
    denied by every worker of the zone or it is not blocked.
    """
    from ramen_tests.mcp_client import Node

    out = []
    for _ in range(n):
        with Node(
            node.target, node.key, group=node.group, zone=node.zone, tls=node.tls, ca=node._ca, timeout=node.timeout
        ) as c:  # ca: a fresh channel through a self-signed LB failed CERTIFICATE_VERIFY on the 0.5.1 GKE run
            out.append(call_blocked(c))
    return out


def settled(node, hidden: bool, timeout: float = 90) -> list[str]:
    """tools/list once the LB has stopped routing to the pre-deploy pods (they drain a few seconds after the job)."""
    deadline, names = time.monotonic() + timeout, tool_names(node)
    while (TOOL not in names) != hidden and time.monotonic() < deadline:
        time.sleep(3)
        names = tool_names(node)
    return names


def test_block_hides_and_denies_then_unblock_restores(admin, node):
    assert TOOL in tool_names(node), "run e2e/test_demo_flow first (deploys the demo group)"
    deploy(admin, [TOOL])
    try:
        assert TOOL not in settled(node, hidden=True)
        assert call_blocked(node) == "jsonrpc -32601"
        envs = [e for e in admin.get("environments_all", params={"group": GROUP}).json() if e["name"] == ENV]
        assert envs[0]["blocked"] == [TOOL]
    finally:
        deploy(admin, [])
    assert TOOL in settled(node, hidden=False)


def test_a_blocked_tool_is_denied_by_every_worker_in_the_zone(admin, node):
    """§9: blocking is a control, so it has to hold for every connection into the zone, not for most of them.

    This is the case that the single-channel check above cannot see. It fails today when the environment was
    deployed with `canary:false`, because that path never restarts `worker-canary` and the zone Service routes to
    it, so the previous configuration — including the unblocked tool — keeps being served.
    """
    deploy(admin, [TOOL])
    try:
        settled(node, hidden=True)
        got = fresh_calls(node)
        assert set(got) == {"jsonrpc -32601"}, (
            f"a blocked tool was still executed on some connections: {got}\n"
            "DEFECT stale-canary: a deploy with canary:false leaves worker-canary running the previous config, and "
            "the zone Service selects both tracks, so the block only applies to the share of traffic that lands on "
            "a stable pod. Scale the canary to 0, or roll it too, when a deploy is not using it."
        )
    finally:
        deploy(admin, [])
    assert call_blocked(node) == "ok"
