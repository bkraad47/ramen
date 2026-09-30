"""v0.5.5 I16: autoscale and rebalance are proven under gRPC load (test_autoscale.py, test_rebalance.py) but,
until now, never under Streamable HTTP — even though HTTP and gRPC share the same port and guard functions
(D32) since v0.5.0. One focused test per behavior, not a full duplicate of the gRPC suite.
"""

import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube

pytestmark = pytest.mark.kind


def test_hpa_scales_up_under_http_load(kube_ready, stack, group, load):
    hot = stack["zones"][0]
    hot_ns = kube.ns_name(group, hot)
    lo, hi = kube.hpa_bounds(hot_ns)
    if hi <= lo:
        pytest.skip(f"HPA {hot_ns}/worker cannot scale (min={lo} max={hi})")
    start, _ = kube.replicas(hot_ns)

    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "8")), transport="http") as gen:
        deadline = time.monotonic() + float(E.env("RAMEN_KIND_SCALE_UP_TIMEOUT", "300"))
        grew = None
        while time.monotonic() < deadline:
            n, _ = kube.replicas(hot_ns)
            if n > start:
                grew = n
                break
            time.sleep(5)
        calls = gen.total
    assert calls > 0, "no HTTP load was generated"
    assert grew is not None, f"{hot_ns} never scaled up under HTTP load (stayed at {start})"
    assert grew <= hi, f"{hot_ns} scaled to {grew}, above maxReplicas {hi}"


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
