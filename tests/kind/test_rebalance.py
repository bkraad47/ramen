"""U21 second half on kind: `rebalance` changes the split between the zones.

What rebalance does (CONTRACTS §7): it reads every worker's reported `load`, decides a capacity scaler for the
zone's load-balancer backend (0.5 when any worker is `high`, 1.0 otherwise) and pulls the zone's Deployment back to
its HPA `minReplicas`. On kind there is no Gateway backend service, so the capacity leg answers `applied:false`
with a note — that degradation is itself contractual and asserted here. The replica leg is fully exercised: after a
load burst the two zones sit at different replica counts and rebalance moves the loaded one back, which is the
split changing.
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
        "DEFECT rebalance-502: GcpCloud.rebalance only catches ApiError around the load-balancer leg, so any other "
        "failure from the compute API (no credentials, 401/403, a transient 503) turns the whole call into a 502 and "
        "the HPA re-scale -- which needs no cloud API at all -- never runs. CONTRACTS §7 says this leg returns 200 "
        "with applied:false and a note, never an error."
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


def test_rebalance_pulls_the_loaded_zone_back_to_the_hpa_minimum(admin, kube_ready, stack, group, load):
    hot, cold = stack["zones"][0], stack["zones"][1]
    hot_ns, cold_ns = kube.ns_name(group, hot), kube.ns_name(group, cold)
    lo, hi = kube.hpa_bounds(hot_ns)
    if hi <= lo:
        pytest.skip(f"HPA {hot_ns}/worker cannot scale (min={lo} max={hi})")

    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "8"))):
        deadline = time.monotonic() + 300
        while kube.replicas(hot_ns)[0] <= lo and time.monotonic() < deadline:
            time.sleep(5)
    grown = kube.replicas(hot_ns)[0]
    assert grown > lo, f"{hot_ns} never scaled above {lo}"
    before = (grown, kube.replicas(cold_ns)[0])

    body = _rebalance(admin, group, hot)
    assert body.get("scaled_to") == lo, body
    deadline = time.monotonic() + 120
    while kube.replicas(hot_ns)[0] > lo and time.monotonic() < deadline:
        time.sleep(3)
    after = (kube.replicas(hot_ns)[0], kube.replicas(cold_ns)[0])
    assert after[0] == lo, f"{hot_ns} still at {after[0]} after rebalance"
    assert after != before, f"the split did not change: {before} -> {after}"


def test_scaling_a_zone_changes_the_split_without_the_load_balancer(admin, kube_ready, stack, group):
    """The half of `rebalance` that needs no cloud API: the console writing a zone's replica count.

    It runs through the same renderer and the same Kubernetes client, so it stands on its own as proof that the
    console can change the split between the zones — and it is what `rebalance` loses when the LB leg throws."""
    hot, cold = stack["zones"][0], stack["zones"][1]
    hot_ns, cold_ns = kube.ns_name(group, hot), kube.ns_name(group, cold)
    before = (kube.replicas(hot_ns)[0], kube.replicas(cold_ns)[0])
    r = admin.put("workers", {"count": before[0] + 1}, group=group, zone=hot)
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    deadline = time.monotonic() + 120
    while kube.replicas(hot_ns)[0] != before[0] + 1 and time.monotonic() < deadline:
        time.sleep(3)
    after = (kube.replicas(hot_ns)[0], kube.replicas(cold_ns)[0])
    assert after[0] == before[0] + 1, f"{hot_ns} did not scale: {before} -> {after}"
    assert after[1] == before[1], f"{cold_ns} moved too: {before} -> {after}"
    assert kube.hpa_bounds(hot_ns)[0] == before[0] + 1, "the HPA minimum did not follow the requested count"
    admin.put("workers", {"count": before[0]}, group=group, zone=hot)


def test_rebalance_is_audited(admin, stack, group):
    _rebalance(admin, group, stack["zones"][0])
    r = admin.audit()
    assert r.status_code == 200
    body = r.json()
    entries = body if isinstance(body, list) else body.get("items", [])
    assert any(e.get("action") == "zone.rebalance" or "rebalance" in str(e.get("action", "")) for e in entries), (
        "no rebalance entry in the audit log"
    )
