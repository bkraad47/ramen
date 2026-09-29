"""`POST /api/v1/refresh` has to survive a zone namespace that is being deleted.

`refresh()` lists every namespace labelled `ramen.io/group` and then reads objects **inside** each one. Deleting a
group (F4.1) deletes its namespaces, and a namespace can sit in `Terminating` for minutes on a managed cluster.
Its RoleBinding to `ramen-console-zone` is collected early, so the console's namespaced reads answer 403 from then
on — and a reconcile that is meant to report the world must not fail whole because one namespace is on its way out.
"""

import time

import pytest

from ramen_tests import env as E

pytestmark = pytest.mark.cloud


def test_refresh_survives_a_terminating_zone_namespace(admin, suffix, demo_repo):
    group, zone = f"refg{suffix}", f"refz{suffix}"
    r = admin.create_zone(zone, provider=E.env("RAMEN_ZONE_PROVIDER", "gcp"), region=E.env("RAMEN_ZONE_REGION", ""))
    assert r.status_code in (201, 409), r.text[:300]
    r = admin.create_group(group, demo_repo)
    assert r.status_code in (201, 409), r.text[:300]
    r = admin.create_environment(group, "dev", [zone])  # skips with RAMEN_NO_CLOUD=iam (a new group needs an identity)
    assert r.status_code in (200, 201), r.text[:300]
    try:
        assert admin.delete("group", group=group).status_code == 200
        deadline = time.monotonic() + 240
        seen = []
        while time.monotonic() < deadline:
            r = admin.post("refresh", {})
            seen.append(r.status_code)
            assert r.status_code == 200, (
                f"refresh answered {r.status_code} while a zone namespace was terminating: {r.text[:300]}\n"
                "DEFECT refresh-403: GcpCloud.refresh() reads Deployments/ServiceAccounts inside every namespace it "
                "lists, and a namespace whose RoleBinding has already been collected answers 403. One namespace on "
                "its way out must not fail the whole reconcile."
            )
            if len(seen) >= 6:
                break
            time.sleep(20)
    finally:
        admin.delete("zone", zone=zone)
