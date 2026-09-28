"""Per group+zone service account (CONTRACTS §7): POST → {name}; on GCP the GSA exists with objectViewer +
secretAccessor and the Workload Identity binding to KSA ramen-<group>-<zone>/worker. Needs RAMEN_CONSOLE_URL."""

import pytest

from ramen_tests import gcp
from ramen_tests.console import items

from .conftest import GROUP, ZONE, ok, poll

pytestmark = pytest.mark.cloud
EXPECTED_ROLES = {"roles/storage.objectViewer", "roles/secretmanager.secretAccessor"}


@pytest.fixture(scope="module")
def sa(admin, world):
    return ok(admin.post("service_account", group=GROUP, zone=ZONE)).json()


def test_service_account_created_and_recorded(admin, sa):
    assert sa.get("name"), sa
    cfg = ok(admin.get("workers", group=GROUP, zone=ZONE)).json()
    assert cfg.get("service_account") == sa["name"]


def test_service_account_is_audited(admin, sa):
    entries = items(ok(admin.audit()))
    assert any(e.get("action") == "service_account.create" and e.get("target") == f"{GROUP}/{ZONE}" for e in entries)


def test_gsa_exists_with_expected_roles(sa, gcp_project):
    email = sa["name"] if "@" in str(sa.get("name")) else sa.get("email") or gcp.gsa(gcp_project, GROUP, ZONE)
    desc = poll(
        lambda: gcp.get("iam", "service-accounts", "describe", email, project=gcp_project),
        timeout=60,
        what=f"GSA {email}",
    )
    assert desc["email"] == email
    member = f"serviceAccount:{email}"
    # SEC-08 (0.3.1, CONTRACTS §11): resource-level bindings only — objectViewer on the groups bucket (conditioned to
    # the group prefix), secretAccessor on each ramen-<group>-* secret; the project policy never mentions worker GSAs.
    bucket = gcp.get("storage", "buckets", "get-iam-policy", f"gs://ramen-{gcp_project}-groups", project=gcp_project)
    viewer = [
        b
        for b in (bucket or {}).get("bindings", [])
        if b["role"] == "roles/storage.objectViewer" and member in b.get("members", [])
    ]
    assert viewer and f"/objects/{GROUP}/" in viewer[0].get("condition", {}).get("expression", ""), bucket
    secrets = gcp.get("secrets", "list", f"--filter=name:ramen-{GROUP}-", project=gcp_project) or []
    for sec in secrets:
        pol = gcp.get("secrets", "get-iam-policy", sec["name"].rsplit("/", 1)[1], project=gcp_project)
        assert "roles/secretmanager.secretAccessor" in gcp.roles_of(pol, member), (sec["name"], pol)
    project_roles = gcp.roles_of(gcp.get("projects", "get-iam-policy", gcp_project, project=gcp_project), member)
    assert project_roles <= {"roles/logging.logWriter", "roles/monitoring.metricWriter"}, project_roles


def test_workload_identity_binding(gcp_project, sa):
    email = sa["name"] if "@" in str(sa.get("name")) else sa.get("email") or gcp.gsa(gcp_project, GROUP, ZONE)
    pol = gcp.get("iam", "service-accounts", "get-iam-policy", email, project=gcp_project)
    member = f"serviceAccount:{gcp_project}.svc.id.goog[{gcp.namespace(GROUP, ZONE)}/worker]"
    assert "roles/iam.workloadIdentityUser" in gcp.roles_of(pol, member), pol
