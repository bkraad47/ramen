"""CONTRACTS §13.1 (V5.1 / F7.2): restore plans, prunes, keeps stripped fields, kills sessions, reconciles zones."""

import json

import pytest
from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures


def backup(root) -> str:
    r = root.post("/api/v1/backups", json={"target": "local"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def test_dry_run_reports_the_plan_and_writes_nothing(demo):
    bid = backup(demo)
    assert demo.post("/api/v1/groups", json={"name": "later"}).status_code == 201
    assert demo.delete("/api/v1/groups/other").status_code == 200

    r = demo.post(f"/api/v1/backups/{bid}/restore", json={"dry_run": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["dry_run"] is True
    assert body["restored"]["groups"]["created"] == 1  # `other`, deleted since the backup
    assert "later" in body["extra"]["groups"]  # created since the backup
    assert body["pruned"] == {}
    assert demo.get("/api/v1/groups/other").status_code == 404  # nothing was written
    assert demo.get("/api/v1/groups/later").status_code == 200


def test_restore_recreates_and_prune_removes_what_the_backup_does_not_have(demo):
    bid = backup(demo)
    assert demo.delete("/api/v1/groups/other").status_code == 200
    assert demo.post("/api/v1/groups", json={"name": "later"}).status_code == 201

    r = demo.post(f"/api/v1/backups/{bid}/restore", json={})
    assert r.status_code == 200, r.text
    assert demo.get("/api/v1/groups/other").status_code == 200
    assert demo.get("/api/v1/groups/later").status_code == 200  # a plain restore deletes nothing

    r = demo.post(f"/api/v1/backups/{bid}/restore", json={"prune": True})
    assert r.status_code == 200, r.text
    assert r.json()["pruned"]["groups"] == ["later"]
    assert demo.get("/api/v1/groups/later").status_code == 404
    assert demo.get("/api/v1/groups/other").status_code == 200


def test_prune_never_removes_the_acting_super_admin(demo):
    """A backup taken before this super admin existed must not prune the account running the restore."""
    bid = backup(demo)
    make_user(demo, "su2@x", "super_admin", [])
    with TestClient(demo.app) as su2:
        login(su2, "su2@x", PW)
        r = su2.post(f"/api/v1/backups/{bid}/restore", json={"prune": True})
        assert r.status_code == 200, r.text
        assert any("su2@x" in w for w in r.json()["warnings"])
        assert r.json()["pruned"].get("users") is None
        assert su2.get("/api/v1/me").status_code == 200  # still signed in, still a user


def test_restore_keeps_the_password_hash_the_backup_strips(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    bid = backup(demo)
    file = demo.get(f"/api/v1/backups/{bid}/download").json()
    assert all("password_hash" not in u for u in file["users"])  # the backup never carries hashes

    assert demo.post(f"/api/v1/backups/{bid}/restore", json={}).status_code == 200
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)  # the hash survived the merge, so the password still works
        assert ga.get("/api/v1/me").json()["role"] == "group_admin"


def test_a_user_the_store_lost_comes_back_login_disabled_and_warned(demo):
    u = make_user(demo, "gone@x", "viewer", ["demo"])
    bid = backup(demo)
    assert demo.delete(f"/api/v1/users/{u['id']}").status_code == 200

    r = demo.post(f"/api/v1/backups/{bid}/restore", json={})
    assert r.status_code == 200, r.text
    assert any("gone@x" in w for w in r.json()["warnings"])
    back = [x for x in demo.get("/api/v1/users").json() if x["email"] == "gone@x"]
    assert back and back[0]["login_disabled"] is True
    with TestClient(demo.app) as ga:
        r = ga.post("/login", data={"email": "gone@x", "password": PW}, follow_redirects=False)
        assert r.status_code == 401


def test_restore_ends_sessions_minted_before_it(demo):
    """A role the restore lowers must not be outlived by an open session (§13.1, U25)."""
    u = make_user(demo, "ga@x", "group_admin", ["demo"])
    bid = backup(demo)
    assert demo.put(f"/api/v1/users/{u['id']}", json={"role": "super_admin"}).status_code == 200
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get("/api/v1/me").json()["role"] == "super_admin"
        assert demo.post(f"/api/v1/backups/{bid}/restore", json={}).status_code == 200
        assert ga.get("/api/v1/me").status_code == 401  # epoch bumped by the restore


def test_a_backup_from_a_newer_release_is_refused_unless_forced(demo, tmp_path):
    bid = backup(demo)
    path = _path(demo, bid)
    data = json.loads(open(path).read())
    data["release_version"] = "99.0.0"
    open(path, "w").write(json.dumps(data))

    r = demo.post(f"/api/v1/backups/{bid}/restore", json={})
    assert r.status_code == 409 and "99.0.0" in r.json()["detail"]
    r = demo.post(f"/api/v1/backups/{bid}/restore", json={"force": True})
    assert r.status_code == 200 and r.json()["release_version"] == "99.0.0"
    forced = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "backup.restore"]
    assert any("force:true" in a["tags"] for a in forced)


def test_reconcile_reapplies_the_restored_zones(demo):
    assert demo.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 3}).status_code == 200
    bid = backup(demo)
    assert demo.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 1}).status_code == 200

    r = demo.post(f"/api/v1/backups/{bid}/restore", json={"reconcile": True})
    assert r.status_code == 200, r.text
    assert "demo/zone-a" in r.json()["reconciled"]
    assert demo.get("/api/v1/groups/demo/zones/zone-a/workers").json()["count"] == 3


def test_restore_needs_a_super_admin(demo):
    bid = backup(demo)
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.post(f"/api/v1/backups/{bid}/restore", json={}).status_code == 403


@pytest.mark.parametrize("body", [{"dry_run": True}, {}])
def test_restore_is_audited_with_its_flags(demo, body):
    bid = backup(demo)
    assert demo.post(f"/api/v1/backups/{bid}/restore", json=body).status_code == 200
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "backup.restore"]
    assert rows and rows[0]["ok"] is True


def _path(client, bid) -> str:
    return [b for b in client.get("/api/v1/backups").json() if b["id"] == bid][0]["path"]


async def test_reconcile_survives_a_bad_zone_and_reports_what_the_cloud_still_runs(demo, monkeypatch):
    """One unreachable zone must not abandon the rest, and namespaces the backup never knew about are reported."""
    assert demo.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 2}).status_code == 200
    assert demo.put("/api/v1/groups/demo/zones/zone-b/workers", json={"count": 2}).status_code == 200
    bid = backup(demo)
    path = _path(demo, bid)
    data = json.loads(open(path).read())
    data["zones"] = [z for z in data["zones"] if z["id"] != "zone-b"]  # a worker row for a zone nobody has any more
    open(path, "w").write(json.dumps(data))
    await demo.app.state.store.delete("zones", "zone-b")

    cloud = demo.app.state.services.cloud

    async def attach(group, zone, spec=None):
        raise RuntimeError("cluster unreachable")

    async def refresh():
        return {"zones": [{"group": "ghost", "zone": "zone-a"}, {"group": "demo", "zone": "zone-a"}]}

    monkeypatch.setattr(cloud, "attach_zone", attach)
    monkeypatch.setattr(cloud, "refresh", refresh)
    r = demo.post(f"/api/v1/backups/{bid}/restore", json={"reconcile": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reconciled"] == ["demo/zone-a: RuntimeError: cluster unreachable"]
    assert body["orphans"] == ["ghost/zone-a"]  # reported, never deleted

    async def boom():
        raise RuntimeError("no cloud")

    monkeypatch.setattr(cloud, "refresh", boom)
    r = demo.post(f"/api/v1/backups/{bid}/restore", json={"reconcile": True})
    assert r.status_code == 200 and r.json()["orphans"] == []


def test_a_backup_file_with_junk_rows_is_restored_without_them(demo):
    bid = backup(demo)
    path = _path(demo, bid)
    data = json.loads(open(path).read())
    data["groups"] += ["not-a-dict", {"name": "no-id"}]
    open(path, "w").write(json.dumps(data))
    r = demo.post(f"/api/v1/backups/{bid}/restore", json={})
    assert r.status_code == 200 and r.json()["restored"]["groups"]["created"] == 0
    assert sorted(g["id"] for g in demo.get("/api/v1/groups").json()) == ["demo", "other"]
