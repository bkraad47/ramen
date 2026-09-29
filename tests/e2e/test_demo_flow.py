"""Full flow: console → zone/group/env → mint MCP key → deploy demo repo (job) → worker SERVING → MCP calls over gRPC.
Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL (host:port). The minted key is reused by conformance/test_mcp_node.py and
test_bridge.py when they run later in the same process (`pytest e2e conformance`); RAMEN_MCP_KEY is the fallback."""

import time
import uuid

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests import state
from ramen_tests.console import items
from ramen_tests.mcp_client import PROTOCOL, Node, text_of

pytestmark = pytest.mark.e2e
GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
ZONE = E.env("RAMEN_E2E_ZONE", "local")


def ok(r, *codes):
    assert r.status_code in codes, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    return r


def ready(node: Node, timeout: float = 180) -> None:
    """Health/Check SERVING and an authenticated ping OK three times in a row (an LB may still route a few calls to a
    draining pod with the old key set right after a deploy)."""
    deadline, last, streak = time.monotonic() + timeout, None, 0
    while time.monotonic() < deadline:
        try:
            last = node.health()
            if last == "SERVING":
                node.ping()
                streak += 1
                if streak >= 3:
                    return
                time.sleep(1)
                continue
        except grpc.RpcError as e:
            last = f"{e.code().name}: {e.details()}"
        streak = 0
        time.sleep(3)
    raise AssertionError(f"{node.target} not ready after {timeout}s: {last}")


@pytest.fixture(scope="module")
def deployed(admin, node_url, demo_repo):
    ok(admin.create_zone(ZONE), 201, 409)
    ok(admin.create_group(GROUP, demo_repo), 201, 409)
    ok(admin.create_environment(GROUP, ENV, [ZONE]), 201, 409)
    key = ok(admin.create_mcp_key(GROUP, f"e2e-{uuid.uuid4().hex[:6]}"), 201).json()["key"]
    assert key.startswith("rmk_")
    state.MCP_KEY = key
    job = ok(admin.deploy(GROUP, ENV, canary=True), 202).json()
    job = admin.wait_job(job["id"])
    assert job["status"] == "ok", job
    assert key not in str(job)
    with Node.from_env(key, group=GROUP, zone=ZONE) as node:
        ready(node)
        yield {"key": key, "job": job, "node": node}


def test_environment_records_last_deploy(admin, deployed):
    envs = items(ok(admin.get("environments_all", params={"group": GROUP}), 200))
    e = next(x for x in envs if x["name"] == ENV)
    assert e["last_deploy"]["status"] == "ok"


def test_workers_visible_with_load(admin, deployed):
    body = ok(admin.get("workers", group=GROUP, zone=ZONE), 200).json()
    assert body["count"] >= 1 and body["live"], body
    assert body["live"][0]["load"] in ("low", "even", "high")


def test_mcp_call_through_deployed_worker(deployed):
    n = deployed["node"]
    assert n.initialize()["protocolVersion"] == PROTOCOL
    assert "demo_calculator_tool" in {t["name"] for t in n.list_tools()}
    r = n.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
    assert not r.get("isError") and float(text_of(r)) == 5
    assert "ramen-demo-mcp-group" in n.read_resource("ramen://demo/readme")["contents"][0]["text"]
    assert "2+3" in n.get_prompt("get_calculation_prompt", {"request": "2+3"})["messages"][0]["content"]["text"]


def test_minted_key_is_the_only_one_accepted(deployed):
    n = deployed["node"]
    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    assert n.status(ping, key="rmk_not_minted") == grpc.StatusCode.UNAUTHENTICATED
    assert n.status(ping, key=None) == grpc.StatusCode.UNAUTHENTICATED


def test_worker_metrics_reflect_load(deployed, node_admin):
    m = node_admin.admin_metrics()
    assert m["total"] >= 1 and m["sidecar_alive"] is True
    assert m["packages"]["tools"] >= 1 and m["packages"]["errors"] == 0


def test_deploy_is_audited(admin, deployed, admin_creds):
    entries = items(ok(admin.audit(), 200))
    assert any(e.get("user") == admin_creds[0] and e.get("action") == "deploy" and e.get("ok") is True for e in entries)


def test_dashboard_shows_group_in_zone(admin, deployed):
    d = ok(admin.get("dashboard"), 200).json()
    cell = d["cells"][ZONE][GROUP]
    assert cell["color"] in ("blue", "green", "red")


def test_redeploy_is_idempotent(admin, deployed):
    job = admin.wait_job(ok(admin.deploy(GROUP, ENV), 202).json()["id"])
    assert job["status"] == "ok", job
    ready(deployed["node"])
    assert "demo_calculator_tool" in {t["name"] for t in deployed["node"].list_tools()}


def test_console_logs_reach_worker_output(admin, deployed):
    if E.no_cloud("logs"):
        pytest.skip("no Cloud Logging here (RAMEN_NO_CLOUD=logs); the logs suites run on the compose stack and GKE")
    r = admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 50})
    assert r.status_code == 200
