"""U21 first half, as of 0.7.5 (CONTRACTS §21.7, D55): the console — not a cloud HPA — scales a zone's workers up to
its `max_count` under real MCP load when Rebalance runs, back down to 1 once the load stops, and leaves the other
zone alone. Every scale step is audited `workers.autoscale`.

Load is real `tools/call demo_calculator_tool` over `ramen.v1.Mcp/Call`. deploy/kind/values.yaml shortens the
cooldown (10 s) and lowers the call-rate target so a laptop can trip a scale-up.
"""

import pytest

from ramen_tests import env as E
from ramen_tests import kube

from .conftest import audit_entries, rebalance_until

pytestmark = pytest.mark.kind

UP_TIMEOUT = float(E.env("RAMEN_KIND_SCALE_UP_TIMEOUT", "300"))
DOWN_TIMEOUT = float(E.env("RAMEN_KIND_SCALE_DOWN_TIMEOUT", "300"))
MAX = int(E.env("RAMEN_KIND_MAX_WORKERS", "3"))


def test_no_zone_has_a_horizontal_pod_autoscaler(kube_ready, stack, group):
    """0.7.5 dropped the HPA on every cloud: an HPA would fight the console over `replicas`."""
    for zone in stack["zones"]:
        assert kube.hpa_names(kube.ns_name(group, zone)) == [], f"{zone} still has an HPA"


def test_max_count_is_set_by_admins_and_refused_to_viewers(admin, stack, group, suffix, bounds):
    from ramen_tests.console import Console

    zone = stack["zones"][0]
    w = bounds(zone, MAX)
    assert w["max_count"] == MAX and 1 <= w["count"] <= MAX
    r = admin.put("workers", {"count": MAX + 1}, group=group, zone=zone)
    assert r.status_code == 422, f"a count above max_count must be refused: {r.status_code} {r.text[:200]}"
    assert admin.put("workers", {"max_count": 0}, group=group, zone=zone).status_code == 422  # min is always 1

    email, pw = f"autoscale-viewer-{suffix}@ramen.local", "Kind-Autoscale-1!"
    assert admin.create_user(email, pw, "viewer", [group]).status_code in (201, 409)
    with Console(E.require("RAMEN_CONSOLE_URL")) as viewer:
        assert viewer.login(email, pw).status_code in (200, 303)
        r = viewer.put("workers", {"max_count": MAX + 5}, group=group, zone=zone)
        assert r.status_code == 403, f"a viewer changed max_count: {r.status_code} {r.text[:200]}"
        page = viewer.page(f"/groups/{group}").text
        assert 'name="max_count"' not in page, "a viewer can see the autoscale max control"
    assert admin.get("workers", group=group, zone=zone).json()["max_count"] == MAX
    # refused before the handler, so the entry carries the request line, the viewer and the reason (§21.7 audit)
    refused = [e for e in audit_entries(admin) if e.get("user") == email and "/workers" in str(e.get("target", ""))]
    assert refused and all(e.get("ok") is False for e in refused), refused[-3:]
    assert any("error:forbidden" in e.get("tags", []) for e in refused), refused[-3:]


def test_rebalance_scales_the_loaded_zone_up_to_max_and_back_down_to_one(admin, kube_ready, stack, group, load, bounds):
    hot, cold = stack["zones"][0], stack["zones"][1]
    hot_ns, cold_ns = kube.ns_name(group, hot), kube.ns_name(group, cold)
    bounds(hot, MAX)
    bounds(cold, MAX)
    assert admin.put("workers", {"count": 1}, group=group, zone=hot).status_code == 200
    start_cold = kube.replicas(cold_ns)[0]

    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "16"))) as gen:
        ups = rebalance_until(admin, group, hot, lambda: kube.replicas(hot_ns)[0] >= MAX, UP_TIMEOUT)
        calls = gen.total
    assert calls > 0, "no load was generated"
    assert kube.replicas(hot_ns)[0] == MAX, f"{hot_ns} went past max_count {MAX}"
    assert [d["action"] for d in ups].count("up") == MAX - 1, ups
    assert kube.replicas(cold_ns)[0] == start_cold, f"{cold_ns} moved while only {hot} was loaded"

    downs = rebalance_until(admin, group, hot, lambda: kube.replicas(hot_ns)[0] == 1, DOWN_TIMEOUT)
    assert [d["action"] for d in downs].count("down") == MAX - 1, downs
    assert admin.get("workers", group=group, zone=hot).json()["count"] == 1

    steps = [e for e in audit_entries(admin) if e.get("action") == "workers.autoscale" and hot in e.get("target", "")]
    tags = [t for e in steps for t in e.get("tags", [])]
    assert "action:up" in tags and "action:down" in tags, f"autoscale steps not audited: {steps[-6:]}"
    assert any(t.startswith("reason:") for t in tags) and any(t == f"to:{MAX}" for t in tags), tags[-12:]


def test_a_max_of_one_never_scales(admin, kube_ready, stack, group, load, bounds):
    hot = stack["zones"][0]
    ns = kube.ns_name(group, hot)
    bounds(hot, 1)
    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "16"))):
        for _ in range(6):
            body = admin.post("rebalance", {}, group=group, zone=hot).json()
            assert body["autoscale"]["action"] == "hold", body["autoscale"]
    assert kube.replicas(ns)[0] == 1
