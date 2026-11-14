"""CONTRACTS §27 (v1.0.0b): an environment is a name shared across groups, a zone names its environments and derives
its groups, every derived pair has a worker record and is deployed when it appears; one admin page for all of it."""

import asyncio

from fastapi.testclient import TestClient

from ramen_console.storage import make_store
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import wait_job

ENVS, ZONES = "/api/v1/environments", "/api/v1/zones"


def run(coro):
    return asyncio.run(coro)


def as_user(app, demo, email, role, groups):  # noqa: F811
    make_user(demo, email, role, groups)
    c = TestClient(app)
    login(c, email, PW)
    return c


def spy(demo, name):  # noqa: F811
    calls, real = [], getattr(demo.app.state.services.cloud, name)

    async def rec(group, zone, *a, **k):
        calls.append((group, zone))
        return await real(group, zone, *a, **k)

    setattr(demo.app.state.services.cloud, name, rec)
    return calls


def worker(demo, group, zone):  # noqa: F811
    return run(demo.app.state.store.get("workers", f"{group}:{zone}"))


def jobs_of(demo, ids):  # noqa: F811
    return [wait_job(demo, j) for j in ids]


# --- §27.2 environments by name ------------------------------------------------------------------------------------
def test_environments_are_listed_by_name_scoped_to_the_callers_groups(app, demo):  # noqa: F811
    rows = demo.get(f"{ENVS}/by-name").json()
    assert [r["name"] for r in rows] == ["prod"]
    assert rows[0]["groups"] == ["demo"] and rows[0]["zones"] == ["zone-a", "zone-b"] and rows[0]["ref"] == "main"
    assert rows[0]["ref_differs"] is False and set(rows[0]["last_deploy"]) == {"demo"}
    assert as_user(app, demo, "go@ramen.local", "group_admin", ["other"]).get(f"{ENVS}/by-name").json() == []
    assert as_user(app, demo, "v@ramen.local", "viewer", ["demo"]).get(f"{ENVS}/by-name").json()[0]["name"] == "prod"


def test_create_update_and_delete_an_environment_across_groups(app, demo):  # noqa: F811
    assert demo.post(ENVS, json={"name": "prod", "groups": ["other"]}).status_code == 409
    assert demo.post(ENVS, json={"name": "dev", "groups": []}).status_code == 422
    assert demo.post(ENVS, json={"name": "dev", "groups": ["nope"]}).status_code == 422
    assert demo.post(ENVS, json={"name": "Bad Name", "groups": ["demo"]}).status_code == 422
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.post(ENVS, json={"name": "dev", "groups": ["demo", "other"]}).status_code == 403
    assert ga.put(f"{ENVS}/prod", json={"groups": ["demo", "other"]}).status_code == 403
    r = demo.post(ENVS, json={"name": "dev", "ref": "v2", "groups": ["demo", "other"]})
    assert r.status_code == 201 and r.json()["groups"] == ["demo", "other"] and r.json()["zones"] == []
    assert demo.get("/api/v1/groups/other/environments/dev").json()["ref"] == "v2"
    assert demo.put(f"{ENVS}/dev", json={"ref": "v3"}).json()["ref"] == "v3"
    assert demo.get("/api/v1/groups/demo/environments/dev").json()["ref"] == "v3"
    assert demo.put(f"{ENVS}/dev", json={"groups": ["other"]}).json()["groups"] == ["other"]
    assert demo.get("/api/v1/groups/demo/environments/dev").status_code == 404
    assert demo.put(f"{ENVS}/dev", json={"groups": []}).status_code == 422
    assert demo.put(f"{ENVS}/nope", json={"ref": "x"}).status_code == 404
    assert demo.delete(f"{ENVS}/dev").status_code == 204
    assert demo.delete(f"{ENVS}/dev").status_code == 404
    assert (
        as_user(app, demo, "v@ramen.local", "viewer", ["demo"])
        .post(ENVS, json={"name": "q", "groups": ["demo"]})
        .status_code
        == 403
    )
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "environment.create" and a["target"] == "dev"]
    assert any(a["ok"] and {"group:demo", "group:other"} <= set(a["tags"]) for a in rows)


def test_a_group_admin_edits_what_is_wholly_theirs(app, demo):  # noqa: F811
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.post(ENVS, json={"name": "dev", "groups": ["demo"]}).status_code == 201
    assert ga.put(f"{ENVS}/dev", json={"ref": "v2"}).status_code == 200
    r = ga.post(ZONES, json={"name": "zone-c", "provider": "local", "region": "local", "environments": ["dev"]})
    assert r.status_code == 201 and r.json()["groups"] == ["demo"]
    assert ga.put(f"{ZONES}/zone-c", json={"region": "here"}).status_code == 200
    demo.put(f"{ENVS}/dev", json={"groups": ["demo", "other"]})  # now partly another group's
    assert ga.put(f"{ENVS}/dev", json={"ref": "v3"}).status_code == 403
    assert ga.put(f"{ZONES}/zone-c", json={"region": "there"}).status_code == 403
    assert ga.delete(f"{ZONES}/zone-c").status_code == 403
    assert ga.delete(f"{ENVS}/dev").status_code == 403
    assert ga.put(f"{ZONES}/zone-a", json={"environments": ["prod", "dev"]}).status_code == 403  # would add other
    assert demo.delete(f"{ZONES}/zone-c").status_code == 200


# --- §27.2 / §27.3 zones name environments; derived pairs are bound and deployed ------------------------------------
def test_a_new_zone_binds_and_deploys_every_derived_pair(demo):  # noqa: F811
    assert demo.post(ZONES, json={"name": "zone-x", "groups": ["demo"]}).status_code == 422  # §26.1's field is gone
    assert demo.post(ZONES, json={"name": "zone-x", "environments": ["nope"]}).status_code == 422
    attached = spy(demo, "attach_zone")
    r = demo.post(ZONES, json={"name": "zone-c", "provider": "local", "region": "local", "environments": ["prod"]})
    assert r.status_code == 201, r.text
    z = r.json()
    assert z["environments"] == ["prod"] and z["groups"] == ["demo"] and len(z["jobs"]) == 1
    assert attached == [("demo", "zone-c")] and worker(demo, "demo", "zone-c")
    job = jobs_of(demo, z["jobs"])[0]
    assert job["target"] == "demo/prod" and job["kind"] == "deploy" and job["status"] in ("ok", "error")
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["zones"] == ["zone-a", "zone-b", "zone-c"]
    assert demo.get("/api/v1/groups/demo/zones").json() == {"zones": ["zone-a", "zone-b", "zone-c"]}
    assert demo.get(f"{ZONES}/zone-c").json()["groups"] == ["demo"]
    r = demo.post(ZONES, json={"name": "zone-d"}).json()
    assert r["environments"] == [] and r["groups"] == [] and r["jobs"] == []


def test_a_zone_that_gains_or_loses_an_environment_binds_or_detaches(demo):  # noqa: F811
    detached = spy(demo, "detach_zone")
    assert demo.post(ENVS, json={"name": "dev", "groups": ["other"]}).status_code == 201
    r = demo.put(f"{ZONES}/zone-a", json={"environments": ["prod", "dev"]})
    assert r.status_code == 200 and r.json()["groups"] == ["demo", "other"] and len(r.json()["jobs"]) == 1
    assert jobs_of(demo, r.json()["jobs"])[0]["target"] == "other/dev"
    assert worker(demo, "other", "zone-a") and demo.get("/api/v1/groups/other/zones").json() == {"zones": ["zone-a"]}
    assert demo.get("/api/v1/groups/other/environments/dev").json()["zones"] == ["zone-a"]
    r = demo.put(f"{ZONES}/zone-a", json={"environments": ["prod"]})
    assert r.json()["groups"] == ["demo"] and r.json()["jobs"] == [] and detached == [("other", "zone-a")]
    assert worker(demo, "other", "zone-a") is None
    assert demo.get("/api/v1/groups/other/environments/dev").json()["zones"] == []
    r = demo.put(f"{ZONES}/zone-b", json={"environments": []})
    assert r.json()["groups"] == [] and detached[-1] == ("demo", "zone-b")
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["zones"] == ["zone-a"]
    assert demo.put(f"{ZONES}/zone-b", json={"environments": ["nope"]}).status_code == 422
    assert demo.put(f"{ZONES}/nope", json={"environments": []}).status_code == 404
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "zone.update" and a["ok"]]
    assert any(a["target"] == "zone-b" and "env:prod" in a["tags"] and "zone:zone-b" in a["tags"] for a in rows)


def test_a_region_change_reapplies_every_pair_in_place_keeping_the_records(demo):  # noqa: F811
    demo.put("/api/v1/groups/demo/zones/zone-a/workers", json={"count": 2, "max_count": 3})
    detached, attached = spy(demo, "detach_zone"), spy(demo, "attach_zone")
    assert demo.put(f"{ZONES}/zone-a", json={"provider": "gcp"}).status_code == 422
    assert demo.put(f"{ZONES}/zone-a", json={"region": "local", "provider": "local"}).json()["jobs"] == []  # unchanged
    r = demo.put(f"{ZONES}/zone-a", json={"region": "elsewhere"})
    assert r.status_code == 200 and r.json()["region"] == "elsewhere" and len(r.json()["jobs"]) == 1
    assert detached == [] and attached == [("demo", "zone-a")]  # D66: no namespace delete, the rollout moves the pods
    w = worker(demo, "demo", "zone-a")
    assert w["count"] == 2 and w["max_count"] == 3  # D66
    assert jobs_of(demo, r.json()["jobs"])[0]["target"] == "demo/prod"
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "zone.update" and a["ok"]]
    assert any("region:elsewhere" in a["tags"] and "zone:zone-a" in a["tags"] for a in rows)


def test_a_running_deploy_skips_the_pairs_job(demo, monkeypatch):  # noqa: F811
    running = run(demo.app.state.jobs.create("deploy", "demo/prod"))
    r = demo.post(ZONES, json={"name": "zone-c", "provider": "local", "region": "local", "environments": ["prod"]})
    assert r.status_code == 201 and len(r.json()["jobs"]) == 1
    j = demo.get(f"/api/v1/jobs/{r.json()['jobs'][0]}").json()
    assert j["status"] == "error" and j["error"] == "a deploy is already running"
    assert worker(demo, "demo", "zone-c")  # the write still succeeded
    running.update(status="ok", finished="x")
    run(demo.app.state.jobs.save(running))


def test_an_environment_gaining_a_group_attaches_and_deploys_its_zones(demo):  # noqa: F811
    attached = spy(demo, "attach_zone")
    r = demo.put(f"{ENVS}/prod", json={"groups": ["demo", "other"]})
    assert r.status_code == 200 and r.json()["groups"] == ["demo", "other"]
    assert sorted(attached) == [("other", "zone-a"), ("other", "zone-b")] and len(r.json()["jobs"]) == 1
    assert jobs_of(demo, r.json()["jobs"])[0]["target"] == "other/prod"
    assert demo.get(f"{ZONES}/zone-a").json()["groups"] == ["demo", "other"]
    assert demo.get("/api/v1/groups/other/environments/prod").json()["zones"] == ["zone-a", "zone-b"]
    detached = spy(demo, "detach_zone")
    assert demo.put(f"{ENVS}/prod", json={"groups": ["other"]}).json()["groups"] == ["other"]
    assert sorted(detached) == [("demo", "zone-a"), ("demo", "zone-b")]
    assert demo.get(f"{ZONES}/zone-a").json()["groups"] == ["other"] and worker(demo, "demo", "zone-a") is None


def test_deleting_an_environment_deletes_the_zones_only_it_used(demo):  # noqa: F811
    assert demo.post(ENVS, json={"name": "dev", "groups": ["other"]}).status_code == 201
    assert demo.post(ZONES, json={"name": "zone-d", "provider": "local", "environments": ["dev"]}).status_code == 201
    assert demo.put(f"{ZONES}/zone-a", json={"environments": ["prod", "dev"]}).status_code == 200
    detached = spy(demo, "detach_zone")
    assert demo.delete(f"{ENVS}/dev").status_code == 204
    assert demo.get(f"{ZONES}/zone-d").status_code == 404  # only dev used it: zone and record gone
    assert worker(demo, "other", "zone-d") is None
    z = demo.get(f"{ZONES}/zone-a").json()
    assert z["environments"] == ["prod"] and z["groups"] == ["demo"]  # shared: detached for other only
    assert sorted(detached) == [("other", "zone-a"), ("other", "zone-d")]
    assert demo.get("/api/v1/groups/other/environments/dev").status_code == 404


def test_the_per_group_shortcut_attaches_the_environment_to_zones_the_caller_may_edit(app, demo):  # noqa: F811
    go = as_user(app, demo, "go@ramen.local", "group_admin", ["other"])
    r = go.post("/api/v1/groups/other/environments", json={"name": "prod", "zones": ["zone-a"]})
    assert r.status_code == 403 and "zone-a" in r.text and "not bound to group other" in r.text
    assert go.post("/api/v1/groups/other/environments", json={"name": "dev", "zones": []}).status_code == 201
    assert demo.get(f"{ENVS}/by-name").json()[0]["name"] == "dev"
    r = demo.post("/api/v1/groups/other/environments", json={"name": "prod", "zones": ["zone-a"]})
    assert r.status_code == 201 and r.json()["zones"] == ["zone-a", "zone-b"] and r.json()["ref"] == "main"
    assert len(r.json()["jobs"]) == 1  # prod's zones apply to its new group: zone-b too, one deploy over both
    assert demo.get(f"{ZONES}/zone-a").json()["groups"] == ["demo", "other"] and worker(demo, "other", "zone-a")
    # PUT …/zones attaches or detaches the environment on those zones, for every group of it
    assert demo.put("/api/v1/groups/demo/environments/prod", json={"zones": ["zone-a"]}).status_code == 200
    assert (
        demo.get(f"{ZONES}/zone-b").json()["environments"] == [] and demo.get(f"{ZONES}/zone-b").json()["groups"] == []
    )
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert (
        ga.put("/api/v1/groups/demo/environments/prod", json={"zones": ["zone-a", "zone-b"]}).status_code == 403
    )  # zone-a is also other's
    # DELETE …/environments/{env} removes only that group's doc; the last group's delete is the environment's
    assert demo.delete("/api/v1/groups/other/environments/prod").status_code == 200
    assert demo.get(f"{ZONES}/zone-a").json()["groups"] == ["demo"] and worker(demo, "other", "zone-a") is None
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["zones"] == ["zone-a"]
    assert demo.delete("/api/v1/groups/demo/environments/prod").status_code == 200
    assert demo.get(f"{ZONES}/zone-a").status_code == 404 and demo.get(f"{ZONES}/zone-b").status_code == 200


def test_set_zone_groups_is_gone_and_delete_zone_follows_the_edit_rule(app, demo):  # noqa: F811
    assert demo.put(f"{ZONES}/zone-a/groups", json={"groups": ["demo"]}).status_code in (404, 405)
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.delete(f"{ZONES}/zone-b").status_code == 200  # wholly demo's
    assert ga.delete(f"{ZONES}/zone-b").status_code == 403  # gone: a group admin learns nothing of other zones
    assert demo.delete(f"{ZONES}/zone-b").status_code == 404
    v = as_user(app, demo, "v@ramen.local", "viewer", ["demo"])
    assert v.delete(f"{ZONES}/zone-a").status_code == 403
    assert v.put(f"{ZONES}/zone-a", json={"region": "x"}).status_code == 403


# --- §27.4 migration -----------------------------------------------------------------------------------------------
def test_existing_zones_learn_their_environments_once(cloud):  # noqa: F811
    from ramen_console.services import Services

    store = make_store()

    async def go():
        await store.put("groups", "g", {"name": "g", "ref": "main"})
        await store.put("zones", "z1", {"name": "z1", "groups": ["g"]})
        await store.put("zones", "z2", {"name": "z2", "groups": []})
        await store.put("zones", "z3", {"name": "z3", "groups": ["g"], "environments": ["kept"]})
        await store.put("environments", "g:e", {"group": "g", "name": "e", "zones": ["z1"]})
        s = Services(store, cloud)
        assert await s.migrate_zone_environments() == 2
        assert (await store.get("zones", "z1"))["environments"] == ["e"]
        assert (await store.get("zones", "z2"))["environments"] == []
        assert (await store.get("zones", "z3"))["environments"] == ["kept"] and (await store.get("zones", "z3"))[
            "groups"
        ] == ["g"]
        assert await s.migrate_zone_environments() == 0

    run(go())


# --- §27.4 pages ----------------------------------------------------------------------------------------------------
def test_the_page_is_for_admins_and_shows_each_their_rows(app, demo):  # noqa: F811
    demo.post(ENVS, json={"name": "dev", "groups": ["other"]})
    demo.post(ZONES, json={"name": "zone-o", "provider": "local", "region": "local", "environments": ["dev"]})
    html = demo.get("/environments").text
    assert "<title>Environments, Zones and Workers</title>" in html
    for h in ("New environment", "Environments", "New zone", "Zones", "Workers per group"):
        assert f"<h2>{h}</h2>" in html or f'<h2 style="margin-top:0">{h}</h2>' in html, h
    assert 'hx-post="/api/v1/environments"' in html and 'hx-post="/api/v1/zones"' in html
    assert 'hx-put="/api/v1/environments/prod"' in html and 'hx-put="/api/v1/zones/zone-a"' in html
    assert 'hx-delete="/api/v1/environments/prod"' in html and 'hx-delete="/api/v1/zones/zone-a"' in html
    assert (
        'hx-get="/ui/groups/demo/zones/zone-a/workers"' in html
        and 'hx-get="/ui/groups/other/zones/zone-o/workers"' in html
    )
    assert 'id="env-groups-prod"' in html and 'id="zone-envs-zone-a"' in html
    assert "/zones/zone-a/groups" not in html
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    page = ga.get("/environments").text
    assert 'hx-put="/api/v1/environments/prod"' in page and "zone-o" not in page and 'value="other"' not in page
    assert "/ui/groups/other/" not in page and 'hx-put="/api/v1/zones/zone-a"' in page
    demo.put(f"{ENVS}/prod", json={"groups": ["demo", "other"]})
    page = ga.get("/environments").text  # prod is now partly other's: read-only for demo's admin
    assert (
        'hx-put="/api/v1/environments/prod"' not in page
        and "prod" in page
        and 'hx-put="/api/v1/zones/zone-a"' not in page
    )
    v = as_user(app, demo, "v@ramen.local", "viewer", ["demo"])
    assert v.get("/environments").status_code == 403
    r = demo.get("/zones", follow_redirects=False)
    assert r.status_code == 301 and r.headers["location"].endswith("/environments")
    nav = demo.get("/").text
    assert "Environments, Zones and Workers</a>" in nav and 'href="/zones"' not in nav
    assert "Environments, Zones and Workers" not in v.get("/").text.split("<main>")[0]


def test_the_group_page_lost_its_environment_form_and_links_to_the_new_page(demo):  # noqa: F811
    html = demo.get("/groups/demo").text
    assert 'hx-post="/api/v1/groups/demo/environments"' not in html and 'id="env-zones"' not in html
    assert html.count("<h2>Environments</h2>") == 1 and 'href="/environments"' in html
    assert 'class="zone-name">zone-a<' in html and "Deploy (canary)" in html and "Delete</button>" in html
