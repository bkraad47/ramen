"""U21 second half on kind: `rebalance` changes the split between the zones.

What rebalance does (CONTRACTS §7, §21.7): it reads every worker's reported `load`, decides a capacity scaler for
the zone's load-balancer backend (0.5 when any worker is `high`, 1.0 otherwise) and, since 0.7.5, runs the console's
autoscaling decision, answered as `autoscale: {action, from, to, reason}`. On kind there is no Gateway backend
service, so the capacity leg answers `applied:false` with a note — that degradation is itself contractual and asserted
here. The scale-up/scale-down proof under load is test_autoscale.py; this file covers the shapes and the manual count.
"""

import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube

pytestmark = pytest.mark.kind


def _rebalance(admin, group, zone) -> dict:
    r = admin.post("rebalance", {}, group=group, zone=zone)
    assert r.status_code == 200, (
        f"{zone}: {r.status_code} {r.text[:300]}\n"
        "DEFECT D-CONSOLE-1 regressed: GcpCloud.rebalance must catch every exception (not just ApiError) around "
        "the load-balancer leg, so a compute-API problem (no credentials, 401/403, a transient 503) degrades to "
        "200 {applied:false, note:...} per CONTRACTS §7, and the autoscaling step below -- which needs no cloud API "
        "at all -- still runs. Fixed and regression-tested at the unit level: "
        "console/tests/test_gcp_cloud.py::test_rebalance_degrades_when_the_compute_api_is_unreachable."
    )
    return r.json()


def test_rebalance_reports_a_capacity_scaler_per_zone(admin, stack, group):
    for zone in stack["zones"]:
        body = _rebalance(admin, group, zone)
        assert body.get("ok") is True, body
        assert body.get("load") in ("low", "even", "high"), body
        assert body.get("capacity_scaler") in (0.5, 1.0), body
        # 0.5 exactly when a worker reports high — the decision the LB would receive
        assert (body["capacity_scaler"] == 0.5) == (body["load"] == "high"), body


def test_on_kind_the_load_balancer_leg_degrades_with_a_note(admin, stack, group):
    """No Gateway controller here, so the capacity change cannot be applied; §7 says that is a 200 with a note,
    never an error. The applied path is proven on GKE (reports/cloud-v0.4.0.md)."""
    body = _rebalance(admin, group, stack["zones"][0])
    assert body.get("applied") is False, body
    assert body.get("note"), f"no note explaining why capacity was not applied: {body}"
    assert body.get("backend_service") is None, body


def test_rebalance_under_load_halves_the_capacity_scaler(admin, stack, group, load):
    """A worker reports `high` when inflight/RAMEN_MAX_INFLIGHT > 80% (§3); rebalance must then pick 0.5."""
    hot = stack["zones"][0]
    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "8"))):
        deadline = time.monotonic() + 120
        seen = []
        while time.monotonic() < deadline:
            body = _rebalance(admin, group, hot)
            seen.append(body["load"])
            if body["load"] == "high":
                assert body["capacity_scaler"] == 0.5, body
                cold = _rebalance(admin, group, stack["zones"][1])
                assert cold["capacity_scaler"] == 1.0, cold
                assert cold["load"] != "high", cold
                return
            time.sleep(3)
    raise AssertionError(f"no worker reported load=high under load; saw {seen}")


def test_rebalance_answers_an_autoscale_decision_within_the_bounds(admin, stack, group):
    for zone in stack["zones"]:
        w = admin.get("workers", group=group, zone=zone).json()
        d = _rebalance(admin, group, zone)["autoscale"]
        assert d["action"] in ("up", "down", "hold") and d.get("reason"), d
        assert 1 <= d["to"] <= max(int(w.get("max_count") or 1), 1), (d, w)
        assert abs(d["to"] - d["from"]) <= 1, f"autoscale moves one worker at a time: {d}"


def test_scaling_a_zone_changes_the_split_without_the_load_balancer(admin, kube_ready, stack, group, bounds):
    """The half of `rebalance` that needs no cloud API: the console writing a zone's replica count.

    It runs through the same renderer and the same Kubernetes client, so it stands on its own as proof that the
    console can change the split between the zones — and it is what `rebalance` loses when the LB leg throws."""
    hot, cold = stack["zones"][0], stack["zones"][1]
    hot_ns, cold_ns = kube.ns_name(group, hot), kube.ns_name(group, cold)
    before = (kube.replicas(hot_ns)[0], kube.replicas(cold_ns)[0])
    bounds(hot, before[0] + 1)  # §21.7: a manual count must sit within [1, max_count]
    r = admin.put("workers", {"count": before[0] + 1}, group=group, zone=hot)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    deadline = time.monotonic() + 120
    while kube.replicas(hot_ns)[0] != before[0] + 1 and time.monotonic() < deadline:
        time.sleep(3)
    after = (kube.replicas(hot_ns)[0], kube.replicas(cold_ns)[0])
    assert after[0] == before[0] + 1, f"{hot_ns} did not scale: {before} -> {after}"
    assert after[1] == before[1], f"{cold_ns} moved too: {before} -> {after}"
    assert kube.hpa_names(hot_ns) == [], "the console scales the zone itself; no HPA may come back"


def test_rebalance_is_audited(admin, stack, group):
    _rebalance(admin, group, stack["zones"][0])
    r = admin.audit()
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("items", [])
    assert any(e.get("action") == "workers.rebalance" and stack["zones"][0] in e.get("target", "") for e in entries), (
        "no workers.rebalance entry in the audit log (§21.7: who pressed Rebalance)"
    )
