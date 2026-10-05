"""N7: per-zone (item) and per-group (scope) throttle config — round trip, masking, encryption, gating,
and that `run_deploy` actually passes the configured limits/URLs to the worker as env vars."""

import time

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures


def wait_job(client, jid):
    for _ in range(100):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job still running")


def test_item_throttle_round_trips_and_masks_the_redis_url(demo):
    r = demo.put(
        "/api/v1/groups/demo/zones/zone-a/item-throttle",
        json={"redis_url": "redis://u:p@host:6379", "ip_per_min": 10, "token_per_min": 5},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["item_ip_per_min"] == 10 and body["item_token_per_min"] == 5 and "redis_item_url" not in body

    r = demo.get("/api/v1/groups/demo/zones/zone-a/workers")
    assert r.status_code == 200 and "redis_item_url" not in r.json()


async def test_item_throttle_redis_url_is_encrypted_at_rest(demo):
    demo.put("/api/v1/groups/demo/zones/zone-a/item-throttle", json={"redis_url": "redis://u:p@host:6379"})
    raw = await demo.app.state.store.inner.get("workers", "demo:zone-a")
    assert raw["redis_item_url"] != "redis://u:p@host:6379" and raw["redis_item_url"].startswith("enc:")


def test_scope_throttle_round_trips_and_masks_the_redis_url(demo):
    r = demo.put(
        "/api/v1/groups/demo/throttle",
        json={"redis_url": "redis://u:p@host:6379", "ip_per_min": 20, "token_per_min": 15},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["scope_ip_per_min"] == 20 and body["scope_token_per_min"] == 15 and "redis_scope_url" not in body

    assert "redis_scope_url" not in demo.get("/api/v1/groups/demo").json()


async def test_scope_throttle_redis_url_is_encrypted_at_rest(demo):
    demo.put("/api/v1/groups/demo/throttle", json={"redis_url": "redis://u:p@host:6379"})
    raw = await demo.app.state.store.inner.get("groups", "demo")
    assert raw["redis_scope_url"] != "redis://u:p@host:6379" and raw["redis_scope_url"].startswith("enc:")


def test_throttle_routes_need_group_admin(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    from fastapi.testclient import TestClient

    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put("/api/v1/groups/demo/zones/zone-a/item-throttle", json={"ip_per_min": 1}).status_code == 403
        assert v.put("/api/v1/groups/demo/throttle", json={"ip_per_min": 1}).status_code == 403


def test_deploy_passes_throttle_config_to_the_worker(demo):
    demo.put(
        "/api/v1/groups/demo/zones/zone-a/item-throttle",
        json={"redis_url": "redis://item/0", "ip_per_min": 10, "token_per_min": 5},
    )
    demo.put(
        "/api/v1/groups/demo/throttle", json={"redis_url": "redis://scope/0", "ip_per_min": 20, "token_per_min": 15}
    )
    seen = {}

    async def fake_deploy(group, env, zone, canary, config, spec, log, gate=None):
        seen[zone] = config
        return {"ok": True, "workers": []}

    async def fake_sync(group, repo_url, ref, token):
        return "/tmp/fake"

    demo.app.state.cloud.deploy = fake_deploy
    demo.app.state.cloud.sync_repo = fake_sync
    jid = demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"]
    wait_job(demo, jid)

    cfg_a = seen["zone-a"]
    assert cfg_a["RAMEN_REDIS_ITEM_URL"] == "redis://item/0"
    assert cfg_a["RAMEN_THROTTLE_ITEM_IP"] == "10" and cfg_a["RAMEN_THROTTLE_ITEM_TOKEN"] == "5"
    assert cfg_a["RAMEN_REDIS_SCOPE_URL"] == "redis://scope/0"
    assert cfg_a["RAMEN_THROTTLE_SCOPE_IP"] == "20" and cfg_a["RAMEN_THROTTLE_SCOPE_TOKEN"] == "15"

    cfg_b = seen["zone-b"]  # zone-b has no item throttle set: scope still applies, item keys absent
    assert "RAMEN_REDIS_ITEM_URL" not in cfg_b and "RAMEN_THROTTLE_ITEM_IP" not in cfg_b
    assert cfg_b["RAMEN_REDIS_SCOPE_URL"] == "redis://scope/0"
