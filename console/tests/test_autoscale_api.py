"""CONTRACTS §21.7: who may set the bounds, what is refused, and that every action is audited."""

import asyncio

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

URL = "/api/v1/groups/demo/zones/zone-a/workers"


def audit(demo, action):
    return [a for a in demo.get("/api/v1/audit?limit=200").json() if a["action"] == action]


def test_a_super_admin_sets_the_max_and_it_is_audited_with_from_and_to(demo):
    r = demo.put(URL, json={"max_count": 4})
    assert r.status_code == 200 and r.json()["max_count"] == 4 and r.json()["count"] == 1
    a = audit(demo, "workers.max_count")[0]
    assert a["ok"] and a["target"] == "demo/zone-a" and {"from:1", "to:4", "zone:zone-a"} <= set(a["tags"])
    assert demo.get(URL).json()["max_count"] == 4


def test_a_count_outside_one_to_max_is_refused_and_audited_invalid(demo):
    demo.put(URL, json={"max_count": 2})
    assert demo.put(URL, json={"count": 3}).status_code == 422
    assert demo.put(URL, json={"count": 0}).status_code == 422
    assert demo.put(URL, json={"max_count": 0}).status_code == 422
    assert demo.put(URL, json={"count": 2}).status_code == 200
    refused = [a for a in audit(demo, "workers.count") if not a["ok"]]
    assert refused and all("error:invalid" in a["tags"] for a in refused)
    ok = [a for a in audit(demo, "workers.count") if a["ok"]][0]
    assert {"from:1", "to:2"} <= set(ok["tags"])


def test_lowering_the_max_clamps_the_running_count(demo):
    demo.put(URL, json={"max_count": 3, "count": 3})
    r = demo.put(URL, json={"max_count": 1})
    assert r.status_code == 200 and r.json()["count"] == 1 and r.json()["max_count"] == 1


def test_a_group_admin_may_set_the_max_a_viewer_may_not_and_the_refusal_is_audited(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.put(URL, json={"max_count": 3}).status_code == 200
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put(URL, json={"max_count": 9}).status_code == 403
        assert v.post("/api/v1/groups/demo/zones/zone-a/rebalance").status_code == 403
    denied = [a for a in demo.get("/api/v1/audit?limit=200").json() if a["user"] == "v@x" and not a["ok"]]
    assert denied and all("error:forbidden" in a["tags"] for a in denied)
    assert demo.get(URL).json()["max_count"] == 3


def test_a_manual_rebalance_is_audited_by_the_person_and_scales_up_under_load(demo, monkeypatch):
    demo.put(URL, json={"max_count": 2})
    svc = demo.app.state.services

    async def busy(group, zone):
        return [{"id": "p1", "load": "high", "metrics": {"inflight": 30, "max_inflight": 32, "total": 9}}]

    monkeypatch.setattr(svc.cloud, "workers", busy)
    r = demo.post("/api/v1/groups/demo/zones/zone-a/rebalance")
    assert r.status_code == 200 and r.json()["autoscale"]["action"] == "up" and r.json()["autoscale"]["to"] == 2
    assert demo.get(URL).json()["count"] == 2
    assert audit(demo, "workers.rebalance")[0]["user"] == "root@ramen.local"
    up = audit(demo, "workers.autoscale")[0]
    assert up["user"] == "root@ramen.local" and {"action:up", "from:1", "to:2"} <= set(up["tags"])
    # at max now: a hold, which is not audited, but is kept on the worker doc with its reason
    r = demo.post("/api/v1/groups/demo/zones/zone-a/rebalance")
    assert r.json()["autoscale"]["action"] == "hold" and "max_count" in r.json()["autoscale"]["reason"]
    assert len(audit(demo, "workers.autoscale")) == 1
    assert demo.get(URL).json()["last_autoscale"]["action"] == "hold"


def test_metrics_unavailable_holds_without_changing_anything(demo, monkeypatch):
    svc = demo.app.state.services

    async def dead(group, zone):
        raise RuntimeError("worker API down")

    monkeypatch.setattr(svc.cloud, "workers", dead)
    r = demo.post("/api/v1/groups/demo/zones/zone-a/rebalance")
    assert (
        r.status_code == 200
        and r.json()["autoscale"]["action"] == "hold"
        and "no metrics" in r.json()["autoscale"]["reason"]
    )


def test_pinned_at_max_under_load_logs_a_warning(demo, monkeypatch, caplog):
    import asyncio
    import logging

    svc = demo.app.state.services

    async def busy(group, zone):
        return [{"id": "p1", "load": "high", "metrics": {"inflight": 30}}]

    monkeypatch.setattr(svc.cloud, "workers", busy)
    with caplog.at_level(logging.WARNING, logger="ramen.autoscale"):
        d = asyncio.run(svc.autoscale("demo", "zone-a", user="scheduler"))
    assert d["action"] == "hold" and any("pinned at max_count" in r.message for r in caplog.records)


def test_only_admins_see_the_max_control_on_the_group_page_and_in_the_workers_partial(demo):
    demo.put(URL, json={"max_count": 5})
    part = demo.get("/ui/groups/demo/zones/zone-a/workers").text
    assert 'name="max_count"' in part and "max 5" in part
    page = demo.get("/groups/demo").text
    assert 'name="max_count"' in page and "Rebalance and autoscale" in page
    make_user(demo, "v2@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v2@x", PW)
        part = v.get("/ui/groups/demo/zones/zone-a/workers").text
        page = v.get("/groups/demo").text
        assert "max_count" not in part and "max 5" not in part and "Last autoscale" not in part
        assert 'name="max_count"' not in page and "Rebalance and autoscale" not in page
    make_user(demo, "ga2@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga2@x", PW)
        assert 'name="max_count"' in ga.get("/ui/groups/demo/zones/zone-a/workers").text


def test_the_last_decision_is_shown_to_admins(demo, monkeypatch):
    demo.put(URL, json={"max_count": 2})
    svc = demo.app.state.services

    async def busy(group, zone):
        return [{"id": "p1", "load": "high", "metrics": {"inflight": 30}}]

    monkeypatch.setattr(svc.cloud, "workers", busy)
    demo.post("/api/v1/groups/demo/zones/zone-a/rebalance")
    part = demo.get("/ui/groups/demo/zones/zone-a/workers").text
    assert "Last autoscale: <b>up</b> 1→2" in part
    assert "Last decision: <b>up</b> 1→2" in demo.get("/groups/demo").text


def test_a_refused_scaling_attempt_is_audited_under_the_scaling_action(demo):
    """Live load test 0.7.5: refusals were audited as a bare `PUT /api/...`, so the audit filter for workers.* missed
    them. Now they carry the scaling action, the group and zone, ok=false and error:forbidden."""
    from fastapi.testclient import TestClient

    from tests.test_api import PW, login, make_user

    make_user(demo, "v-scale@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v-scale@x", PW)
        h = {"X-Ramen-CSRF": v.cookies.get("ramen_csrf", "")}
        assert v.put("/api/v1/groups/demo/zones/zone-a/workers", json={"max_count": 9}, headers=h).status_code == 403
        assert v.post("/api/v1/groups/demo/zones/zone-a/rebalance", headers=h).status_code == 403
    rows = demo.get("/api/v1/audit?limit=50").json()
    rows = rows if isinstance(rows, list) else rows.get("items", [])
    mine = {e["action"]: e for e in rows if e.get("user") == "v-scale@x" and e["action"].startswith("workers.")}
    assert set(mine) >= {"workers.max_count", "workers.rebalance"}
    for e in mine.values():
        assert "error:forbidden" in e["tags"] and "group:demo" in e["tags"] and "zone:zone-a" in e["tags"]


def test_a_viewer_is_refused_before_the_zone_is_looked_up(demo):
    """CI conformance (security matrix): a viewer scaling an unknown zone got 404, which tells it the zone does not
    exist. The role check comes first, so every viewer attempt is a 403 and still audited under the action."""
    make_user(demo, "v-any@x", "viewer", ["demo"])
    with TestClient(demo.app) as v:
        login(v, "v-any@x", PW)
        assert v.put("/api/v1/groups/demo/zones/any/workers", json={"count": 2}).status_code == 403
        assert v.post("/api/v1/groups/demo/zones/any/rebalance").status_code == 403
    mine = [a for a in audit(demo, "workers.count") if a["user"] == "v-any@x"]
    assert mine and "error:forbidden" in mine[0]["tags"] and "zone:any" in mine[0]["tags"]


def test_concurrent_scheduler_and_button_scale_only_once(demo, monkeypatch):
    """0.8.0 SC4: the scheduler's tick and a Rebalance click on the same zone must not both scale it."""
    demo.put(URL, json={"max_count": 3})
    svc = demo.app.state.services
    scaled = []

    async def busy(group, zone):
        await asyncio.sleep(0.05)  # the metrics round trip, during which the other caller reads the same doc
        return [{"id": "p1", "load": "high", "metrics": {"inflight": 30, "max_inflight": 32, "total": 9}}]

    async def scale(group, zone, spec):
        scaled.append(spec.get("count") if isinstance(spec, dict) else spec)
        return {"ok": True}

    monkeypatch.setattr(svc.cloud, "workers", busy)
    monkeypatch.setattr(svc.cloud, "scale", scale)

    async def both():
        return await asyncio.gather(
            svc.autoscale("demo", "zone-a", user="scheduler"), svc.autoscale("demo", "zone-a", user="root")
        )

    a, b = asyncio.run(both())
    assert sorted(d["action"] for d in (a, b)) == ["hold", "up"], (a, b)
    assert len(scaled) == 1 and demo.get(URL).json()["count"] == 2
    assert len(audit(demo, "workers.autoscale")) == 1
