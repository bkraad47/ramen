"""Cloud suite fixtures. Needs RAMEN_CONSOLE_URL (else everything skips); RAMEN_NODE_URL enables worker-side
checks; RAMEN_GCP_PROJECT (+ gcloud on PATH) enables the GCP resource assertions."""
import time

import httpx
import pytest

from ramen_tests import env as E
from ramen_tests import state
from ramen_tests.console import items

GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
ZONE = E.env("RAMEN_E2E_ZONE", "local")


def ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    return r


def poll(fn, timeout=90, every=3, what="condition"):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"{what} not met after {timeout}s (last={last!r})")


@pytest.fixture(scope="session")
def gcp_project() -> str:
    return E.require("RAMEN_GCP_PROJECT")


@pytest.fixture(scope="session")
def node_opt() -> str | None:
    return E.strip(E.env("RAMEN_NODE_URL")) if E.env("RAMEN_NODE_URL") else None


@pytest.fixture(scope="session")
def node_http(node_opt):
    if not node_opt:
        pytest.skip("RAMEN_NODE_URL not set")
    with httpx.Client(base_url=node_opt, verify=E.tls_verify(), timeout=30) as c:
        yield c


@pytest.fixture(scope="session")
def node_admin_http(node_http, node_opt):
    """Bare node only (health/metrics/admin). Skips for an MCP-only LB route."""
    if not E.node_admin(node_opt):
        pytest.skip(f"{node_opt} is an MCP-only route (LB); health/metrics/admin are not exposed there")
    return node_http


def mcp_post(node_http: httpx.Client, body: dict, key: str | None) -> httpx.Response:
    headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    return node_http.post(E.mcp_url(str(node_http.base_url)), json=body, headers=headers)


@pytest.fixture(scope="session")
def mcp_key_opt() -> str | None:
    return state.MCP_KEY or E.env("RAMEN_MCP_KEY")


@pytest.fixture(scope="package")
def world(admin, demo_repo, node_opt):
    """Zone/group/env exist and the env has one successful deploy (so a main worker is serving)."""
    ok(admin.create_zone(ZONE), 201, 409)
    ok(admin.create_group(GROUP, demo_repo), 201, 409)
    ok(admin.create_environment(GROUP, ENV, [ZONE]), 201, 409)
    envs = items(ok(admin.get("environments_all", params={"group": GROUP})))
    e = next(x for x in envs if x["name"] == ENV)
    if (e.get("last_deploy") or {}).get("status") != "ok":
        job = admin.wait_job(ok(admin.deploy(GROUP, ENV, canary=True), 202).json()["id"])
        assert job["status"] == "ok", job
    if node_opt and E.node_admin(node_opt):
        poll(lambda: httpx.get(f"{node_opt}/readyz", verify=E.tls_verify(), timeout=5).status_code == 200,
             timeout=180, what=f"{node_opt}/readyz")
    elif node_opt:
        poll(lambda: httpx.post(E.mcp_url(node_opt), json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                                verify=E.tls_verify(), timeout=15).status_code in (200, 401),
             timeout=180, what=f"{E.mcp_url(node_opt)} answering")
    return {"group": GROUP, "env": ENV, "zone": ZONE}


def metrics(node_http) -> dict:
    return node_http.get("/metrics").json()
