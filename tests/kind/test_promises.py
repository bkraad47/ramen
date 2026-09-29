"""CONTRACTS §13 against a live two-zone cluster: restore, revocation and per-group images (V5.1–V5.3).

Ordering note: the pruning restore takes its backup *after* the `stack` fixture has deployed, so the backup is a
superset of what the later kind/e2e/conformance modules need — prune only removes this module's own probe group.
The restore also bumps every user's session epoch; the acting client is reissued a session, and every other suite
logs in after this module runs.

The service-account half of §13.2 needs real IAM, which kind has none of (RAMEN_NO_CLOUD=iam): the role-grant path
is proven here, the cloud-role path in `tests/cloud/test_permissions.py` and the console unit tests.
"""

import shutil
import subprocess
import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube
from ramen_tests.console import Console
from ramen_tests.mcp_client import text_of

pytestmark = pytest.mark.kind
PW = "Kind-Promises-1!"
TOOL = "demo_calculator_tool"


def _image(group, zone, name="worker") -> str:
    return kube.deployment(kube.ns_name(group, zone), name)["spec"]["template"]["spec"]["containers"][0]["image"]


def _serves(nodes, zone) -> bool:
    n = nodes[zone]
    n.initialize()
    return text_of(n.call_tool(TOOL, {"var1": 2, "var2": 3, "func": "add"})).strip().startswith("5")


# --- §13.3 per-group worker images -------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def pinned_tag(admin, stack, group):
    """Tag the image the cluster already runs under a per-group name and side-load it, exactly as a per-group build
    would. Unpins and redeploys afterwards so the rest of the suite runs the release image again."""
    if not shutil.which("kind") or not shutil.which("docker"):
        pytest.skip("per-group image proof needs kind and docker on PATH")
    current = _image(group, stack["zones"][0])
    alias = f"{current.split(':')[0]}:{current.split(':')[-1]}-{group}"
    cluster = E.env("KIND_CLUSTER", "ramen")
    subprocess.run(["docker", "tag", current, alias], check=True, timeout=120)
    subprocess.run(["kind", "load", "docker-image", alias, "--name", cluster], check=True, timeout=300)
    yield alias
    admin.delete("image_current", group=group)
    job = admin.wait_job(admin.deploy(group, stack["env"]).json()["id"], timeout=900)
    assert job["status"] == "ok", job.get("error")


def test_a_recorded_image_is_what_the_zones_run_and_a_recall_puts_it_back(admin, stack, group, nodes, pinned_tag):
    release = _image(group, stack["zones"][0])
    # no pin yet (a re-run against the same cluster keeps the earlier history, so it is the pin that must be absent)
    assert [h for h in admin.get("images", group=group).json() if h["current"]] == []

    r = admin.post("images", {"tag": pinned_tag, "note": "kind per-group proof"}, group=group)
    assert r.status_code == 201, r.text
    recorded = r.json()
    assert recorded["ref"] == pinned_tag and recorded["current"] is True

    job = admin.wait_job(admin.deploy(group, stack["env"]).json()["id"], timeout=900)
    assert job["status"] == "ok", f"{job.get('error')}\n" + "\n".join(job.get("log", []))
    for zone in stack["zones"]:  # every zone of the group, stable and canary track
        assert _image(group, zone) == pinned_tag, zone
        assert _image(group, zone, "worker-canary") == pinned_tag, zone
        assert _serves(nodes, zone), zone  # the pinned image really runs: the tool answers through it

    assert admin.delete("image_current", group=group).status_code == 200
    job = admin.wait_job(admin.deploy(group, stack["env"]).json()["id"], timeout=900)
    assert job["status"] == "ok", job.get("error")
    assert _image(group, stack["zones"][0]) == release  # unpinned: back to the release image

    assert admin.put("image_current", {"id": recorded["id"]}, group=group).status_code == 200
    history = admin.get("images", group=group).json()
    assert history[0]["id"] == recorded["id"] and history[0]["current"] is True  # recalled (F9.3)
    assert [h["current"] for h in history[1:]] == [False] * len(history[1:])


# --- §13.2 revocation --------------------------------------------------------------------------------------------
def test_a_revoked_role_grant_ends_the_session_it_was_granted_to(admin, stack, group, suffix):
    email = f"promises-{suffix}@ramen.local"
    assert admin.create_user(email, PW, "viewer", [group]).status_code == 201
    with Console(E.require("RAMEN_CONSOLE_URL")) as user:
        assert user.login(email, PW).status_code in (200, 303)
        rid = user.post("requests", {"role": "group_admin", "group": group}).json()["id"]
        assert admin.post("request_approve", id=rid).status_code == 200
        assert user.login(email, PW).status_code in (200, 303)  # a fresh session on the new epoch
        assert user.me().json()["role"] == "group_admin"

        assert admin.post("request_revoke", id=rid).status_code == 200
        assert user.me().status_code == 401  # the open session dies with the grant (U25)
        assert user.login(email, PW).status_code in (200, 303)
        assert user.me().json()["role"] == "viewer"
    assert [q["status"] for q in admin.get("requests").json() if q["id"] == rid] == ["revoked"]
    assert admin.post("request_revoke", id=rid).status_code == 409


def test_a_denied_request_grants_nothing(admin, group, suffix):
    email = f"denied-{suffix}@ramen.local"
    assert admin.create_user(email, PW, "viewer", [group]).status_code == 201
    with Console(E.require("RAMEN_CONSOLE_URL")) as user:
        user.login(email, PW)
        rid = user.post("requests", {"role": "group_admin", "group": group}).json()["id"]
        assert admin.post("request_deny", id=rid).status_code == 200
        assert admin.post("request_approve", id=rid).status_code == 409  # denied is final
        user.login(email, PW)
        assert user.me().json()["role"] == "viewer"


@pytest.mark.skipif(not E.no_cloud("iam"), reason="cloud IAM present: tests/cloud covers the service-account path")
def test_the_service_account_path_is_not_provable_without_iam(admin, stack, group):
    """States the boundary rather than pretending: on kind the approval cannot bind a cloud role, so a permission
    request is refused at the adapter and the revoke route has nothing to take away."""
    zone = stack["zones"][0]
    r = admin.post("requests", {"group": group, "zone": zone, "permission": "bucket.read"})
    assert r.status_code == 201, r.text
    assert admin.post("request_approve", id=r.json()["id"]).status_code in (200, 502)
    got = admin.delete("zone_permission", group=group, zone=zone, permission="bucket.read")
    assert got.status_code in (200, 404)


# --- §13.1 backup restore ----------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def zone_count(admin, stack, group):
    """The restore proof scales a zone and brings the scale back through a backup. Whatever it ends on, the zone is
    handed to the later modules at the count they found it at: the HPA minimum follows this number, and
    `test_rebalance` needs room above it to scale into."""
    zone = stack["zones"][0]
    before = admin.get("workers", group=group, zone=zone).json().get("count", 1)
    yield zone
    assert admin.put("workers", {"count": before}, group=group, zone=zone).status_code == 200


def test_restore_puts_a_populated_deployment_back_and_the_zones_keep_serving(admin, stack, group, nodes, zone_count):
    zone = zone_count
    assert admin.put("workers", {"count": 2}, group=group, zone=zone).status_code == 200
    bid = admin.post("backups", {"target": "local"}).json()["id"]

    probe = f"probe-{stack['deploy_job']['id'][:6]}"[:40]
    assert admin.create_group(probe, "https://example.com/none.git").status_code == 201
    assert admin.put("workers", {"count": 1}, group=group, zone=zone).status_code == 200

    plan = admin.post("backup_restore", {"dry_run": True}, id=bid)
    assert plan.status_code == 200, plan.text
    assert probe in plan.json()["extra"]["groups"]
    assert admin.get("group", group=probe).status_code == 200  # a preview writes nothing

    r = admin.post("backup_restore", {"prune": True, "reconcile": True}, id=bid)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["pruned"]["groups"] == [probe]
    assert f"{group}/{zone}" in body["reconciled"]
    assert admin.get("group", group=probe).status_code == 404
    assert admin.get("group", group=group).status_code == 200
    assert admin.get("workers", group=group, zone=zone).json()["count"] == 2  # the scale came back

    kube.kubectl("-n", kube.ns_name(group, zone), "rollout", "status", "deployment/worker", "--timeout=300s")
    for z in stack["zones"]:
        assert _serves(nodes, z), z  # a restore that reconciles live zones leaves them serving
    deadline = time.monotonic() + 120
    while kube.replicas(kube.ns_name(group, zone))[0] != 2 and time.monotonic() < deadline:
        time.sleep(5)
    assert kube.replicas(kube.ns_name(group, zone))[0] == 2  # and the cluster matches the restored count
    assert any(a["action"] == "backup.restore" and "prune:true" in a["tags"] for a in admin.audit().json())
