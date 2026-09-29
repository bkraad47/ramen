"""CONTRACTS §14 (W1–W9): the console usability round — pickers, refresh, filters, sizing, wording, icon, version."""

import re
from pathlib import Path

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

STATIC = Path(__file__).resolve().parents[1] / "src/ramen_console/static"
TEMPLATES = Path(__file__).resolve().parents[1] / "src/ramen_console/templates"


def cell_of(page: str, needle: str) -> str:
    """The <td> that holds `needle`, so ordering inside a row's actions cell can be asserted."""
    return next(td for td in re.findall(r"<td[^>]*>(.*?)</td>", page, re.S) if needle in td)


# --- W1 the group picker on the API keys page is a dropdown, not a list box ---------------------------------------
def test_api_key_groups_are_a_dropdown_that_still_takes_several_groups(demo):
    page = demo.get("/api-keys").text
    assert '<select name="groups" multiple' not in page and "multiple size=" not in page
    assert 'id="group-pick"' in page and "Add" in page
    assert 'name="groups"' in page  # the chips post as repeated fields

    r = demo.post("/api/v1/api-keys", json={"name": "two-groups", "groups": ["demo", "other"]})
    assert r.status_code == 201 and sorted(r.json()["groups"]) == ["demo", "other"]


# --- W2 dashboard refreshes every minute, and the toggle can stop it ----------------------------------------------
def test_dashboard_polls_every_minute_behind_a_toggle(demo):
    page = demo.get("/").text
    assert "every 60s" in page and "every 10s" not in page
    assert "Auto refresh" in page
    assert "ramenAuto" in page  # the trigger is filtered by the flag the button owns


# --- W3 audit: newest 100, searchable, filterable, scrollable ------------------------------------------------------
def test_audit_shows_a_hundred_rows_with_search_filter_and_a_scroll_frame(demo):
    for i in range(120):
        demo.post("/api/v1/zones", json={"name": f"z{i}", "provider": "local", "region": "local"})
    page = demo.get("/audit").text
    assert page.count("<tr") <= 102  # 100 rows + header (+ the empty-state row never rendered here)
    assert 'id="audit-search"' in page and 'id="audit-outcome"' in page
    assert "audit-scroll" in page
    assert "newest 100" in page.lower()
    assert demo.get("/audit?limit=5").text.count("<tr") <= 7
    assert demo.get("/audit?limit=99999").status_code == 422


# --- W4 backups actions are one evenly spaced row of equal buttons -------------------------------------------------
def test_backup_actions_are_the_same_size(demo):
    assert demo.post("/api/v1/backups", json={"target": "local"}).status_code == 201
    page = demo.get("/backups").text
    cell = cell_of(page, "Preview restore")
    assert cell.count("act") >= 4  # Download, Preview restore, Restore, Restore and prune all carry the width class
    assert "row-actions" in page
    for label in ("Download", "Preview restore", "Restore", "Restore and prune"):
        assert label in cell


# --- W5 service-account rules are added from controls, not hand-written JSON ---------------------------------------
def test_super_admin_adds_and_removes_service_account_rules_from_the_page(demo):
    page = demo.get("/config").text
    assert 'name="effect"' in page and "Add rule" in page
    assert 'id="rule-permission"' in page and "bucket.read" in page

    r = demo.put("/api/v1/config/sa-rules", json={"rules": [{"effect": "deny", "permission": "kms.*"}]})
    assert r.status_code == 200
    page = demo.get("/config").text
    assert "kms.*" in page and "Remove" in page
    assert cell_of(page, "kms.*")  # the rule is a table row now, not only a blob in the textarea


# --- W6 environments: last deploy flattened, actions sized ---------------------------------------------------------
def test_environments_flattens_the_last_deploy_and_sizes_its_actions(demo):
    assert demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": True}).status_code == 202
    page = demo.get("/environments").text
    assert "Last deploy" in page and "When" in page
    assert '<code>{"job"' not in page and "&#34;job&#34;" not in page  # never a JSON blob
    assert "act" in cell_of(page, "Open group")


# --- W7 users: delete to the right of save, and it says what it deletes ---------------------------------------------
def test_users_row_puts_delete_user_right_of_save(demo):
    make_user(demo, "someone@x", "viewer", ["demo"])
    cell = cell_of(demo.get("/users").text, "Delete user")
    assert "Save" in cell and cell.index("Save") < cell.index("Delete user")
    assert "row-actions" in cell
    assert ">Delete<" not in cell  # the bare label is gone


# --- W8 the favicon is the console's icon everywhere ----------------------------------------------------------------
def test_every_page_uses_the_favicon(demo):
    assert (STATIC / "favicon.png").exists()
    for path in ("/", "/groups", "/users", "/api-keys", "/audit", "/backups", "/config", "/logs"):
        assert '<link rel="icon" href="/static/favicon.png"' in demo.get(path).text, path
    demo.get("/logout")
    assert '<link rel="icon" href="/static/favicon.png"' in demo.get("/login").text
    assert all(
        'href="/static/logo.png"' not in t.read_text() or "icon" not in t.read_text().split("logo.png")[0][-40:]
        for t in TEMPLATES.glob("*.html")
    )


# --- W9 the version in the sidebar is the release, from one source -------------------------------------------------
def test_the_sidebar_shows_the_release_version_and_nothing_hard_codes_it(demo):
    from ramen_console import __version__
    from ramen_console.backup import release_version

    version = (Path(__file__).resolve().parents[2] / "VERSION").read_text().strip()
    assert __version__ == version and release_version() == version
    assert f"v{version}" in demo.get("/").text
    src = (Path(__file__).resolve().parents[1] / "src/ramen_console/__init__.py").read_text()
    assert re.search(r'__version__\s*=\s*["\']\d', src) is None  # derived, never typed in
