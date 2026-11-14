"""CONTRACTS §26.1 (D61) as §27 kept it: a group's zones are the zones listing it (derived from the zone's
environments since 1.0.0b); worker records exist only for bound pairs; the group page is one Environments table."""

import asyncio

from fastapi.testclient import TestClient

from ramen_console.storage import make_store
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures

URL = "/api/v1/groups/demo/zones"


def as_user(app, demo, email, role, groups):  # noqa: F811
    make_user(demo, email, role, groups)
    c = TestClient(app)
    login(c, email, PW)
    return c


def run(coro):
    return asyncio.run(coro)


def test_a_groups_zones_are_the_zones_listing_it(app, demo):  # noqa: F811
    assert demo.get(URL).json() == {"zones": ["zone-a", "zone-b"]}
    assert demo.get("/api/v1/groups/other/zones").json() == {"zones": []}
    assert demo.put(URL, json={"zones": ["zone-a"]}).status_code in (404, 405)  # §25's PUT is gone
    assert demo.get("/api/v1/zones/zone-a").json()["groups"] == ["demo"]
    assert "allowed_zones" not in demo.get("/api/v1/groups/demo").json()
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.get(URL).json() == {"zones": ["zone-a", "zone-b"]}  # a group admin reads them
    w = demo.get("/api/v1/groups/demo/zones/zone-a/workers")
    assert w.status_code == 200 and w.json()["count"] == 1 and w.json()["size"] == "s"
    assert demo.get("/api/v1/groups/other/zones/zone-a/workers").status_code == 404  # an unbound pair has no record


def test_an_environment_may_name_only_zones_the_caller_may_edit(app, demo):  # noqa: F811
    assert demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local"}).status_code == 201
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    r = ga.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-c"]})
    assert r.status_code == 403 and "zone-c" in r.text and "not bound to group demo" in r.text  # serves no group yet
    assert ga.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-a"]}).status_code == 201
    r = demo.post("/api/v1/groups/demo/environments", json={"name": "qa", "zones": ["zone-c"]})  # a super admin may
    assert r.status_code == 201 and demo.get("/api/v1/zones/zone-c").json()["groups"] == ["demo"]
    assert ga.put("/api/v1/groups/demo/environments/dev", json={"zones": ["zone-c"]}).status_code == 200  # now theirs
    go = as_user(app, demo, "go@ramen.local", "group_admin", ["other"])
    assert go.post("/api/v1/groups/other/environments", json={"name": "e", "zones": ["zone-a"]}).status_code == 403
    assert go.post("/api/v1/groups/other/environments", json={"name": "e", "zones": []}).status_code == 201


def test_deleting_a_group_unbinds_its_zones(demo):  # noqa: F811
    demo.put("/api/v1/environments/prod", json={"groups": ["demo", "other"]})
    assert demo.delete("/api/v1/groups/other").status_code == 200
    assert demo.get("/api/v1/zones/zone-a").json()["groups"] == ["demo"]
    demo.post("/api/v1/environments", json={"name": "only", "groups": ["demo"]})
    demo.put("/api/v1/zones/zone-b", json={"environments": ["prod", "only"]})
    assert demo.delete("/api/v1/groups/demo").status_code == 200
    z = demo.get("/api/v1/zones/zone-b").json()
    assert z["groups"] == [] and z["environments"] == []  # §27.1: names with no doc left go too


def test_the_group_page_is_one_environments_table(app, demo):  # noqa: F811
    assert demo.post("/api/v1/environments", json={"name": "spare", "groups": ["demo"]}).status_code == 201
    assert (
        demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local", "environments": ["spare"]}).status_code
        == 201
    )
    assert demo.post("/api/v1/zones", json={"name": "zone-d", "provider": "local"}).status_code == 201
    html = demo.get("/groups/demo").text
    assert "Zones and workers</h2>" not in html and "Zone actions</h2>" not in html and "allowed-zones" not in html
    assert html.count("<h2>Environments</h2>") == 1
    assert 'class="zone-name">zone-a<' in html and 'class="zone-name">zone-b<' in html
    assert 'class="zone-name">zone-c<' in html  # spare's zone row
    assert "zone-d" not in html.split("<h2>Environments</h2>")[1].split("<h2>")[0]  # not this group's
    assert 'hx-get="/ui/groups/demo/zones/zone-a/workers"' in html  # the live cell
    assert html.count('<details class="zone-tools">') == 3  # the actions, one disclosure per zone row
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert "zone-tools" in ga.get("/groups/demo").text
    v = as_user(app, demo, "v@ramen.local", "viewer", ["demo"])
    page = v.get("/groups/demo").text
    assert "zone-tools" not in page and "zone-actions" not in page and 'class="zone-name">zone-a<' in page


def test_existing_zones_are_bound_to_the_groups_that_used_them(cloud):  # noqa: F811
    from ramen_console.services import Services

    store = make_store()

    async def go():
        for z in ("zone-a", "zone-b"):
            await store.put("zones", z, {"name": z})
        await store.put("zones", "zone-c", {"name": "zone-c", "groups": ["x"]})
        await store.put("groups", "legacy", {"name": "legacy", "ref": "main", "allowed_zones": ["zone-b"]})
        await store.put("groups", "x", {"name": "x", "ref": "main"})
        await store.put("environments", "legacy:e", {"group": "legacy", "name": "e", "zones": ["zone-a"]})
        s = Services(store, cloud)
        assert await s.migrate_zone_groups() == 2
        assert (await store.get("zones", "zone-a"))["groups"] == ["legacy"]
        assert (await store.get("zones", "zone-b"))["groups"] == ["legacy"]
        assert (await store.get("zones", "zone-c"))["groups"] == ["x"]  # present: left alone
        assert "allowed_zones" not in await store.get("groups", "legacy")
        assert await store.get("workers", "legacy:zone-a") and await store.get("workers", "legacy:zone-b")
        assert await s.migrate_zone_groups() == 0
        assert await s.group_zones("legacy") == ["zone-a", "zone-b"]

    run(go())
