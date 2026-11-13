"""1.0.0, the user on the live GKE console: a group must not reach every zone. Each group has `allowed_zones`, set by a
super admin; a group admin's environments may use only those; the group page shows only those."""

import asyncio

from ramen_console.storage import make_store
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures

URL = "/api/v1/groups/demo/zones"


def gadmin(app, demo):  # noqa: F811
    from fastapi.testclient import TestClient

    make_user(demo, "ga@ramen.local", "group_admin", ["demo"])
    c = TestClient(app)
    login(c, "ga@ramen.local", PW)
    return c


def test_a_super_admins_environment_allows_its_zones_and_the_group_reports_them(demo):  # noqa: F811
    assert demo.get(URL).json() == {"allowed": ["zone-a", "zone-b"]}
    assert demo.get("/api/v1/groups/other").json().get("allowed_zones", []) == []


def test_only_a_super_admin_changes_the_allowed_zones(app, demo):  # noqa: F811
    ga = gadmin(app, demo)
    assert ga.get(URL).json() == {"allowed": ["zone-a", "zone-b"]}  # a group admin may read them
    assert ga.put(URL, json={"allowed": ["zone-a", "zone-b", "zone-c"]}).status_code == 403
    assert (
        demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local", "region": "local"}).status_code == 201
    )
    assert demo.put(URL, json={"allowed": ["zone-a", "zone-b", "zone-c"]}).json() == {
        "allowed": ["zone-a", "zone-b", "zone-c"]
    }
    assert demo.put(URL, json={"allowed": ["zone-a", "nope"]}).status_code == 422  # unknown zone
    assert any(a["action"] == "group.zones" for a in demo.get("/api/v1/audit").json())


def test_a_zone_in_use_cannot_be_disallowed(demo):  # noqa: F811
    r = demo.put(URL, json={"allowed": ["zone-a"]})
    assert r.status_code == 409 and "prod" in r.text and "zone-b" in r.text
    assert demo.get(URL).json() == {"allowed": ["zone-a", "zone-b"]}


def test_a_group_admin_may_use_only_allowed_zones(app, demo):  # noqa: F811
    assert (
        demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local", "region": "local"}).status_code == 201
    )
    ga = gadmin(app, demo)
    r = ga.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-c"]})
    assert r.status_code == 403 and "zone-c" in r.text and "super admin" in r.text
    r = ga.put("/api/v1/groups/demo/environments/prod", json={"zones": ["zone-a", "zone-c"]})
    assert r.status_code == 403
    assert ga.post("/api/v1/groups/demo/environments", json={"name": "dev", "zones": ["zone-a"]}).status_code == 201
    # once allowed, the group admin may use it
    demo.put(URL, json={"allowed": ["zone-a", "zone-b", "zone-c"]})
    assert ga.put("/api/v1/groups/demo/environments/dev", json={"zones": ["zone-c"]}).status_code == 200


def test_the_other_group_never_reaches_demos_zones(app, demo):  # noqa: F811
    make_user(demo, "go@ramen.local", "group_admin", ["other"])
    from fastapi.testclient import TestClient

    go = TestClient(app)
    login(go, "go@ramen.local", PW)
    assert go.post("/api/v1/groups/other/environments", json={"name": "e", "zones": ["zone-a"]}).status_code == 403
    assert go.post("/api/v1/groups/other/environments", json={"name": "e", "zones": []}).status_code == 201


def test_the_group_page_shows_only_allowed_zones_and_the_editor_to_super_admins(app, demo):  # noqa: F811
    assert (
        demo.post("/api/v1/zones", json={"name": "zone-c", "provider": "local", "region": "local"}).status_code == 201
    )
    html = demo.get("/groups/demo").text
    assert 'class="zone-name">zone-a<' in html and 'class="zone-name">zone-c<' not in html
    assert 'id="allowed-zones"' in html and "zone-c" in html  # the super admin's editor offers every zone
    ga = gadmin(app, demo)
    page = ga.get("/groups/demo").text
    assert 'id="allowed-zones"' not in page and 'class="zone-name">zone-c<' not in page
    assert "Allowed zones" in page  # the group admin sees the list, read-only


def test_existing_groups_are_given_the_zones_their_environments_use(cloud):  # noqa: F811
    from ramen_console.services import Services

    store = make_store()

    async def run():
        await store.put("zones", "zone-a", {"id": "zone-a"})
        await store.put("groups", "legacy", {"name": "legacy", "ref": "main"})
        await store.put("environments", "legacy:e", {"group": "legacy", "name": "e", "zones": ["zone-a"]})
        s = Services(store, cloud)
        await s.migrate_allowed_zones()
        assert (await store.get("groups", "legacy"))["allowed_zones"] == ["zone-a"]
        await s.migrate_allowed_zones()  # idempotent, never widens a list a super admin set
        g = await store.get("groups", "legacy")
        g["allowed_zones"] = []
        await store.put("groups", "legacy", g)
        await s.migrate_allowed_zones()
        assert (await store.get("groups", "legacy"))["allowed_zones"] == []

    asyncio.run(run())
