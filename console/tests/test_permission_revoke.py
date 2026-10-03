"""CONTRACTS §13.2 (V5.2 / F4.2): the missing half of the approval flow — deny, revoke, and IAM that shrinks."""

import json

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures


def grant(root, permission, zone="zone-a") -> str:
    r = root.post("/api/v1/requests", json={"group": "demo", "zone": zone, "permission": permission})
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert root.post(f"/api/v1/requests/{rid}/approve").status_code == 200
    return rid


def test_revoking_a_permission_shrinks_what_the_cloud_holds(demo):
    grant(demo, "bucket.read")
    rid = grant(demo, "secrets.read")
    applied = demo.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "logs.write"})
    assert applied.status_code == 201
    assert demo.post(f"/api/v1/requests/{applied.json()['id']}/approve").status_code == 200
    w = demo.get("/api/v1/groups/demo/zones/zone-a/workers").json()
    assert w["sa_permissions"] == ["bucket.read", "secrets.read", "logs.write"]

    r = demo.delete("/api/v1/groups/demo/zones/zone-a/permissions/secrets.read")
    assert r.status_code == 200, r.text
    assert r.json()["permissions"] == ["bucket.read", "logs.write"]
    w = demo.get("/api/v1/groups/demo/zones/zone-a/workers").json()
    assert w["sa_permissions"] == ["bucket.read", "logs.write"]
    recorded = json.loads(open(r.json()["cloud"]["recorded"]).read())
    assert recorded == ["bucket.read", "logs.write"]  # the adapter was called with the remaining set, not the removal
    assert [q for q in demo.get("/api/v1/requests").json() if q["id"] == rid][0]["status"] == "revoked"


def test_revoking_something_that_was_never_granted_is_a_404(demo):
    grant(demo, "bucket.read")
    assert demo.delete("/api/v1/groups/demo/zones/zone-a/permissions/kms.decrypt").status_code == 404
    assert demo.delete("/api/v1/groups/demo/zones/zone-b/permissions/bucket.read").status_code == 404
    assert demo.delete("/api/v1/groups/demo/zones/nozone/permissions/bucket.read").status_code == 404


def test_revoke_through_the_request_record(demo):
    rid = grant(demo, "bucket.read")
    r = demo.post(f"/api/v1/requests/{rid}/revoke")
    assert r.status_code == 200 and r.json()["status"] == "revoked"
    assert demo.get("/api/v1/groups/demo/zones/zone-a/workers").json()["sa_permissions"] == []
    assert demo.post(f"/api/v1/requests/{rid}/revoke").status_code == 409  # not approved any more


def test_denying_a_pending_request_applies_nothing(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        rid = ga.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "bucket.read"}).json()[
            "id"
        ]
    r = demo.post(f"/api/v1/requests/{rid}/deny")
    assert r.status_code == 200 and r.json()["status"] == "denied"
    assert demo.get("/api/v1/groups/demo/zones/zone-a/workers").json().get("sa_permissions") is None
    assert demo.post(f"/api/v1/requests/{rid}/deny").status_code == 409
    assert demo.post(f"/api/v1/requests/{rid}/approve").status_code == 409  # denied is final


def test_revoking_a_role_grant_lowers_the_user_and_ends_their_session(demo):
    u = make_user(demo, "v@x", "viewer", [])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        rid = v.post("/api/v1/requests", json={"role": "group_admin", "group": "demo"}).json()["id"]
        assert demo.post(f"/api/v1/requests/{rid}/approve").status_code == 200
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        me = v.get("/api/v1/me").json()
        assert me["role"] == "group_admin" and me["groups"] == ["demo"]

        r = demo.post(f"/api/v1/requests/{rid}/revoke")
        assert r.status_code == 200, r.text
        assert v.get("/api/v1/me").status_code == 401  # revocation takes effect immediately (U25)
    after = [x for x in demo.get("/api/v1/users").json() if x["id"] == u["id"]][0]
    assert after["role"] == "viewer" and after["groups"] == []


def test_group_admins_of_the_group_may_revoke_but_not_strip_a_binding_directly(demo):
    """0.5.95 (R4): an admin of the request's group (not the requester) may deny or revoke it; taking a bound
    permission off the zone directly stays a super-admin call."""
    rid = grant(demo, "bucket.read")
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "o@x", "group_admin", ["other"])
    with TestClient(demo.app) as o:
        login(o, "o@x", PW)
        assert o.post(f"/api/v1/requests/{rid}/revoke").status_code == 403
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.delete("/api/v1/groups/demo/zones/zone-a/permissions/bucket.read").status_code == 403
        assert ga.post(f"/api/v1/requests/{rid}/deny").status_code == 409  # approved, not pending
        assert ga.post(f"/api/v1/requests/{rid}/revoke").status_code == 200


def test_revocation_is_audited_and_visible_on_the_group_page(demo):
    grant(demo, "bucket.read")
    page = demo.get("/groups/demo").text
    assert "Revoke permission" in page
    assert demo.delete("/api/v1/groups/demo/zones/zone-a/permissions/bucket.read").status_code == 200
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "permission.revoke"]
    assert rows and rows[0]["ok"] is True and "permission:bucket.read" in rows[0]["tags"]
