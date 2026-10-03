"""0.5.92: SMTP and warning-email settings live on the Config page (super admin), and no form needs `eval` —
the CSP forbids it, so every `hx-vals='js:…'` form silently never sent its request in a real browser."""

import re
from pathlib import Path

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

TEMPLATES = Path(__file__).resolve().parents[1].joinpath("src/ramen_console/templates")


def test_no_template_needs_eval():
    """`js:` values, trigger filters (`every 60s[flag]`) and hx-on all compile with Function(), which the CSP blocks."""
    needs_eval = re.compile(r"hx-vals=['\"]js:|hx-trigger=['\"][^'\"]*\[|hx-on[:-]")
    bad = [f.name for f in TEMPLATES.rglob("*.html") if needs_eval.search(f.read_text())]
    assert bad == [], bad


def test_mail_settings_moved_to_config_for_super_admins_only(demo):
    users, config = demo.get("/users").text, demo.get("/config").text
    for title in ("Server email (SMTP)", "Server warning/error emails"):
        assert title in config and title not in users, title
    assert 'type="hidden" name="user_ids" value=""' in config  # an emptied list is still sent
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get("/config", follow_redirects=False).status_code == 403
        assert ga.put("/api/v1/config/notify", json={"user_ids": []}).status_code == 403
        assert ga.put("/api/v1/config/smtp", json={"host": "x"}).status_code == 403


def test_notify_list_accepts_what_the_chip_form_sends(demo):
    uid = make_user(demo, "n@x", "viewer", ["demo"])["id"]
    assert demo.put("/api/v1/config/notify", json={"user_ids": ["", uid]}).json()["user_ids"] == [uid]
    page = demo.get("/config").text
    assert f'<input type="hidden" name="user_ids" value="{uid}">' in page  # the chip carries its id as a field
    assert demo.put("/api/v1/config/notify", json={"user_ids": ""}).json()["user_ids"] == []


def test_rules_forms_post_plain_fields(demo):
    """Config: the current rules travel as a JSON string plus the new rule's fields; group: a JSON textarea."""
    r = demo.put("/api/v1/config/sa-rules", json={"rules": "[]", "effect": "deny", "permission": "*", "glob": "kms.*"})
    assert r.status_code == 200 and r.json()["rules"] == [{"effect": "deny", "permission": "kms.*"}]
    current = r.json()["rules"]
    r = demo.put("/api/v1/config/sa-rules", json={"rules": current, "effect": "allow", "permission": "bucket.read"})
    assert r.json()["rules"] == [
        {"effect": "deny", "permission": "kms.*"},
        {"effect": "allow", "permission": "bucket.read"},
    ]
    assert demo.put("/api/v1/config/sa-rules", json={"rules": "not json"}).status_code == 422
    page = demo.get("/config").text
    assert re.search(r'<input type="hidden" name="rules" value=\'\[\{', page) or 'name="rules" value="[' in page
    r = demo.put(
        "/api/v1/groups/demo/sa-restrictions", json={"rules": '[{"effect": "deny", "permission": "pubsub.*"}]'}
    )
    assert r.status_code == 200
    assert 'name="rules"' in demo.get("/groups/demo").text
