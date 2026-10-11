"""N6, as of 0.7.5 (CONTRACTS §21.7): the auto-rebalance scheduler scales a loaded zone up to its `max_count` and,
once the load stops, back down to 1 — with no `POST .../rebalance` anywhere in this file. Every step is audited
`workers.autoscale` under the user `scheduler`.
"""

import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube

from .conftest import audit_entries

pytestmark = pytest.mark.kind

MAX = int(E.env("RAMEN_KIND_MAX_WORKERS", "3"))


@pytest.fixture
def scheduler_on(admin):
    """Short interval so the test does not sit around for the real default (60s) many times over."""
    before = admin.get("config_scheduler").json()
    r = admin.put("config_scheduler", {"enabled": True, "interval_seconds": 5})
    assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"
    yield
    admin.put("config_scheduler", before)  # restore: a kind suite that leaves config changed trips up later runs


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(3)
    raise AssertionError(f"{what} did not happen within {timeout:.0f}s")


def test_the_scheduler_scales_up_to_max_and_back_down_without_a_manual_rebalance(
    admin, kube_ready, stack, group, load, bounds, scheduler_on
):
    hot = stack["zones"][0]
    hot_ns = kube.ns_name(group, hot)
    bounds(hot, MAX)
    assert admin.put("workers", {"count": 1}, group=group, zone=hot).status_code == 200
    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "16"))):
        _wait(lambda: kube.replicas(hot_ns)[0] >= MAX, float(E.env("RAMEN_KIND_SCALE_UP_TIMEOUT", "300")), "scale up")
    assert kube.replicas(hot_ns)[0] == MAX, f"{hot_ns} went past max_count {MAX}"
    _wait(lambda: kube.replicas(hot_ns)[0] == 1, float(E.env("RAMEN_KIND_SCALE_DOWN_TIMEOUT", "300")), "scale down")

    steps = [
        e
        for e in audit_entries(admin)
        if e.get("action") == "workers.autoscale" and e.get("user") == "scheduler" and f"{group}/{hot}" in e["target"]
    ]
    tags = [t for e in steps for t in e.get("tags", [])]
    assert "action:up" in tags and "action:down" in tags, f"scheduler steps not audited: {steps[-6:]}"
