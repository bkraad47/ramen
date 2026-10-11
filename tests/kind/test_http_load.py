"""v0.5.5 I16, as of 0.7.5 (CONTRACTS §21.7): the console's autoscaling is proven under gRPC load (test_autoscale.py,
test_scheduler.py); this is the same scale-up under Streamable HTTP, which shares the node's port and guards (D32).
"""

import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube

from .conftest import rebalance_until

pytestmark = pytest.mark.kind


def test_rebalance_scales_up_under_http_load(admin, kube_ready, stack, group, load, bounds):
    hot = stack["zones"][0]
    hot_ns = kube.ns_name(group, hot)
    bounds(hot, 2)
    assert admin.put("workers", {"count": 1}, group=group, zone=hot).status_code == 200
    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "16")), transport="http") as gen:
        ups = rebalance_until(
            admin, group, hot, lambda: kube.replicas(hot_ns)[0] >= 2, float(E.env("RAMEN_KIND_SCALE_UP_TIMEOUT", "300"))
        )
        calls = gen.total
    assert calls > 0, "no HTTP load was generated"
    assert "up" in [d.get("action") for d in ups], ups
    assert kube.replicas(hot_ns)[0] == 2, "scaled past max_count 2"


def test_rebalance_halves_the_capacity_scaler_under_http_load(admin, stack, group, load):
    hot, cold = stack["zones"][0], stack["zones"][1]
    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "8")), transport="http"):
        deadline = time.monotonic() + 120
        seen = []
        while time.monotonic() < deadline:
            r = admin.post("rebalance", {}, group=group, zone=hot)
            assert r.status_code == 200, f"{hot}: {r.status_code} {r.text[:300]}"
            body = r.json()
            seen.append(body["load"])
            if body["load"] == "high":
                assert body["capacity_scaler"] == 0.5, body
                cold_body = admin.post("rebalance", {}, group=group, zone=cold).json()
                assert cold_body["capacity_scaler"] == 1.0, cold_body
                return
            time.sleep(3)
    raise AssertionError(f"no worker reported load=high under HTTP load; saw {seen}")
