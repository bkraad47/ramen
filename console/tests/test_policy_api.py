"""CONTRACTS §9 SA policy engine: requests → approve → Cloud.apply_sa_permissions, rules gate, 409 audited."""

import json

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures


def test_permission_request_lifecycle(demo, tmp_path):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    cat = demo.get("/api/v1/policy/permissions").json()
    assert {"permission": "bucket.read"}.items() <= [c for c in cat if c["permission"] == "bucket.read"][0].items()
    assert (
        demo.put("/api/v1/config/sa-rules", json={"rules": [{"effect": "deny", "permission": "kms.*"}]}).status_code
        == 200
    )
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert (
            ga.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "nope"}).status_code
            == 422
        )
        assert ga.post("/api/v1/requests", json={"group": "demo", "permission": "bucket.read"}).status_code == 422
        assert ga.post("/api/v1/requests", json={}).status_code == 422
        assert (
            ga.post(
                "/api/v1/requests", json={"group": "other", "zone": "zone-a", "permission": "bucket.read"}
            ).status_code
            == 403
        )
        assert (
            ga.post(
                "/api/v1/requests", json={"group": "demo", "zone": "nozone", "permission": "bucket.read"}
            ).status_code
            == 404
        )
        r = ga.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "kms.decrypt"})
        assert r.status_code == 409 and "super-admin rule denies" in r.json()["detail"]
        ga.put("/api/v1/groups/demo/sa-restrictions", json={"rules": [{"effect": "deny", "permission": "pubsub.*"}]})
        r = ga.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "pubsub.publish"})
        assert r.status_code == 409 and "group rule denies" in r.json()["detail"]
        r = ga.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "bucket.read"})
        assert r.status_code == 201 and r.json()["type"] == "permission" and r.json()["status"] == "pending"
        rid = r.json()["id"]
        r2 = ga.post("/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "secrets.read"})
        assert r2.status_code == 201
        assert ga.get("/groups/demo").status_code == 200 and "Request permission" in ga.get("/groups/demo").text
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert (
            v.post(
                "/api/v1/requests", json={"group": "demo", "zone": "zone-a", "permission": "bucket.read"}
            ).status_code
            == 403
        )
        assert v.post("/api/v1/requests", json={"role": "group_admin", "group": "demo"}).status_code == 201
    audit = demo.get("/api/v1/audit").json()
    denied = [
        a
        for a in audit
        if a["action"] == "permission.request" and not a["ok"] and "permission:kms.decrypt" in a["tags"]
    ]
    assert denied and denied[0]["user"] == "ga@x" and "group:demo" in denied[0]["tags"]
    pending = [q for q in demo.get("/api/v1/requests").json() if q["status"] == "pending"]
    assert {q.get("permission") for q in pending} == {"bucket.read", "secrets.read", None}
    r = demo.post(f"/api/v1/requests/{rid}/approve")
    assert (
        r.status_code == 200
        and r.json()["status"] == "approved"
        and r.json()["applied"]["permissions"] == ["bucket.read"]
    )
    assert "recorded" in r.json()["applied"]
    recorded = json.loads(open(r.json()["applied"]["recorded"]).read())
    assert recorded == ["bucket.read"]
    assert demo.post(f"/api/v1/requests/{r2.json()['id']}/approve").json()["applied"]["permissions"] == [
        "bucket.read",
        "secrets.read",
    ]
    w = demo.get("/api/v1/groups/demo/zones/zone-a/workers").json()
    assert w["sa_permissions"] == ["bucket.read", "secrets.read"]
    assert demo.get("/api/v1/groups/demo/zones/zone-b/workers").json().get("sa_permissions") is None
    page = demo.get("/groups/demo").text
    # §13.2: each granted permission is its own badge now, so a super admin can revoke them one at a time
    assert "bucket.read" in page and "secrets.read" in page and "approved" in page
    assert page.count("Revoke permission") == 2
    users_page = demo.get("/users").text
    assert "Service-account permission" in users_page and "bucket.read" in users_page
    assert demo.post(f"/api/v1/requests/{rid}/approve").status_code == 409
    assert demo.get("/config").status_code == 200 and "kms.decrypt" in demo.get("/config").text
