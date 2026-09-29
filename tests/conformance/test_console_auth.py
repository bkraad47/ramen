"""CONTRACTS §9 against a running console: auth config toggle, CSRF, password reset via the file:// mail backend,
SA permission requests → approve, tool blocking on deploy config. Needs RAMEN_CONSOLE_URL.
The reset/magic-link cases also need RAMEN_MAIL_DIR = the host path of the console's `RAMEN_SMTP_HOST=file://` dir
(compose: deploy/local/.mail); otherwise they skip."""

import email
import email.policy
import re
import time
from pathlib import Path

import pytest

from ramen_tests import env as E
from ramen_tests.console import CSRF_COOKIE, CSRF_HEADER, Console, items
from ramen_tests.env import DEMO_REPO

pytestmark = pytest.mark.conformance
PW = "Passw0rd!-for-tests"


def ok(r, *codes):
    assert r.status_code in (codes or (200, 201)), (
        f"{r.request.method} {r.request.url} -> {r.status_code}: {r.text[:400]}"
    )
    return r


@pytest.fixture(scope="module")
def world(admin: Console, console_url, suffix):
    zone, g = f"az{suffix}", f"ag{suffix}"
    ok(admin.create_zone(zone), 201, 409)
    ok(admin.create_group(g, DEMO_REPO), 201)
    ok(admin.create_environment(g, "dev", [zone]), 201)
    gadmin = f"agadmin-{suffix}@ramen.test"
    uid = ok(admin.create_user(gadmin, PW, "group_admin", [g]), 201).json()["id"]
    c = Console(console_url)
    ok(c.login(gadmin, PW), 303, 200)

    def relogin():
        """V1.4: a change to `config/auth` bumps every user's session epoch, so any client that logged in before it
        is dead — including this one. The acting super admin is re-issued a session in the same response; nobody
        else is."""
        ok(c.login(gadmin, PW), 303, 200)

    yield {"zone": zone, "group": g, "gadmin": gadmin, "gadmin_c": c, "uid": uid, "relogin": relogin}
    c.close()
    admin.delete("user", id=uid)
    admin.delete("group", group=g)
    admin.delete("zone", zone=zone)


def test_csrf_required_for_cookie_sessions_not_api_keys(console_url, admin_creds, admin):
    with Console(console_url) as c:
        ok(c.login(*admin_creds), 303, 200)
        assert c.http.cookies.get(CSRF_COOKIE), "login must set the ramen_csrf cookie"
        r = c.http.post(c.url("zones"), json={"name": "csrf-probe"}, headers={CSRF_HEADER: "wrong"})
        assert r.status_code == 403 and "csrf" in r.text.lower()
        page = c.page("/login").text
        assert 'name="csrf_token"' in page
    key = ok(admin.create_api_key("csrf-exempt"), 201).json()["key"]
    with Console(console_url, api_key=key) as k:
        assert k.me().status_code == 200
        assert k.http.post(k.url("zones"), json={"name": ""}).status_code in (422, 409, 201, 400)  # reached the route


def test_auth_config_toggle_is_super_admin_only_and_audited(admin, world):
    cfg = ok(admin.get("config_auth")).json()
    assert {"password_login", "magic_link", "break_glass", "providers", "mail"} <= set(cfg)
    assert world["gadmin_c"].get("config_auth").status_code == 403
    assert world["gadmin_c"].put("config_auth", {"magic_link": True}).status_code == 403
    r = admin.put("config_auth", {"magic_link": cfg["magic_link"]})
    ok(r)
    assert any(a["action"] == "config.auth" for a in items(admin.audit()))
    if not cfg["providers"] and not cfg["break_glass"] and not cfg["magic_link"]:
        r = admin.put("config_auth", {"password_login": False})
        assert r.status_code == 422, "disabling password login with no other way in must be refused"
    # V1.4: an accepted change to config/auth bumps every user's session epoch. The acting super admin is re-issued a
    # session in the same response; every other client in this module has to log in again, or the tests after this
    # one see 401 on calls that have nothing to do with authentication.
    assert admin.me().status_code == 200, "the acting super admin was not re-issued a session"
    world["relogin"]()
    assert world["gadmin_c"].me().status_code == 200


def test_permission_request_approve_and_rules(admin, world):
    g, z, c = world["group"], world["zone"], world["gadmin_c"]
    cat = {p["permission"] for p in ok(admin.get("policy_permissions")).json()}
    assert {"bucket.read", "secrets.read"} <= cat
    assert c.post("requests", {"group": g, "zone": z, "permission": "not.a.permission"}).status_code == 422
    rules = ok(admin.get("sa_rules")).json()["rules"]
    ok(admin.put("sa_rules", {"rules": rules + [{"effect": "deny", "permission": "kms.*"}]}))
    try:
        r = c.post("requests", {"group": g, "zone": z, "permission": "kms.decrypt"})
        assert r.status_code == 409, r.text
        assert any(
            a["action"] == "permission.request" and not a["ok"] and "permission:kms.decrypt" in a.get("tags", [])
            for a in items(admin.audit())
        )
        r = ok(c.post("requests", {"group": g, "zone": z, "permission": "bucket.read"}), 201).json()
        assert r["status"] == "pending" and r["type"] == "permission"
        assert c.get("requests").status_code == 403
        assert any(q["id"] == r["id"] for q in ok(admin.get("requests")).json())
        a = ok(admin.post("request_approve", id=r["id"])).json()
        assert a["status"] == "approved" and a["applied"]["permissions"] == ["bucket.read"]
        w = ok(admin.get("workers", group=g, zone=z)).json()
        assert w["sa_permissions"] == ["bucket.read"]
        assert admin.post("request_approve", id=r["id"]).status_code == 409
    finally:
        ok(admin.put("sa_rules", {"rules": rules}))


def test_blocked_list_reaches_deploy_config(admin, world):
    g, c = world["group"], world["gadmin_c"]
    r = ok(
        c.put("env_blocked", {"blocked": ["demo_calculator_tool", "demo_calculator_tool"]}, group=g, env="dev")
    ).json()
    assert r["blocked"] == ["demo_calculator_tool"]
    assert c.put("env_blocked", {"blocked": ["a,b"]}, group=g, env="dev").status_code == 422
    envs = [e for e in ok(admin.get("environments_all", params={"group": g})).json() if e["name"] == "dev"]
    assert envs[0]["blocked"] == ["demo_calculator_tool"]
    page = c.page(f"/groups/{g}").text
    assert "demo_calculator_tool" in page and "/environments/dev/blocked" in page
    ok(c.put("env_blocked", {"blocked": []}, group=g, env="dev"))
    assert any(a["action"] == "environment.blocked" for a in items(admin.audit()))


@pytest.fixture
def maildir():
    d = E.env("RAMEN_MAIL_DIR")
    if not d or not Path(d).is_dir():
        pytest.skip("RAMEN_MAIL_DIR (host path of the console's file:// mail dir) not set")
    return Path(d)


def newest_link(maildir: Path, kind: str, since: float) -> str:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        for p in sorted(maildir.glob("*.eml"), reverse=True):
            if p.stat().st_mtime >= since:
                body = email.message_from_bytes(
                    p.read_bytes(), policy=email.policy.default
                ).get_content()  # decodes QP wrapping
                m = re.search(rf"https?://\S+?/auth/{kind}/(\S+)", body)
                if m:
                    return m.group(1)
        time.sleep(1)
    raise AssertionError(f"no {kind} mail in {maildir}")


def test_password_reset_via_file_mail_backend(console_url, admin, world, maildir):
    email = world["gadmin"]
    since = time.time() - 1
    with Console(console_url) as anon:
        assert anon.page("/auth/reset").status_code == 200
        ok(anon.form("auth_reset", {"email": "nobody-" + email}))
        ok(anon.form("auth_reset", {"email": email}))
        token = newest_link(maildir, "reset", since)
        assert anon.page(f"/auth/reset/{token}").status_code == 200
        assert anon.form("auth_reset_token", {"password": "N3w-" + PW}, token=token).status_code == 303
        assert anon.form("auth_reset_token", {"password": "again-" + PW}, token=token).status_code == 400  # single use
    with Console(console_url) as c:
        assert c.login(email, PW).status_code in (401, 403)
        ok(c.login(email, "N3w-" + PW), 303, 200)
        assert c.me().json()["email"] == email
    assert any(a["action"] == "password.reset" and a["user"] == email and a["ok"] for a in items(admin.audit()))
