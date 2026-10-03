"""N1: scheduler/health-check node — config round-trip, tick() skew detection, API gating."""

from fastapi.testclient import TestClient

from ramen_console import scheduler
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures


async def test_config_defaults_and_round_trip(app):
    store = app.state.store
    assert await scheduler.get_config(store) == {"enabled": False, "interval_seconds": 60}
    await scheduler.set_config(store, enabled=True, interval_seconds=30)
    assert await scheduler.get_config(store) == {"enabled": True, "interval_seconds": 30}


async def test_tick_does_nothing_when_disabled(demo):
    svc = demo.app.state.services
    calls = []
    svc.cloud.rebalance = lambda g, z: calls.append((g, z))
    assert await scheduler.tick(svc) == []
    assert calls == []


async def test_tick_rebalances_only_skewed_pairs(demo, monkeypatch):
    svc = demo.app.state.services
    await scheduler.set_config(svc.store, enabled=True)

    async def fake_workers(group, zone):
        return [{"load": "high"}] if zone == "zone-a" else [{"load": "even"}]

    monkeypatch.setattr(svc.cloud, "workers", fake_workers)
    fired = await scheduler.tick(svc)
    assert [(f["group"], f["zone"]) for f in fired] == [("demo", "zone-a")]

    audit = await svc.store.list("audit")
    entries = [a for a in audit if a["action"] == "scheduler.rebalance"]
    assert len(entries) == 1 and entries[0]["tags"] == ["scheduler", "auto"] and entries[0]["user"] == "scheduler"


async def test_tick_skips_pair_when_workers_call_fails(demo, monkeypatch):
    svc = demo.app.state.services
    await scheduler.set_config(svc.store, enabled=True)

    async def boom(group, zone):
        raise RuntimeError("down")

    monkeypatch.setattr(svc.cloud, "workers", boom)
    assert await scheduler.tick(svc) == []


def test_config_api_is_super_admin_gated(demo):
    r = demo.get("/api/v1/config/scheduler")
    assert r.status_code == 200 and r.json() == {"enabled": False, "interval_seconds": 60}
    r = demo.put("/api/v1/config/scheduler", json={"enabled": True, "interval_seconds": 15})
    assert r.status_code == 200 and r.json() == {"enabled": True, "interval_seconds": 15}

    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.get("/api/v1/config/scheduler").status_code == 403
        assert v.put("/api/v1/config/scheduler", json={"enabled": True}).status_code == 403
