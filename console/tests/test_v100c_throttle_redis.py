"""CONTRACTS §38.3 (D85, F3): a worker whose throttle Redis is unreachable says so (`Admin/Metrics.redis`, and the
reload answer the canary gate reads); the zone row's workers partial shows "throttling off: Redis unreachable" and the
deploy log warns. The worker egress policy admits the chart's Redis and the legacy hand-applied one."""

import json

from ramen_console.cloud import gcp_k8s
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures
from tests.test_deploy_gates import deploy, manifest, ws  # noqa: F401 - pytest fixtures

OFF = "throttling off: Redis unreachable"
PART = "/ui/groups/demo/zones/zone-a/workers"


def test_the_egress_policy_admits_the_chart_redis_and_the_legacy_one(monkeypatch):
    monkeypatch.setenv("RAMEN_WORKER_EGRESS", "1")
    monkeypatch.setenv("RAMEN_CLUSTER_SERVICE_CIDR", "10.96.0.0/16")
    np = gcp_k8s.egress_policy("ramen-demo-a", "demo", "a")
    (rule,) = [r for r in np["spec"]["egress"] if r.get("ports") == [{"port": 6379, "protocol": "TCP"}]]
    selectors = [peer["podSelector"]["matchLabels"] for peer in rule["to"]]
    assert selectors == [{"app.kubernetes.io/name": "redis"}, {"app": "redis"}]
    assert all(p["namespaceSelector"] == gcp_k8s.SYSTEM_NS["namespaceSelector"] for p in rule["to"])


def fake_workers(*states):
    async def workers(group, zone):
        return [
            {"id": f"p{i}", "load": "low", "metrics": {"inflight": 0, "total": 1} | ({"redis": s} if s else {})}
            for i, s in enumerate(states)
        ]

    return workers


def test_the_workers_partial_says_throttling_is_off_when_a_worker_cannot_reach_redis(demo, monkeypatch):
    svc = demo.app.state.services
    monkeypatch.setattr(svc.cloud, "workers", fake_workers("ok", "unreachable"))
    part = demo.get(PART).text
    assert part.count(OFF) == 1 and "1 of 2 workers" in part
    assert "p1: low" in part and '<small class="err">redis unreachable</small>' in part
    for states in (("ok", "ok"), ("off",), (None,)):  # reachable, none configured, a pre-1.0.0c worker
        monkeypatch.setattr(svc.cloud, "workers", fake_workers(*states))
        assert OFF not in demo.get(PART).text, states


def test_the_deploy_log_warns_when_the_canary_reports_redis_unreachable(demo, ws):
    ws["cold"].load_result = manifest(redis="unreachable")
    job = deploy(demo)
    assert job["status"] == "ok", job
    warn = [line for line in job["log"] if OFF in line]
    assert len(warn) == 1 and "zone zone-a: warning:" in warn[0], job["log"]
    ws["cold"].load_result = manifest(redis="ok")
    assert not any(OFF in line for line in deploy(demo)["log"])
    assert "redis" not in json.dumps(job.get("result", {}).get("compat") or {})
