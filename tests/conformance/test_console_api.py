"""CONTRACTS §4: console API — bootstrap login, RBAC matrix, secret values never leak, API keys, audit, backup.
Needs RAMEN_CONSOLE_URL (+ RAMEN_ADMIN_EMAIL/RAMEN_ADMIN_PASSWORD, default admin@ramen.local / ramen-admin)."""

import json

import pytest

from ramen_tests.console import Console, items
from ramen_tests.env import DEMO_REPO

pytestmark = pytest.mark.conformance
PW = "Passw0rd!-for-tests"
SECRET_VALUE = "s3cr3t-value-must-never-appear-9b1d"


def ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), (
        f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    )
    return r


def denied(r):
    assert r.status_code == 403, f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    return r


@pytest.fixture(scope="module")
def world(admin: Console, console_url, suffix):
    """One zone, two groups (A, B) with env `dev`, a viewer and a group admin on A. Cleaned up best-effort."""
    zone, ga, gb = f"tz{suffix}", f"tga{suffix}", f"tgb{suffix}"
    ok(admin.create_zone(zone), 201, 409)
    for g in (ga, gb):
        ok(admin.create_group(g, DEMO_REPO), 201)
        ok(admin.create_environment(g, "dev", [zone]), 201)
    viewer, gadmin = f"viewer-{suffix}@ramen.test", f"gadmin-{suffix}@ramen.test"
    uids = [
        ok(admin.create_user(viewer, PW, "viewer", [ga]), 201).json()["id"],
        ok(admin.create_user(gadmin, PW, "group_admin", [ga]), 201).json()["id"],
    ]
    w = {"zone": zone, "ga": ga, "gb": gb, "viewer": viewer, "gadmin": gadmin, "uids": uids}
    w["viewer_c"] = Console(console_url)
    ok(w["viewer_c"].login(viewer, PW), 303, 200)
    w["gadmin_c"] = Console(console_url)
    ok(w["gadmin_c"].login(gadmin, PW), 303, 200)
    yield w
    for c in (w["viewer_c"], w["gadmin_c"]):
        c.close()
    for u in [x["id"] for x in items(admin.get("users")) if x.get("email", "").endswith(f"-{suffix}@ramen.test")]:
        admin.delete("user", id=u)
    for g in (ga, gb):
        admin.delete("group", group=g)
    admin.delete("zone", zone=zone)


def test_bootstrap_login_and_me(admin, admin_creds):
    me = ok(admin.me()).json()
    assert me["email"] == admin_creds[0]
    assert me["role"] == "super_admin"
    assert "password" not in json.dumps(me).lower()


def test_bad_password_rejected(console_url, admin_creds):
    with Console(console_url) as c:
        assert c.login(admin_creds[0], "definitely-wrong").status_code in (401, 403)
        assert c.me().status_code == 401


def test_unauthenticated_api_rejected(console_url):
    with Console(console_url) as c:
        assert c.get("groups").status_code == 401
        c.http.cookies.set("ramen_session", "tampered")
        assert c.me().status_code == 401


def test_viewer_reads_but_cannot_mutate(world):
    v, ga, gb, zone = world["viewer_c"], world["ga"], world["gb"], world["zone"]
    assert ok(v.me()).json()["role"] == "viewer"
    assert [g["name"] for g in items(ok(v.get("groups")))] == [ga]
    ok(v.get("group", group=ga))
    denied(v.get("secrets", group=ga))  # 0.5.95 (N18): secrets are for super and group admins only
    denied(v.get("group", group=gb))
    denied(v.put("group", {"ref": "main"}, group=ga))
    denied(v.post("rebalance", group=ga, zone=zone))
    denied(v.deploy(ga, "dev"))
    denied(v.create_user(f"x-{ga}@ramen.test", PW, "viewer", [ga]))
    denied(v.add_secret(ga, "VIEWER_SECRET", "nope"))
    denied(v.delete("user", id=world["uids"][1]))
    denied(v.create_mcp_key(ga))
    denied(v.create_api_key("viewer-key"))


def test_group_admin_scoped_to_own_group(world, suffix):
    g, ga, gb, zone = world["gadmin_c"], world["ga"], world["gb"], world["zone"]
    sid = ok(g.add_secret(ga, "GA_TOKEN", SECRET_VALUE, env="dev", zone=zone), 201).json()["id"]
    assert ok(g.post("rebalance", group=ga, zone=zone)).json()["ok"] is True
    ok(g.put("group", {"ref": "main"}, group=ga))
    ok(g.create_user(f"v2-{suffix}@ramen.test", PW, "viewer", [ga]), 201)
    ok(g.create_mcp_key(ga, "ga-key"), 201)
    ok(g.delete("secret", group=ga, id=sid))
    denied(g.get("group", group=gb))
    denied(g.add_secret(gb, "GB_TOKEN", "x"))
    denied(g.get("secrets", group=gb))
    denied(g.post("rebalance", group=gb, zone=zone))
    denied(g.deploy(gb, "dev"))
    denied(g.create_user(f"v3-{suffix}@ramen.test", PW, "viewer", [gb]))
    denied(g.create_user(f"a2-{suffix}@ramen.test", PW, "group_admin", [ga]))
    denied(g.create_user(f"sa-{suffix}@ramen.test", PW, "super_admin", []))
    denied(g.create_group(f"{ga}x", DEMO_REPO))
    denied(g.delete("group", group=ga))
    denied(g.create_zone(f"{zone}x"))
    denied(g.post("refresh"))
    denied(g.create_api_key("esc", role="super_admin"))
    denied(g.create_api_key("esc", groups=[gb]))


def test_super_admin_touches_everything(admin, world):
    ga, gb, zone = world["ga"], world["gb"], world["zone"]
    for g in (ga, gb):
        sid = ok(admin.add_secret(g, "SA_TOKEN", SECRET_VALUE, env="dev", zone=zone), 201).json()["id"]
        assert ok(admin.post("rebalance", group=g, zone=zone)).json()["ok"] is True
        ok(admin.get("workers", group=g, zone=zone))
        ok(admin.delete("secret", group=g, id=sid))
    ok(admin.get("users"))
    ok(admin.get("zones"))
    ok(admin.get("config"))
    assert "groups" in ok(admin.post("refresh")).json()


def test_secret_values_never_in_any_response(admin, world):
    ga, zone = world["ga"], world["zone"]
    r = ok(admin.add_secret(ga, "LEAK_CHECK", SECRET_VALUE, env="dev", zone=zone), 201)
    assert SECRET_VALUE not in r.text
    sid = r.json()["id"]
    b = ok(admin.post("backups", {"target": "local"}), 201).json()
    responses = [
        admin.get("secrets", group=ga),
        admin.get("secrets", group=ga, params={"format": "csv"}),
        admin.get("group", group=ga),
        admin.get("environments_all", params={"group": ga}),
        admin.audit(),
        admin.get("config"),
        admin.get("dashboard"),
        admin.get("backups"),
        admin.get("backup_download", id=b["id"]),
        admin.page("/secrets"),
        admin.page(f"/secrets?group={ga}"),
        admin.page(f"/groups/{ga}"),
        admin.page("/audit"),
    ]
    for r in responses:
        assert SECRET_VALUE not in r.text, f"secret leaked by {r.request.method} {r.request.url}"
    listing = items(responses[0])
    mine = next(s for s in listing if s["name"] == "LEAK_CHECK")
    assert "value" not in mine
    ok(admin.delete("secret", group=ga, id=sid))
    assert "LEAK_CHECK" not in [s["name"] for s in items(admin.get("secrets", group=ga))]


def test_config_masks_sensitive_env(admin, admin_creds):
    r = ok(admin.get("config"))
    assert admin_creds[1] not in r.text
    assert admin_creds[1] not in admin.page("/config").text


def test_api_key_scoped_calls(admin, console_url, world, suffix):
    ga, gb = world["ga"], world["gb"]
    created = ok(admin.create_api_key(f"tests-{suffix}", groups=[ga], role="group_admin"), 201).json()
    key, kid = created["key"], created["id"]
    assert key.startswith("rmn_") and key.count("_") >= 2
    listing = ok(admin.get("api_keys")).text
    assert key not in listing and "secret_hash" not in listing, "full API key must only be returned once, at creation"
    with Console(console_url, api_key=key) as c:
        assert ok(c.me()).json().get("kind") == "apikey"
        ok(c.get("group", group=ga))
        ok(c.get("secrets", group=ga))
        denied(c.get("group", group=gb))
        denied(c.create_group(f"{ga}k", DEMO_REPO))
    for bad in ("rmn_bogus_bogus", f"rmn_{kid}_wrongsecret", "garbage"):
        with Console(console_url, api_key=bad) as c:
            assert c.me().status_code == 401
    ok(admin.delete("api_key", id=kid))
    with Console(console_url, api_key=key) as c:
        assert c.me().status_code == 401


def test_audit_entries_created(admin, world, admin_creds):
    ga = world["ga"]
    sid = ok(admin.add_secret(ga, "AUDITED", "v"), 201).json()["id"]
    admin.delete("secret", group=ga, id=sid)
    entries = items(ok(admin.audit()))
    mine = [e for e in entries if e.get("user") == admin_creds[0]]
    assert mine, entries[:3]
    for k in ("ts", "user", "ip", "action", "target", "ok", "tags"):
        assert k in mine[0], mine[0]
    actions = {e["action"] for e in mine}
    assert "login" in actions
    assert any("secret" in a for a in actions), actions
    assert any(f"group:{ga}" in e.get("tags", []) for e in mine)


def test_audit_is_super_admin_only_and_names_the_group(admin, world):
    """0.5.95 (N19): the audit log is a super-admin page; a group admin's actions still land in it, tagged."""
    g, ga = world["gadmin_c"], world["ga"]
    ok(g.put("group", {"ref": "main"}, group=ga))
    denied(g.audit())
    rows = items(ok(admin.audit()))
    assert any(f"group:{ga}" in e.get("tags", []) and e.get("user") == world["gadmin"] for e in rows)


def test_backup_contains_release_version(admin):
    b = ok(admin.post("backups", {"target": "local"}), 201).json()
    assert b["release_version"].count(".") == 2
    doc = ok(admin.get("backup_download", id=b["id"])).json()
    assert doc["release_version"] == b["release_version"]
    for k in ("users", "groups", "environments", "zones"):
        assert k in doc, list(doc)
    assert "secrets" not in doc
    assert all("password_hash" not in u for u in doc["users"])


def test_health_endpoint(admin):
    codes = {p: admin.page(p).status_code for p in ("/healthz", "/login")}
    assert 200 in codes.values(), codes
