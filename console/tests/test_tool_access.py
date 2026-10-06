"""C10 (0.7.2, A8): per-tool list/call permissions by caller kind — environment doc, API, deploy env var, the
`role` token claim and the group page."""

import asyncio
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console import toolaccess
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures
from tests.test_oauth_server import consent, exchange, node_verify, pkce, register

URL = "/api/v1/groups/demo/environments/prod/tool-access"
ALL = list(toolaccess.KINDS)


def test_kinds_are_the_four_the_node_knows_and_super_admin_is_not_one():
    assert toolaccess.KINDS == ("key", "group_admin", "viewer", "mcp_user")


def test_clean_validates_orders_and_drops_unrestricted_entries():
    assert toolaccess.clean({}) == {}
    out = toolaccess.clean({" calc ": {"list": ["viewer", "key", "key"], "call": "key"}})
    assert out == {"calc": {"list": ["key", "viewer"], "call": ["key"]}}
    assert toolaccess.clean({"calc": {"list": ALL, "call": ALL}}) == {}  # same as no entry
    assert toolaccess.clean({"calc": {"list": ALL}}) == {"calc": {"list": ALL, "call": []}}
    assert toolaccess.clean({"calc": {}}) == {"calc": {"list": [], "call": []}}  # super admins only
    with pytest.raises(ValueError, match="unknown kind 'root'"):
        toolaccess.clean({"calc": {"list": ["root"]}})
    with pytest.raises(ValueError, match="viewer may call calc but not list it"):
        toolaccess.clean({"calc": {"list": ["key"], "call": ["key", "viewer"]}})
    with pytest.raises(ValueError, match="Tool name"):
        toolaccess.clean({"": {"list": []}})
    with pytest.raises(ValueError, match="calc"):
        toolaccess.clean({"calc": ["key"]})
    with pytest.raises(ValueError, match="map"):
        toolaccess.clean(["calc"])


def test_compact_json_is_sorted_and_spaceless():
    assert toolaccess.compact(None) == "{}"
    assert toolaccess.compact({"b": {"list": ["key"], "call": []}, "a": {"list": [], "call": []}}) == (
        '{"a":{"call":[],"list":[]},"b":{"call":[],"list":["key"]}}'
    )


def test_rows_merge_the_last_deploys_tools_with_restricted_names():
    env = {
        "last_deploy": {"packages": {"zone-a w1": {"tools": [{"name": "calc"}, {"name": "sum"}], "resources": []}}},
        "tool_access": {"zzz": {"list": [], "call": []}},
    }
    assert toolaccess.known_tools(env) == ["calc", "sum"]
    assert toolaccess.rows(env) == ["calc", "sum", "zzz"]
    assert toolaccess.rows({}) == []


# --- API ----------------------------------------------------------------------------------------------------------


def test_put_takes_the_whole_map_validates_and_round_trips(demo):
    assert "tool_access" not in demo.get("/api/v1/groups/demo/environments/prod").json()
    body = {"calc": {"list": ["key", "viewer"], "call": ["key"]}, "not-deployed-yet": {"list": [], "call": []}}
    r = demo.put(URL, json=body)
    assert r.status_code == 200, r.text
    assert r.json()["tool_access"] == body
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["tool_access"] == body
    assert demo.put(URL, json={"tool_access": body}).json()["tool_access"] == body  # wrapped is fine too
    r = demo.put(URL, json={"calc": {"list": ["root"]}})
    assert r.status_code == 422 and "root" in r.json()["detail"]
    r = demo.put(URL, json={"calc": {"list": ["key"], "call": ["viewer"]}})
    assert r.status_code == 422 and "may call calc but not list it" in r.json()["detail"]  # U10 capitalises
    assert demo.put(URL, json=["calc"]).status_code == 422
    assert demo.put(URL, json={"calc": {"list": ALL, "call": ALL}}).json()["tool_access"] == {}  # back to open
    assert demo.put(URL, json={}).json()["tool_access"] == {}
    assert demo.put("/api/v1/groups/demo/environments/nope/tool-access", json={}).status_code == 404
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "environment.tool_access"]
    assert any("tool:calc" in a["tags"] and "tool:not-deployed-yet" in a["tags"] for a in rows)


def test_put_accepts_what_the_group_pages_form_posts(demo):
    # the json extension posts the multi dropdowns as `list:<tool>` / `call:<tool>`; one box = string, none = ""
    form = {"list:calc": ["", "key", "viewer"], "call:calc": "key", "list:sum": "", "call:sum": ""}
    assert demo.put(URL, json=form).json()["tool_access"] == {
        "calc": {"list": ["key", "viewer"], "call": ["key"]},
        "sum": {"list": [], "call": []},
    }


def test_group_admins_of_the_group_write_viewers_and_other_admins_do_not(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "og@x", "group_admin", ["other"])
    make_user(demo, "v@x", "viewer", ["demo"])
    body = {"calc": {"list": ["key"], "call": ["key"]}}
    for email, code in (("ga@x", 200), ("og@x", 403), ("v@x", 403)):
        with TestClient(demo.app) as c:
            login(c, email, PW)
            assert c.put(URL, json=body).status_code == code, email


# --- deploy -------------------------------------------------------------------------------------------------------


def test_deploy_hands_every_zone_the_compact_map(demo):
    demo.put(URL, json={"calc": {"list": ["viewer", "key"], "call": ["key"]}})
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok", job
    for z in ("zone-a", "zone-b"):
        env_file = Path(job["result"][z]["env_file"]).read_text()
        assert 'RAMEN_TOOL_ACCESS={"calc":{"call":["key"],"list":["key","viewer"]}}\n' in env_file, z
        assert env_file.index("RAMEN_BLOCKED=") < env_file.index("RAMEN_TOOL_ACCESS=")
    demo.put(URL, json={})
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert "RAMEN_TOOL_ACCESS={}\n" in Path(job["result"]["zone-a"]["env_file"]).read_text()


# --- token claim --------------------------------------------------------------------------------------------------


def _claims(demo, user_client) -> dict:
    cid = register(demo)
    verifier, challenge = pkce()
    tok = exchange(demo, cid, consent(user_client, cid, challenge), verifier).json()
    secret = asyncio.run(demo.app.state.services.store.get("groups", "demo"))["session_secret"]
    return node_verify(tok["access_token"], secret)


def test_access_token_carries_the_users_role_in_the_scopes_group(demo):
    assert _claims(demo, demo)["role"] == "super_admin"
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "m@x", "mcp_user", ["demo"])
    # a group admin elsewhere is only a viewer here: the claim is the role in THIS group (D41)
    mixed = {"email": "mixed@x", "password": PW, "memberships": {"demo": "viewer", "other": "group_admin"}}
    assert demo.post("/api/v1/users", json=mixed).status_code == 201
    for email, role in (("ga@x", "group_admin"), ("v@x", "viewer"), ("m@x", "mcp_user"), ("mixed@x", "viewer")):
        with TestClient(demo.app) as c:
            login(c, email, PW)
            claims = _claims(demo, c)
            assert claims["role"] == role, email
            assert claims["group"] == "demo" and claims["scope"] == "mcp:demo:zone-a"


# --- group page ---------------------------------------------------------------------------------------------------


def test_group_page_shows_a_tool_access_table_per_environment_and_badges_restricted_tools(demo):
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok", job
    demo.put(URL, json={"calc": {"list": ["key", "viewer"], "call": ["key"]}, "ghost": {"list": [], "call": []}})
    page = demo.get("/groups/demo").text
    assert "<h2>Tool access</h2>" in page
    assert 'hx-put="/api/v1/groups/demo/environments/prod/tool-access"' in page
    for mode in ("list", "call"):
        assert f'<details class="multi" id="ta-prod-calc-{mode}">' in page, mode
    assert 'name="list:calc" value="viewer" checked' in page and 'name="list:calc" value="mcp_user">' in page
    assert 'name="call:calc" value="key" checked' in page and 'name="call:calc" value="viewer">' in page
    assert '<details class="multi" id="ta-prod-ghost-list">' in page and "not in the last deploy" in page
    assert page.count('<span class="badge error">restricted</span>') >= 2  # the table rows and the packages row
    assert 'type="hidden" name="list:calc" value=""' in page  # an emptied column still reaches the API
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        text = v.get("/groups/demo").text
        assert "<h2>Tool access</h2>" in text and "tool-access" not in text and 'id="ta-prod-calc-list"' not in text
        assert "key, viewer" in text


def test_an_unrestricted_environment_offers_every_kind_checked(demo):
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok", job
    page = demo.get("/groups/demo").text
    for kind in ALL:
        assert f'name="list:calc" value="{kind}" checked' in page and f'name="call:calc" value="{kind}" checked' in page
    assert "restricted</span>" not in page
    assert json.loads(demo.get("/api/v1/groups/demo/environments/prod").text).get("tool_access", {}) == {}
