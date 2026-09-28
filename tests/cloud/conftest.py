"""Cloud suite fixtures. Needs RAMEN_CONSOLE_URL (else everything skips); RAMEN_NODE_URL (gRPC host:port) enables
worker-side checks; RAMEN_GCP_PROJECT (+ gcloud on PATH) enables the GCP resource assertions."""

import time

import grpc
import pytest

from ramen_tests import env as E
from ramen_tests import state
from ramen_tests.console import items
from ramen_tests.mcp_client import Node

GROUP = E.env("RAMEN_E2E_GROUP", "demo")
ENV = E.env("RAMEN_E2E_ENV", "dev")
ZONE = E.env("RAMEN_E2E_ZONE", "local")
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
S = grpc.StatusCode


def ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), (
        f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    )
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
def mcp_key_opt() -> str | None:
    return state.MCP_KEY or E.env("RAMEN_MCP_KEY")


@pytest.fixture(scope="session")
def node_opt(mcp_key_opt) -> Node | None:
    """gRPC client for RAMEN_NODE_URL (None when unset). Uses the e2e-minted key / RAMEN_MCP_KEY when present."""
    if not E.env("RAMEN_NODE_URL"):
        yield None
        return
    with Node.from_env(mcp_key_opt, group=GROUP, zone=ZONE) as n:
        yield n


@pytest.fixture(scope="session")
def node_grpc(node_opt) -> Node:
    if node_opt is None:
        pytest.skip("RAMEN_NODE_URL not set")
    return node_opt


def mcp_status(node: Node, key) -> S:
    """gRPC status of an authenticated-as-`key` ping (OK / UNAUTHENTICATED / PERMISSION_DENIED / ...)."""
    return node.status(PING, key=key)


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
    if node_opt:
        poll(lambda: node_opt.health_code() == S.OK and node_opt.health() == "SERVING", timeout=180, what="SERVING")
    return {"group": GROUP, "env": ENV, "zone": ZONE}
