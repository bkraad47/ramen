"""CONTRACTS §26.1 (D61): zones are bound to groups. A zone lists its groups (super admin); a group's zones are the
zones listing it; worker records exist only for bound pairs; an environment picks from the group's zones."""

import asyncio

from fastapi.testclient import TestClient

from ramen_console.storage import make_store
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures

URL = "/api/v1/groups/demo/zones"
ZA = "/api/v1/zones/zone-a/groups"


def as_user(app, demo, email, role, groups):  # noqa: F811
    make_user(demo, email, role, groups)
    c = TestClient(app)
    login(c, email, PW)
    return c


def run(coro):
    return asyncio.run(coro)


def test_a_groups_zones_are_the_zones_listing_it(demo):  # noqa: F811
    assert demo.get(URL).json() == {"zones": ["zone-a", "zone-b"]}
    assert demo.get("/api/v1/groups/other/zones").json() == {"zones": []}
    assert demo.put(URL, json={"zones": ["zone-a"]}).status_code in (404, 405)  # §25's PUT is gone
    assert demo.get("/api/v1/zones/zone-a").json()["groups"] == ["demo"]
    assert "allowed_zones" not in demo.get("/api/v1/groups/demo").json()


def test_only_a_super_admin_binds_and_every_group_must_exist(app, demo):  # noqa: F811
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.get(URL).json() == {"zones": ["zone-a", "zone-b"]}  # a group admin reads them
    assert ga.put(ZA, json={"groups": ["demo", "other"]}).status_code == 403
    assert demo.put(ZA, json={"groups": ["demo", "nope"]}).status_code == 422
    assert demo.put("/api/v1/zones/nope/groups", json={"groups": []}).status_code == 404
    assert demo.put(ZA, json={"groups": ["demo", "other", "demo"]}).json()["groups"] == ["demo", "other"]
    assert demo.get("/api/v1/groups/other/zones").json() == {"zones": ["zone-a"]}
    a = next(a for a in demo.get("/api/v1/audit").json() if a["action"] == "zone.groups" and a["ok"])
    assert a["target"] == "zone-a" and {"group:demo", "group:other", "zone:zone-a"} <= set(a["tags"])
    # binding created the worker record with the defaults; the pair is now a known one
    w = demo.get("/api/v1/groups/other/zones/zone-a/workers")
    assert w.status_code == 200 and w.json()["count"] == 1 and w.json()["size"] == "s"
    assert run(demo.app.state.store.get("workers", "other:zone-a"))


def test_a_zone_in_use_cannot_be_unbound(demo):  # noqa: F811
    r = demo.put("/api/v1/zones/zone-b/groups", json={"groups": []})
    assert r.status_code == 409 and "prod" in r.text and "zone-b" in r.text
    assert demo.get("/api/v1/zones/zone-b").json()["groups"] == ["demo"]


def test_unbinding_detaches_the_zone_for_that_group(demo, cloud):  # noqa: F811
    demo.put(ZA, json={"groups": ["demo", "other"]})
    rules = cloud._bucket("other") / ".ramen" / "ip_rules_zone-a.json"
    assert demo.put("/api/v1/groups/other/zones/zone-a/ip-rules", json={"cidrs": ["10.0.0.0/8"]}).status_code == 200
    assert rules.exists()
    assert demo.put(ZA, json={"groups": ["demo"]}).json()["groups"] == ["demo"]
    assert not rules.exists()  # the adapter's detach ran
    assert run(demo.app.state.store.get("workers", "other:zone-a")) is None
    r = demo.get("/api/v1/groups/other/zones/zone-a/workers")
    assert r.status_code == 404  # an unbound pair has no worker record
    assert demo.get("/api/v1/groups/demo/zones/zone-a/workers").status_code == 200


def test_an_environment_may_name_only_the_groups_zones_whoever_asks(app, demo):  # noqa: F811
    assert demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local"}).status_code == 201
    r = demo.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-c"]})
    assert r.status_code == 403 and "zone-c" in r.text and "Zones page" in r.text  # a super admin too (§26.1)
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.put("/api/v1/groups/demo/environments/prod", json={"zones": ["zone-a", "zone-c"]}).status_code == 403
    assert ga.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-a"]}).status_code == 201
    demo.put("/api/v1/zones/zone-c/groups", json={"groups": ["demo"]})
    assert ga.put("/api/v1/groups/demo/environments/dev", json={"zones": ["zone-c"]}).status_code == 200
    go = as_user(app, demo, "go@ramen.local", "group_admin", ["other"])
    assert go.post("/api/v1/groups/other/environments", json={"name": "e", "zones": ["zone-a"]}).status_code == 403
    assert go.post("/api/v1/groups/other/environments", json={"name": "e", "zones": []}).status_code == 201


def test_a_new_zone_takes_its_groups(demo):  # noqa: F811
    assert demo.post("/api/v1/zones", json={"name": "zone-x", "groups": ["nope"]}).status_code == 422
    r = demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local", "groups": ["demo"]})
    assert r.status_code == 201 and r.json()["groups"] == ["demo"]
    assert run(demo.app.state.store.get("workers", "demo:zone-c"))
    assert demo.get(URL).json() == {"zones": ["zone-a", "zone-b", "zone-c"]}
    assert demo.post("/api/v1/zones", json={"name": "zone-d"}).json()["groups"] == []


def test_deleting_a_group_unbinds_its_zones(demo):  # noqa: F811
    demo.put(ZA, json={"groups": ["demo", "other"]})
    assert demo.delete("/api/v1/groups/other").status_code == 200
    assert demo.get("/api/v1/zones/zone-a").json()["groups"] == ["demo"]


def test_the_group_page_is_one_environments_table(app, demo):  # noqa: F811
    assert demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local", "groups": ["demo"]}).status_code
    assert demo.post("/api/v1/zones", json={"name": "zone-d", "provider": "local"}).status_code == 201
    html = demo.get("/groups/demo").text
    assert "Zones and workers</h2>" not in html and "Zone actions</h2>" not in html and "allowed-zones" not in html
    assert html.count("<h2>Environments</h2>") == 1
    assert 'class="zone-name">zone-a<' in html and 'class="zone-name">zone-b<' in html
    assert 'class="zone-name">zone-c<' not in html  # bound, used by no environment: listed once below the table
    assert "Bound, unused: <code>zone-c</code>" in html and "zone-d" not in html.split("Bound, unused")[1][:200]
    picker = html.split('id="env-zones"')[1].split("</details>")[0]
    assert 'value="zone-c"' in picker and 'value="zone-d"' not in picker  # the picker offers the group's zones
    assert 'hx-get="/ui/groups/demo/zones/zone-a/workers"' in html  # the live cell
    assert html.count('<details class="zone-tools">') == 2  # the actions, one disclosure per zone row
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert "zone-tools" in ga.get("/groups/demo").text
    v = as_user(app, demo, "v@ramen.local", "viewer", ["demo"])
    page = v.get("/groups/demo").text
    assert "zone-tools" not in page and "zone-actions" not in page and 'class="zone-name">zone-a<' in page


def test_the_zones_page_has_a_groups_column_and_lists_bound_pairs_only(app, demo):  # noqa: F811
    html = demo.get("/zones").text
    assert 'id="zone-groups-zone-a"' in html and "Save groups" in html
    assert 'hx-put="/api/v1/zones/zone-a/groups"' in html
    assert 'hx-get="/ui/groups/demo/zones/zone-a/workers"' in html
    assert "/ui/groups/other/zones/zone-a/workers" not in html  # not bound
    demo.put(ZA, json={"groups": ["demo", "other"]})
    assert "/ui/groups/other/zones/zone-a/workers" in demo.get("/zones").text
    ga = as_user(app, demo, "ga@ramen.local", "group_admin", ["demo"])
    page = ga.get("/zones").text
    assert "Save groups" not in page and "zone-groups-zone-a" not in page and "demo, other" in page


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
