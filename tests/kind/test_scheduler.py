"""N6: proves N1's auto-rebalance scheduler fires on its own against a real cluster — no `POST .../rebalance`
call anywhere in this file. Mirrors test_rebalance.py's HPA-pull-back proof (CONTRACTS §7's replica leg,
which needs no Gateway backend and so runs fully on kind), but the trigger is sustained load plus time, not
a human or a test calling the API.
"""

import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube

pytestmark = pytest.mark.kind


@pytest.fixture
def scheduler_on(admin):
    """Short interval so the test does not sit around for the real default (60s) many times over."""
    before = admin.get("config_scheduler").json()
    r = admin.put("config_scheduler", {"enabled": True, "interval_seconds": 5})
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    yield
    admin.put("config_scheduler", before)  # restore: a kind suite that leaves config changed trips up later runs


def test_scheduler_pulls_a_loaded_zone_back_without_a_manual_rebalance_call(
    admin, kube_ready, stack, group, load, scheduler_on
):
    hot = stack["zones"][0]
    hot_ns = kube.ns_name(group, hot)
    lo, hi = kube.hpa_bounds(hot_ns)
    if hi <= lo:
        pytest.skip(f"HPA {hot_ns}/worker cannot scale (min={lo} max={hi})")

    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "8"))):
        deadline = time.monotonic() + 300
        while kube.replicas(hot_ns)[0] <= lo and time.monotonic() < deadline:
            time.sleep(5)
        grown = kube.replicas(hot_ns)[0]
        assert grown > lo, f"{hot_ns} never scaled above {lo} under load"

        # Load keeps running; the scheduler alone (interval_seconds=5) must now pull the hot zone back to
        # `lo`, the same observable effect a human calling POST .../rebalance would cause.
        deadline = time.monotonic() + 120
        while kube.replicas(hot_ns)[0] > lo and time.monotonic() < deadline:
            time.sleep(3)
    after_hot = kube.replicas(hot_ns)[0]
    assert after_hot == lo, f"{hot_ns} still at {after_hot} after waiting for the scheduler, not {lo}"

    entries = admin.audit().json()
    entries = entries if isinstance(entries, list) else entries.get("items", [])
    assert any(
        e.get("action") == "scheduler.rebalance"
        and e.get("user") == "scheduler"
        and f"{group}/{hot}" in e.get("target", "")
        for e in entries
    ), f"no scheduler.rebalance audit entry for {group}/{hot}: {entries[-10:]}"
