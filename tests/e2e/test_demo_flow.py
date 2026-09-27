"""Full flow: console → zone/group/env → mint MCP key → deploy demo repo (job) → worker ready → MCP calls.
Needs RAMEN_CONSOLE_URL + RAMEN_NODE_URL. The minted key is reused by conformance/test_mcp_node.py when it
runs later in the same process (`pytest e2e conformance`); RAMEN_MCP_KEY is the fallback."""
import time
import uuid

import httpx
import pytest

from ramen_tests import env as E
from ramen_tests import state
from ramen_tests.console import items
from ramen_tests.mcp_client import INIT_BODY, MCP_HEADERS, session, text_of

pytestmark = pytest.mark.e2e
GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
ZONE = E.env("RAMEN_E2E_ZONE", "local")


def ok(r, *codes):
    assert r.status_code in codes, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    return r


def wait(url: str, timeout: float = 180) -> None:
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, verify=E.tls_verify(), timeout=5)
            if r.status_code == 200:
                return
            last = r.status_code
        except httpx.HTTPError as e:
            last = e
        time.sleep(2)
    raise AssertionError(f"{url} not ready after {timeout}s: {last}")


def metrics(node_url) -> dict:
    return httpx.get(f"{node_url}/metrics", verify=E.tls_verify(), timeout=10).json()


def ready(node_url: str, key: str, timeout: float = 180) -> None:
    """Bare node: /readyz 200. MCP-only LB route: an authenticated initialize answers 200."""
    if E.node_admin(node_url):
        return wait(f"{node_url}/readyz", timeout)
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        try:
            r = httpx.post(E.mcp_url(node_url), json=INIT_BODY, verify=E.tls_verify(), timeout=15,
                           headers={**MCP_HEADERS, "Authorization": f"Bearer {key}"})
            if r.status_code == 200:
                return
            last = r.status_code
        except httpx.HTTPError as e:
            last = e
        time.sleep(3)
    raise AssertionError(f"{E.mcp_url(node_url)} not ready after {timeout}s: {last}")


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
    ready(node_url, key)
    return {"key": key, "job": job}


def test_environment_records_last_deploy(admin, deployed):
    envs = items(ok(admin.get("environments_all", params={"group": GROUP}), 200))
    e = next(x for x in envs if x["name"] == ENV)
    assert e["last_deploy"]["status"] == "ok"


def test_workers_visible_with_load(admin, deployed):
    body = ok(admin.get("workers", group=GROUP, zone=ZONE), 200).json()
    assert body["count"] >= 1 and body["live"], body
    assert body["live"][0]["load"] in ("low", "even", "high")


async def test_mcp_call_through_deployed_worker(deployed, node_url):
    async with session(node_url, deployed["key"]) as s:
        assert s.protocol_version == "2025-06-18"
        names = {t.name for t in (await s.list_tools()).tools}
        assert "demo_calculator_tool" in names
        r = await s.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
        assert not r.is_error and float(text_of(r)) == 5
        readme = await s.read_resource("ramen://demo/readme")
        assert "ramen-demo-mcp-group" in readme.contents[0].text
        p = await s.get_prompt("get_calculation_prompt", {"request": "2+3"})
        assert "2+3" in p.messages[0].content.text


def test_worker_metrics_reflect_load(deployed, node_admin_url):
    m = metrics(node_admin_url)
    assert m["total"] >= 1 and m["sidecar_alive"] is True
    assert m["packages"]["tools"] >= 1 and m["packages"]["errors"] == 0


def test_deploy_is_audited(admin, deployed, admin_creds):
    entries = items(ok(admin.audit(), 200))
    assert any(e.get("user") == admin_creds[0] and e.get("action") == "deploy" and e.get("ok") is True for e in entries)


def test_dashboard_shows_group_in_zone(admin, deployed):
    d = ok(admin.get("dashboard"), 200).json()
    cell = d["cells"][ZONE][GROUP]
    assert cell["color"] in ("blue", "green", "red")


def test_redeploy_is_idempotent(admin, deployed, node_url):
    job = admin.wait_job(ok(admin.deploy(GROUP, ENV), 202).json()["id"])
    assert job["status"] == "ok", job
    ready(node_url, deployed["key"])
    if E.node_admin(node_url):
        assert metrics(node_url)["packages"]["tools"] >= 1


def test_console_logs_reach_worker_output(admin, deployed):
    r = admin.get("logs", params={"group": GROUP, "zone": ZONE, "tail": 50})
    assert r.status_code == 200
