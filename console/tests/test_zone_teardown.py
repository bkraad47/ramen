"""0.6.0: a zone that is deleted, or dropped by every environment of a group, is really torn down in the cloud
(namespace, workers, zone identity) and its worker record removed — not just forgotten by the console."""

import asyncio

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures


def spy(demo):
    calls = []
    real = demo.app.state.services.cloud.detach_zone

    async def detach_zone(group, zone):
        calls.append((group, zone))
        return await real(group, zone)

    demo.app.state.services.cloud.detach_zone = detach_zone
    return calls


def worker_doc(demo, group, zone):
    return asyncio.run(demo.app.state.store.get("workers", f"{group}:{zone}"))


def test_deleting_a_zone_tears_down_every_groups_deployment_in_it(demo):
    calls = spy(demo)
    assert demo.put("/api/v1/groups/demo/zones/zone-b/workers", json={"count": 2}).status_code == 200
    assert demo.post("/api/v1/groups/other/environments", json={"name": "dev", "zones": ["zone-b"]}).status_code == 201
    assert worker_doc(demo, "demo", "zone-b")
    assert demo.delete("/api/v1/zones/zone-b").status_code == 200
    assert calls == [("demo", "zone-b"), ("other", "zone-b")]
    assert worker_doc(demo, "demo", "zone-b") is None
    assert demo.get("/api/v1/groups/demo/environments/prod").json()["zones"] == ["zone-a"]
    assert demo.get("/api/v1/zones/zone-b").status_code == 404


def test_dropping_a_zone_from_an_environment_tears_it_down_unless_another_environment_uses_it(demo):
    calls = spy(demo)
    assert (
        demo.post("/api/v1/groups/demo/environments", json={"name": "staging", "zones": ["zone-b"]}).status_code == 201
    )
    demo.put("/api/v1/groups/demo/zones/zone-b/workers", json={"count": 3})
    assert demo.put("/api/v1/groups/demo/environments/prod", json={"zones": ["zone-a"]}).status_code == 200
    assert calls == [] and worker_doc(demo, "demo", "zone-b")  # staging still runs there
    assert demo.put("/api/v1/groups/demo/environments/staging", json={"zones": []}).status_code == 200
    assert calls == [("demo", "zone-b")] and worker_doc(demo, "demo", "zone-b") is None
    # deleting an environment tears its zones down the same way; a zone another environment keeps is left alone
    calls.clear()
    assert (
        demo.post("/api/v1/groups/demo/environments", json={"name": "qa", "zones": ["zone-a", "zone-b"]}).status_code
        == 201
    )
    assert demo.delete("/api/v1/groups/demo/environments/qa").status_code == 200
    assert calls == [("demo", "zone-b")]  # zone-a is prod's


def test_local_adapter_forgets_the_zones_records(demo, tmp_path):
    svc = demo.app.state.services
    asyncio.run(svc.apply_sa_permissions("demo", "zone-b", "bucket.read"))
    r = asyncio.run(svc.cloud.detach_zone("demo", "zone-b"))
    assert r["ok"] and len(r["removed"]) == 1 and r["removed"][0].endswith("sa_permissions_zone-b.json")
