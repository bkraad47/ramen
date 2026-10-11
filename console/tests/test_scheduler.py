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


async def test_tick_rebalances_every_pair_and_scales_up_the_busy_one(demo, monkeypatch):
    """§21.7: every pair is rebalanced each interval; the busy one scales up (audited by `scheduler`), the quiet one
    holds; only the skewed pair gets a `scheduler.rebalance` line."""
    svc = demo.app.state.services
    await scheduler.set_config(svc.store, enabled=True)
    for z in ("zone-a", "zone-b"):
        w = await svc.worker_config("demo", z)
        await svc.store.put("workers", w["id"], {**w, "max_count": 3})

    async def fake_workers(group, zone):
        load = "high" if zone == "zone-a" else "even"
        return [{"id": f"{zone}-p1", "load": load, "metrics": {"inflight": 30 if load == "high" else 10, "total": 5}}]

    monkeypatch.setattr(svc.cloud, "workers", fake_workers)
    fired = await scheduler.tick(svc)
    assert sorted((f["group"], f["zone"]) for f in fired) == [("demo", "zone-a"), ("demo", "zone-b")]
    by_zone = {f["zone"]: f["result"]["autoscale"] for f in fired}
    assert (
        by_zone["zone-a"]["action"] == "up" and by_zone["zone-a"]["to"] == 2 and by_zone["zone-b"]["action"] == "hold"
    )
    assert (await svc.worker_config("demo", "zone-a"))["count"] == 2

    audit = await svc.store.list("audit")
    skew = [a for a in audit if a["action"] == "scheduler.rebalance"]
    assert len(skew) == 1 and skew[0]["user"] == "scheduler" and "load:high" in skew[0]["tags"]
    scaled = [a for a in audit if a["action"] == "workers.autoscale"]
    assert len(scaled) == 1 and scaled[0]["user"] == "scheduler" and scaled[0]["target"] == "demo/zone-a"
    assert {"action:up", "from:1", "to:2"} <= set(scaled[0]["tags"])


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
