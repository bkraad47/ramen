"""U5 / CONTRACTS §12.1: tools, resources and prompts can be blocked per zone, and deploy writes the union."""

from pathlib import Path

import pytest

from ramen_console.services import Services
from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures

ZB = "/api/v1/groups/demo/environments/prod/zones/{zone}/blocked"


def test_a_new_environment_starts_with_no_per_zone_blocks(demo):
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["blocked_zones"] == {}


def test_blocking_a_name_in_one_zone_only(demo):
    r = demo.put(ZB.format(zone="zone-a"), json={"blocked": ["calc", " calc ", "readme"]})
    assert r.status_code == 200, r.text
    assert r.json()["blocked_zones"] == {"zone-a": ["calc", "readme"]}
    assert r.json()["blocked"] == []  # the environment-wide list is untouched


def test_clearing_a_zone_removes_its_entry(demo):
    demo.put(ZB.format(zone="zone-a"), json={"blocked": ["calc"]})
    assert demo.put(ZB.format(zone="zone-a"), json={"blocked": []}).json()["blocked_zones"] == {}


def test_a_zone_not_attached_to_the_environment_is_refused(demo):
    assert demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local"}).status_code == 201
    r = demo.put(ZB.format(zone="zone-c"), json={"blocked": ["calc"]})
    assert r.status_code == 422
    assert "not attached" in r.json()["detail"]


def test_an_unknown_environment_is_a_404(demo):
    r = demo.put("/api/v1/groups/demo/environments/nope/zones/zone-a/blocked", json={"blocked": []})
    assert r.status_code == 404


def test_commas_are_still_refused(demo):
    assert demo.put(ZB.format(zone="zone-a"), json={"blocked": ["a,b"]}).status_code == 422


def test_a_viewer_may_not_toggle(demo, app):  # noqa: F811
    from fastapi.testclient import TestClient

    from tests.test_api import make_user

    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(app) as v:
        login(v, "v@x", "Passw0rd!-for-tests")
        assert v.put(ZB.format(zone="zone-a"), json={"blocked": ["calc"]}).status_code == 403


def test_the_union_is_what_a_zone_is_deployed_with():
    env = {"blocked": ["shared"], "blocked_zones": {"zone-a": ["only-a", "shared"]}}
    assert Services.blocked_for_zone(env, "zone-a") == ["shared", "only-a"]
    assert Services.blocked_for_zone(env, "zone-b") == ["shared"]
    assert Services.blocked_for_zone({}, "zone-a") == []


def test_deploy_writes_the_union_into_that_zones_ramen_blocked(demo):
    demo.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": ["shared"]})
    demo.put(ZB.format(zone="zone-a"), json={"blocked": ["only-a"]})
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok", job
    assert "RAMEN_BLOCKED=shared,only-a\n" in Path(job["result"]["zone-a"]["env_file"]).read_text()


def test_the_change_is_audited(demo):
    demo.put(ZB.format(zone="zone-a"), json={"blocked": ["calc"]})
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "environment.blocked_zone"]
    assert rows and "zone:zone-a" in rows[0]["tags"] and "blocked:calc" in rows[0]["tags"]


def test_the_group_page_lists_packages_per_zone_with_a_toggle(demo):
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok"
    page = demo.get("/groups/demo").text
    assert "/environments/prod/zones/zone-a/blocked" in page
    assert "Zone zone-a" in page
    assert "Disable" in page


def test_the_group_page_shows_a_blocked_package_as_enableable(demo):
    wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    demo.put(ZB.format(zone="zone-a"), json={"blocked": ["calc"]})
    page = demo.get("/groups/demo").text
    assert "Enable" in page


@pytest.mark.parametrize("zone", ["zone-a"])
def test_a_zone_block_survives_an_environment_update(demo, zone):
    demo.put(ZB.format(zone=zone), json={"blocked": ["calc"]})
    demo.put("/api/v1/groups/demo/environments/prod", json={"ref": "other"})
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["blocked_zones"] == {zone: ["calc"]}
