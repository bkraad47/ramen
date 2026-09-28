"""Canary rollout (CONTRACTS §7 deploy): a bad ref/broken package fails the job, the canary is scaled to 0 and the
main worker keeps serving; a good ref then succeeds. Needs RAMEN_CONSOLE_URL; worker checks need RAMEN_NODE_URL."""

import pytest

from ramen_tests import env as E
from ramen_tests.console import items
from ramen_tests.mcp_client import INIT_BODY

from .conftest import GROUP, ZONE, mcp_post, metrics, ok

pytestmark = pytest.mark.cloud
BAD_REF = "ramen-no-such-ref-0badc0de"


@pytest.fixture(scope="module")
def canary_env(admin, world, suffix):
    name = f"canary{suffix}"
    ok(admin.create_environment(GROUP, name, [ZONE], ref=BAD_REF), 201, 409)
    yield name
    admin.delete("environment", group=GROUP, env=name)


def _canary_workers(admin):
    live = ok(admin.get("workers", group=GROUP, zone=ZONE)).json()["live"]
    return [w for w in live if w.get("track") == "canary" or "canary" in str(w.get("id", "")).lower()]


def _serving(node_http, key):
    if E.node_admin(str(node_http.base_url)):
        m = metrics(node_http)
        return m["packages"]["tools"] >= 1 and node_http.get("/readyz").status_code == 200
    return mcp_post(node_http, INIT_BODY, key).status_code == (200 if key else 401)


def test_bad_ref_fails_job_and_records_error(admin, canary_env):
    job = admin.wait_job(ok(admin.deploy(GROUP, canary_env, canary=True), 202).json()["id"])
    assert job["status"] == "error", job
    assert job["error"], job
    envs = items(ok(admin.get("environments_all", params={"group": GROUP})))
    e = next(x for x in envs if x["name"] == canary_env)
    assert e["last_deploy"]["status"] == "error" and e["last_deploy"]["job"] == job["id"]
    audit = items(ok(admin.audit()))
    assert any(
        a.get("action") == "deploy" and a.get("ok") is False and f"job:{job['id']}" in a.get("tags", []) for a in audit
    )


def test_failed_canary_is_scaled_to_zero(admin, canary_env, gcp_project):
    """GCP only: after the failure no canary pod is live (local adapter has no canary deployment)."""
    live = _canary_workers(admin)
    assert all(w.get("load") == "down" for w in live), live


def test_main_worker_still_serves_after_failed_canary(node_http, canary_env, world, mcp_key_opt):
    assert _serving(node_http, mcp_key_opt)
    r = mcp_post(node_http, {"jsonrpc": "2.0", "id": 1, "method": "ping"}, "definitely-not-a-key")
    assert r.status_code == 401, "worker must still answer (auth layer alive)"


def test_good_ref_then_succeeds(admin, canary_env, node_opt):
    ok(admin.put("environment", {"ref": "main"}, group=GROUP, env=canary_env))
    job = admin.wait_job(ok(admin.deploy(GROUP, canary_env, canary=True), 202).json()["id"])
    assert job["status"] == "ok", job
    envs = items(ok(admin.get("environments_all", params={"group": GROUP})))
    e = next(x for x in envs if x["name"] == canary_env)
    assert e["last_deploy"]["status"] == "ok"
    if E.env("RAMEN_GCP_PROJECT"):
        canary = _canary_workers(admin)
        assert any(w.get("load") != "down" for w in canary), f"canary should be at 1 after success: {canary}"


def test_deploy_without_canary_also_succeeds(admin, canary_env):
    job = admin.wait_job(ok(admin.deploy(GROUP, canary_env, canary=False), 202).json()["id"])
    assert job["status"] == "ok", job
