"""CONTRACTS §24.1: sleeping workers — the setting, the zone state, sleep and wake by hand, by the scheduler when
idle, and by the activator's internal call."""

import asyncio
import time

import pytest
from fastapi.testclient import TestClient

from ramen_console import scheduler, sleeping
from ramen_console.grpcclient import Client
from tests.stub_cloud import StubCloud
from tests.test_api import PW, app, client, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import wait_job

URL = "/api/v1/groups/demo/zones/zone-a/workers"
ZONE = "/api/v1/groups/demo/zones/zone-a"
INTERNAL = "/api/v1/internal/zones/demo/zone-a/wake"
KEY = {"x-ramen-admin-key": "adm"}


@pytest.fixture
def cloud(tmp_path, workers):
    return StubCloud(
        tmp_path / "buckets",
        tmp_path / "logs",
        {"demo/zone-a": [workers["cold"].target], "demo/zone-b": [workers["hot"].target]},
        workers["cold"].target,
        "adm",
        Client(deadline=2),
    )


def audit(c, action):
    return [a for a in c.get("/api/v1/audit?limit=300").json() if a["action"] == action]


def state(c):
    return c.get(URL).json().get("state", "awake")


def svc_of(c):
    return c.app.state.services


# the setting ------------------------------------------------------------------------------------------------------


def test_sleep_after_secs_is_zero_or_at_least_a_minute_and_is_audited(demo):
    assert demo.get(URL).json().get("sleep_after_secs", 0) == 0
    for bad in (1, 59, -5):
        assert demo.put(URL, json={"sleep_after_secs": bad}).status_code == 422
    r = demo.put(URL, json={"sleep_after_secs": 120})
    assert r.status_code == 200 and r.json()["sleep_after_secs"] == 120
    assert demo.get(URL).json()["sleep_after_secs"] == 120
    ok = [a for a in audit(demo, "workers.sleep_after") if a["ok"]][0]
    assert {"from:0", "to:120", "zone:zone-a"} <= set(ok["tags"])
    assert any("error:invalid" in a["tags"] for a in audit(demo, "workers.sleep_after") if not a["ok"])
    assert demo.put(URL, json={"sleep_after_secs": 0}).json()["sleep_after_secs"] == 0
    assert demo.put(URL, json={"sleep_after_minutes": 5}).json()["sleep_after_secs"] == 300  # the page's form
    assert demo.put(URL, json={"sleep_after_minutes": "0.5"}).status_code == 422


def test_only_admins_of_the_group_set_it_sleep_and_wake(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.put(URL, json={"sleep_after_secs": 60}).status_code == 200
        assert ga.post(f"{ZONE}/sleep").status_code == 200 and ga.post(f"{ZONE}/wake").status_code == 200
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put(URL, json={"sleep_after_secs": 600}).status_code == 403
        assert v.post(f"{ZONE}/sleep").status_code == 403 and v.post(f"{ZONE}/wake").status_code == 403
        html = v.get("/groups/demo").text
        assert "Sleep after idle" not in html and "Sleep now" not in html
    assert demo.get(URL).json()["sleep_after_secs"] == 60
    refused = [a for a in audit(demo, "workers.sleep") if a["user"] == "v@x"]
    assert refused and not refused[0]["ok"] and "error:forbidden" in refused[0]["tags"]


# by hand ----------------------------------------------------------------------------------------------------------


def test_sleep_and_wake_by_hand_remember_the_count_and_the_canary(demo, cloud):
    demo.put(URL, json={"max_count": 3, "count": 3})
    cloud.runtime = {"workers": 3, "canary": 1, "activator": 0}
    r = demo.post(f"{ZONE}/sleep")
    assert r.status_code == 200 and r.json()["state"] == "asleep"
    assert r.json()["sleep_saved"] == {"count": 3, "canary": 1} and "redis_item_url" not in r.json()
    assert cloud.calls == [("sleep", "demo", "zone-a")] and state(demo) == "asleep"
    a = audit(demo, "workers.sleep")
    assert len(a) == 1 and a[0]["ok"] and {"trigger:manual", "zone:zone-a", "group:demo"} <= set(a[0]["tags"])
    assert demo.post(f"{ZONE}/sleep").json()["state"] == "asleep" and len(cloud.calls) == 1  # already asleep
    r = demo.post(f"{ZONE}/wake")
    assert r.status_code == 200 and r.json()["state"] == "awake" and "sleep_saved" not in r.json()
    assert cloud.calls[-1] == ("wake", "demo", "zone-a", 3, 1) and demo.get(URL).json()["count"] == 3
    a = audit(demo, "workers.wake")
    assert len(a) == 1 and a[0]["ok"] and "trigger:manual" in a[0]["tags"] and a[0]["user"] == "root@ramen.local"
    assert demo.post(f"{ZONE}/wake").json()["state"] == "awake" and len(cloud.calls) == 2  # already awake


def test_a_failed_wake_leaves_the_zone_asleep_and_is_audited(demo, cloud):
    demo.post(f"{ZONE}/sleep")
    cloud.wake_error = RuntimeError("pods not Ready: worker-abc")
    with pytest.raises(RuntimeError):
        demo.post(f"{ZONE}/wake")
    assert state(demo) == "asleep"
    bad = [a for a in audit(demo, "workers.wake") if not a["ok"]]
    assert bad and any("pods not Ready" in t for t in bad[0]["tags"])
    cloud.wake_error = None
    assert demo.post(f"{ZONE}/wake").json()["state"] == "awake"


def test_a_failed_sleep_leaves_the_zone_awake(demo, cloud):
    cloud.sleep_error = RuntimeError("activator not Ready")
    with pytest.raises(RuntimeError):
        demo.post(f"{ZONE}/sleep")
    assert state(demo) == "awake"


# the activator's call ---------------------------------------------------------------------------------------------


def test_the_internal_wake_needs_the_admin_key_and_never_answers_through_an_edge(demo, cloud):
    demo.post(f"{ZONE}/sleep")
    with TestClient(demo.app) as pod:  # no session, no API key: a pod of the zone
        assert pod.post(INTERNAL, json={"trigger": "call"}).status_code == 401
        assert pod.post(INTERNAL, headers={"x-ramen-admin-key": "nope"}).status_code == 401
        r = pod.post(INTERNAL, headers={**KEY, "X-Forwarded-For": "203.0.113.9"}, json={"trigger": "call"})
        assert r.status_code == 404
        assert state(demo) == "asleep" and len(cloud.calls) == 1
        assert pod.post("/api/v1/internal/zones/nope/zone-a/wake", headers=KEY).status_code == 404
        assert pod.post("/api/v1/internal/zones/demo/nope/wake", headers=KEY).status_code == 404
        r = pod.post(INTERNAL, headers=KEY, json={"trigger": "call"})
        assert r.status_code == 200
        assert r.json() == {"group": "demo", "zone": "zone-a", "state": "waking", "started": True}
        assert state(demo) == "awake" and cloud.calls[-1] == ("wake", "demo", "zone-a", 1, 0)
        r = pod.post(INTERNAL, headers=KEY)  # idempotent: awake now, nothing is scaled again
        assert r.json() == {"group": "demo", "zone": "zone-a", "state": "awake", "started": False}
        assert len(cloud.calls) == 2
    a = [x for x in audit(demo, "workers.wake") if "trigger:call" in x["tags"]]
    assert len(a) == 1 and a[0]["ok"] and a[0]["user"] == "activator"


def test_the_internal_wake_is_off_without_a_worker_admin_key(demo, cloud, monkeypatch):
    monkeypatch.delenv("RAMEN_ADMIN_KEY", raising=False)
    cloud.admin_key = ""
    with TestClient(demo.app) as pod:
        assert pod.post(INTERNAL, headers={"x-ramen-admin-key": ""}).status_code == 404


def test_a_failed_wake_from_a_call_is_logged_and_the_next_call_retries(demo, cloud, caplog):
    demo.post(f"{ZONE}/sleep")
    cloud.wake_error = RuntimeError("quota")
    with TestClient(demo.app) as pod:
        assert pod.post(INTERNAL, headers=KEY).json()["started"] is True
        assert state(demo) == "asleep" and any("wake of demo/zone-a failed" in r.message for r in caplog.records)
        cloud.wake_error = None
        assert pod.post(INTERNAL, headers=KEY).json()["started"] is True and state(demo) == "awake"


async def test_concurrent_wakes_scale_once_and_the_second_sees_waking(demo, cloud):
    s = svc_of(demo)
    await s.sleep("demo", "zone-a", "manual")
    cloud.wake_gate = asyncio.Event()
    first = asyncio.create_task(s.wake("demo", "zone-a", "call"))
    for _ in range(20):
        await asyncio.sleep(0)
    w, started = await s.wake_begin("demo", "zone-a")  # the activator's second call while the first still scales
    assert (w["state"], started) == ("waking", False)
    assert (await s.wake("demo", "zone-a", "call"))["state"] == "waking"
    cloud.wake_gate.set()
    assert (await first)["state"] == "awake"
    assert [c[0] for c in cloud.calls] == ["sleep", "wake"]


async def test_the_autoscaler_holds_a_zone_that_is_not_awake(demo, cloud):
    s = svc_of(demo)
    await s.sleep("demo", "zone-a", "manual")
    d = await s.autoscale("demo", "zone-a")
    assert d["action"] == "hold" and d["reason"] == "zone is asleep" and len(cloud.calls) == 1


async def test_a_wake_left_behind_by_a_dead_console_is_started_again(demo, cloud):
    s = svc_of(demo)
    await s.sleep("demo", "zone-a", "manual")
    w = await s.worker_config("demo", "zone-a")
    await s.store.put("workers", w["id"], {**w, "state": "waking", "state_since": time.time() - 3600})
    assert (await s.wake("demo", "zone-a", "call"))["state"] == "awake" and cloud.calls[-1][0] == "wake"


# idle -------------------------------------------------------------------------------------------------------------


def pod(now, ago=600, **m):
    return {"id": "p1", "load": "low", "metrics": {"last_call_at": now - ago, "inflight": 0, "tasks": 0, **m}}


def test_idle_is_every_pod_quiet_for_the_whole_period():
    now = 10_000.0
    assert sleeping.idle([pod(now), pod(now, subscriptions=0)], 300, now) == (True, "")
    assert sleeping.idle([pod(now, ago=now)], 300, now)[0]  # last_call_at 0: never called
    for live, why in [
        ([pod(now), pod(now, ago=10)], "called 10s ago"),
        ([pod(now, inflight=1)], "inflight 1"),
        ([pod(now, tasks=2)], "tasks 2"),
        ([pod(now, subscriptions=1)], "subscriptions 1"),
        ([{"id": "p1", "load": "low", "metrics": {"inflight": 0}}], "no last_call_at"),  # a 0.8 worker
        ([{"id": "p1", "load": "down", "metrics": {}, "error": "x"}], "no metrics"),
        ([{"id": "a", "load": "low", "metrics": {"asleep": True, "last_call_at": 0, "woken": 1}}], "already asleep"),
        ([], "no workers"),
    ]:
        ok, got = sleeping.idle(live, 300, now)
        assert not ok and why in got, (live, got)
    assert sleeping.idle([pod(now)], 300, now, since=now - 60) == (False, "woken or deployed too recently")
    assert sleeping.idle([pod(now)], 0, now) == (False, "sleeping is off")


async def test_the_scheduler_puts_an_idle_zone_to_sleep_and_then_leaves_it_alone(demo, cloud, monkeypatch):
    s = svc_of(demo)
    demo.put(URL, json={"sleep_after_secs": 300})
    now = time.time()
    live = {"ago": 10}
    seen = []

    async def workers(group, zone):
        seen.append(zone)
        return [pod(now, ago=live["ago"])]

    monkeypatch.setattr(cloud, "workers", workers)
    assert await scheduler.sleep_tick(s, now) == [] and state(demo) == "awake"  # called 10 s ago
    live["ago"] = 900
    assert await scheduler.sleep_tick(s, now) == [{"group": "demo", "zone": "zone-a"}]
    assert state(demo) == "asleep" and cloud.calls == [("sleep", "demo", "zone-a")]
    a = audit(demo, "workers.sleep")[0]
    assert a["user"] == "scheduler" and "trigger:idle" in a["tags"]
    seen.clear()
    assert await scheduler.sleep_tick(s, now) == [] and seen == []  # asleep: not even read
    await scheduler.set_config(s.store, enabled=True)
    fired = await scheduler.tick(s)  # the rebalance scan skips the sleeping zone, so it is not woken
    assert [f["zone"] for f in fired] == ["zone-b"] and "zone-a" not in seen and state(demo) == "asleep"


async def test_a_zone_woken_or_deployed_recently_is_not_idle_yet(demo, cloud, monkeypatch):
    s = svc_of(demo)
    demo.put(URL, json={"sleep_after_secs": 300})
    now = time.time()

    async def workers(group, zone):
        return [pod(now, ago=900)]

    monkeypatch.setattr(cloud, "workers", workers)
    await s.touch_zone("demo", "zone-a")  # what a deploy does
    assert await scheduler.sleep_tick(s, now) == [] and state(demo) == "awake"
    assert await scheduler.sleep_tick(s, now + 400) == [{"group": "demo", "zone": "zone-a"}]


async def test_one_unreadable_zone_does_not_stop_the_idle_scan(demo, cloud, monkeypatch, caplog):
    s = svc_of(demo)
    demo.put(URL, json={"sleep_after_secs": 60})

    async def dead(group, zone):
        raise RuntimeError("kube API down")

    monkeypatch.setattr(cloud, "workers", dead)
    assert await scheduler.sleep_tick(s) == [] and any("idle check" in r.message for r in caplog.records)


# what wakes a sleeping zone ---------------------------------------------------------------------------------------


def test_a_count_change_and_a_rebalance_wake_the_zone_first_other_settings_do_not(demo, cloud):
    demo.put(URL, json={"max_count": 4})
    demo.post(f"{ZONE}/sleep")
    r = demo.put(URL, json={"sleep_after_secs": 600, "max_count": 5})
    assert r.status_code == 200 and "applied when it wakes" in r.json()["cloud"]["note"] and state(demo) == "asleep"
    r = demo.put(URL, json={"count": 2})
    assert r.status_code == 200 and r.json()["state"] == "awake" and r.json()["count"] == 2
    assert cloud.calls[-1] == ("wake", "demo", "zone-a", 2, 0)
    assert any("trigger:manual" in a["tags"] and a["user"] == "root@ramen.local" for a in audit(demo, "workers.wake"))
    demo.post(f"{ZONE}/sleep")
    assert demo.post(f"{ZONE}/rebalance").status_code == 200 and state(demo) == "awake"
    assert cloud.calls[-1][0] == "wake"


def test_a_deploy_wakes_a_sleeping_zone_first(demo, cloud, monkeypatch):
    async def sync(group, repo_url, ref, token):
        return "/tmp/fake"

    monkeypatch.setattr(cloud, "sync_repo", sync)
    demo.put(URL, json={"sleep_after_secs": 600})
    demo.post(f"{ZONE}/sleep")
    job = wait_job(
        demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"zone": "zone-a"}).json()["id"]
    )
    assert job["status"] == "ok", job
    assert [c[0] for c in cloud.calls] == ["sleep", "wake"] and state(demo) == "awake"
    assert any("asleep, waking it before the deploy" in ln for ln in job["log"])
    a = [x for x in audit(demo, "workers.wake") if "trigger:deploy" in x["tags"]]
    assert a and a[0]["user"] == "root@ramen.local"
    assert time.time() - demo.get(URL).json()["state_since"] < 30  # the deploy counts as activity


# pages ------------------------------------------------------------------------------------------------------------


def test_the_group_page_has_the_control_in_minutes_and_the_table_shows_the_state(demo):
    part = demo.get("/ui/groups/demo/zones/zone-a/workers").text
    assert ">awake<" not in part and ">asleep<" not in part  # a zone that never sleeps renders as before
    demo.put(URL, json={"sleep_after_secs": 90})
    html = demo.get("/groups/demo").text
    assert "Sleep after idle (minutes, 0 = never)" in html and 'name="sleep_after_minutes"' in html
    assert 'value="1.5"' in html and "Sleep now" in html and "Wake now" not in html
    assert ">awake</span>" in demo.get("/ui/groups/demo/zones/zone-a/workers").text
    demo.post(f"{ZONE}/sleep")
    assert ">asleep</span>" in demo.get("/ui/groups/demo/zones/zone-a/workers").text
    html = demo.get("/groups/demo").text
    assert "Wake now" in html and f'hx-post="{ZONE}/wake"' in html
