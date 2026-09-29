"""U21 first half: an HPA scales a zone's workers up under generated gRPC load and back down afterwards,
and the other zone is untouched while it happens.

Load is real MCP traffic (`tools/call demo_calculator_tool`) over `ramen.v1.Mcp/Call`, not a synthetic CPU burner.
Scale-down is slow by design (the HPA's default stabilisation window is 300s), so the wait is generous.
"""

import time

import pytest

from ramen_tests import env as E
from ramen_tests import kube

pytestmark = pytest.mark.kind

UP_TIMEOUT = float(E.env("RAMEN_KIND_SCALE_UP_TIMEOUT", "300"))
DOWN_TIMEOUT = float(E.env("RAMEN_KIND_SCALE_DOWN_TIMEOUT", "600"))
POLL = 5.0


def _wait(predicate, timeout: float, what: str):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last is not None:
            return last
        time.sleep(POLL)
    raise AssertionError(f"{what} did not happen within {timeout:.0f}s (last seen {last})")


def test_metrics_server_is_serving_cpu(kube_ready, stack, group):
    """Without metrics the HPA reports <unknown> and can never scale; fail loudly rather than time out later."""
    assert kube.metrics_ready(), "kubectl top failed: metrics-server is not serving (deploy/kind/up.sh installs it)"
    ns = kube.ns_name(group, stack["zones"][0])
    h = kube.hpa(ns)
    current = ((h.get("status") or {}).get("currentMetrics") or [None])[0]
    assert current is not None, f"HPA {ns}/worker has no CPU metric yet: {h.get('status')}"


def test_hpa_scales_one_zone_up_under_load_and_leaves_the_other_alone(kube_ready, stack, group, load, nodes):
    hot, cold = stack["zones"][0], stack["zones"][1]
    hot_ns, cold_ns = kube.ns_name(group, hot), kube.ns_name(group, cold)
    hot_min, hot_max = kube.hpa_bounds(hot_ns)
    assert hot_max > hot_min, f"HPA {hot_ns}/worker cannot scale: min={hot_min} max={hot_max}"
    start_hot, _ = kube.replicas(hot_ns)
    start_cold, _ = kube.replicas(cold_ns)

    with load(hot, threads=int(E.env("RAMEN_KIND_LOAD_THREADS", "8"))) as gen:
        grew = _wait(
            lambda: (lambda n: n if n > start_hot else None)(kube.replicas(hot_ns)[0]),
            UP_TIMEOUT,
            f"{hot_ns} scale up from {start_hot}",
        )
        calls = gen.total
    assert calls > 0, "no load was generated"
    assert grew <= hot_max, f"{hot_ns} scaled to {grew}, above maxReplicas {hot_max}"
    assert kube.replicas(cold_ns)[0] == start_cold, (
        f"{cold_ns} changed from {start_cold} to {kube.replicas(cold_ns)[0]} while only {hot} was loaded"
    )
    # the loaded zone kept answering while it scaled
    assert nodes[hot].health() == "SERVING"
    assert nodes[cold].health() == "SERVING"


def test_hpa_scales_back_down_when_the_load_stops(kube_ready, stack, group):
    """Runs after the load test; the HPA's stabilisation window means this is minutes, not seconds."""
    hot = stack["zones"][0]
    ns = kube.ns_name(group, hot)
    lo, _ = kube.hpa_bounds(ns)
    back = _wait(
        lambda: (lambda n: n if n <= lo else None)(kube.replicas(ns)[0]),
        DOWN_TIMEOUT,
        f"{ns} scale back down to {lo}",
    )
    assert back == lo
