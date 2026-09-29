"""CONTRACTS §13.2 on a real cloud: an approved permission binds its mapped role, a revoke unbinds it, and the
zone identity keeps its baseline roles. Needs RAMEN_CONSOLE_URL + RAMEN_GCP_PROJECT (+ gcloud on PATH)."""

import pytest

from ramen_tests import gcp
from ramen_tests.console import items

from .conftest import GROUP, ZONE, ok, poll

pytestmark = pytest.mark.cloud
PERMISSION = "logs.write"  # project-wide and not a baseline role, so binding and unbinding are both observable
ROLE = "roles/logging.logWriter"
BASELINE = {"roles/storage.objectViewer", "roles/secretmanager.secretAccessor"}


@pytest.fixture(scope="module")
def granted(admin, world) -> dict:
    r = ok(admin.post("requests", {"group": GROUP, "zone": ZONE, "permission": PERMISSION}), 201).json()
    applied = ok(admin.post("request_approve", id=r["id"])).json()
    assert applied["status"] == "approved", applied
    return {"request": r["id"], "applied": applied.get("applied") or {}}


def _member(gcp_project, granted) -> str:
    email = granted["applied"].get("service_account") or gcp.gsa(gcp_project, GROUP, ZONE)
    return f"serviceAccount:{email}"


def test_the_approved_permission_is_bound_on_the_project(admin, gcp_project, granted):
    member = _member(gcp_project, granted)
    poll(
        lambda: ROLE in gcp.roles_of(gcp.get("projects", "get-iam-policy", gcp_project, project=gcp_project), member),
        timeout=120,
        what=f"{ROLE} bound to {member}",
    )
    assert ok(admin.get("workers", group=GROUP, zone=ZONE)).json()["sa_permissions"] == [PERMISSION]


def test_revoking_unbinds_the_role_and_keeps_the_baseline(admin, gcp_project, granted):
    member = _member(gcp_project, granted)
    r = ok(admin.delete("zone_permission", group=GROUP, zone=ZONE, permission=PERMISSION)).json()
    assert r["permissions"] == [] and r["revoked"] == PERMISSION
    assert ROLE in (r["cloud"].get("revoked") or []), r["cloud"]

    poll(
        lambda: (
            ROLE not in gcp.roles_of(gcp.get("projects", "get-iam-policy", gcp_project, project=gcp_project), member)
        ),
        timeout=120,
        what=f"{ROLE} unbound from {member}",
    )
    # the identity still works: baseline bucket/secret access is not a granted permission and is never taken away
    bucket = gcp.get("storage", "buckets", "get-iam-policy", f"gs://ramen-{gcp_project}-groups", project=gcp_project)
    assert any(
        b["role"] == "roles/storage.objectViewer" and member in b.get("members", [])
        for b in (bucket or {}).get("bindings", [])
    ), bucket
    assert set(r["cloud"].get("retained") or []) <= BASELINE
    assert ok(admin.get("workers", group=GROUP, zone=ZONE)).json()["sa_permissions"] == []


def test_the_request_record_and_the_audit_follow_the_grant(admin, granted):
    assert [q["status"] for q in items(ok(admin.get("requests"))) if q["id"] == granted["request"]] == ["revoked"]
    assert any(
        e.get("action") == "permission.revoke" and f"permission:{PERMISSION}" in (e.get("tags") or [])
        for e in items(ok(admin.audit()))
    )


def test_revoking_twice_is_a_404(admin, granted):
    assert admin.delete("zone_permission", group=GROUP, zone=ZONE, permission=PERMISSION).status_code == 404
