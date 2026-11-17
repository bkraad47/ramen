"""CONTRACTS §21.7: the pure scaling decision."""

import pytest

from ramen_console import autoscale as a


def wk(name, load="low", inflight=0, total=None, max_inflight=32, p50=None, n=None):
    m = {"inflight": inflight, "max_inflight": max_inflight}
    if total is not None:
        m["total"] = total
    if p50 is not None:  # §35.1: a 1.0.0c node's latency fields (n = samples in the last 60 s)
        m.update(latency_p50_ms=p50, latency_p95_ms=p50 * 2, latency_n=60 * 5 if n is None else n)
    return {"id": name, "load": load, "metrics": m if load != "down" else {}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("RAMEN_AUTOSCALE_TARGET_RPS", "50")
    for k in ("RAMEN_AUTOSCALE_COOLDOWN_SECS", "RAMEN_AUTOSCALE_P50_MS", "RAMEN_AUTOSCALE_MIN_RPS"):
        monkeypatch.delenv(k, raising=False)


def test_bounds_clamp_count_and_max():
    assert a.bounds({"count": 3}) == (3, 3)  # max defaults to count
    assert a.bounds({"count": 5, "max_count": 2}) == (2, 2)
    assert a.bounds({"count": 0, "max_count": 0}) == (1, 1)
    assert a.bounds({}) == (1, 1)


def test_cooldown_and_target_come_from_the_environment(monkeypatch):
    monkeypatch.setenv("RAMEN_AUTOSCALE_COOLDOWN_SECS", "7")
    monkeypatch.setenv("RAMEN_AUTOSCALE_TARGET_RPS", "9.5")
    assert a.cooldown_secs() == 7 and a.target_rps() == 9.5


def test_a_high_worker_scales_up_by_one():
    d, st = a.decide([wk("p1", "high", 30), wk("p2")], {}, {"count": 2, "max_count": 4}, now=1000, cooldown=120)
    assert d["action"] == "up" and d["from"] == 2 and d["to"] == 3 and "high" in d["reason"]
    assert st["last_change"] == 1000 and st["low_streak"] == 0


def test_mean_inflight_at_half_scales_up():
    d, _ = a.decide([wk("p1", "even", 16), wk("p2", "even", 16)], {}, {"count": 2, "max_count": 3}, 1000, 120)
    assert d["action"] == "up" and "mean inflight" in d["reason"]


def test_older_workers_without_max_inflight_count_against_32():
    w = {"id": "p1", "load": "even", "metrics": {"inflight": 16}}
    d, _ = a.decide([w], {}, {"count": 1, "max_count": 2}, 1000, 120)
    assert d["action"] == "up" and d["mean_pct"] == 50.0


def test_the_call_rate_per_worker_scales_up():
    st = {"totals": {"p1": [0, 990]}}
    d, nxt = a.decide([wk("p1", "low", 0, total=600)], st, {"count": 1, "max_count": 3}, 1000, 120)
    assert d["action"] == "up" and d["rate"] == 60.0 and "calls/s" in d["reason"]
    assert nxt["totals"]["p1"] == [600, 1000]


def test_at_max_count_holds_and_says_so():
    d, _ = a.decide([wk("p1", "high", 30)], {}, {"count": 3, "max_count": 3}, 1000, 120)
    assert d["action"] == "hold" and d["to"] == 3 and "at max_count 3" in d["reason"]


def test_max_one_never_scales():
    for load, inflight in (("high", 30), ("low", 0)):
        d, _ = a.decide([wk("p1", load, inflight)], {}, {"count": 1, "max_count": 1}, 1000, 120)
        assert d["action"] == "hold" and d["to"] == 1


def test_up_waits_for_the_short_up_cooldown_and_down_for_the_long_one():
    """0.7.5 load tests: up fast (30 s), down carefully (120 s)."""
    st = {"last_change": 990}
    d, _ = a.decide([wk("p1", "high", 30)], st, {"count": 1, "max_count": 3}, 1000, 120, 30)
    assert d["action"] == "hold" and "cooling down" in d["reason"]
    d, _ = a.decide([wk("p1", "high", 30)], st, {"count": 1, "max_count": 3}, 1021, 120, 30)
    assert d["action"] == "up"


def test_an_overloaded_zone_scales_up_in_one_proportional_step_within_max():
    """Saturated workers (inflight at the limit, calls refused) need more than one more worker."""
    sat = [wk("p1", "high", 32)]
    d, _ = a.decide(sat, {}, {"count": 1, "max_count": 5}, 1000, 120, 30)
    assert d["action"] == "up" and d["to"] == 2  # 100 % / 50 % → 2 workers' worth
    two = [wk("p1", "high", 32), wk("p2", "high", 32)]
    d, _ = a.decide(two, {}, {"count": 2, "max_count": 3}, 1000, 120, 30)
    assert d["action"] == "up" and d["to"] == 3  # 4 wanted, clamped at max_count
    mild = [wk("p1", "high", 27)]  # 84 %: one more is enough
    d, _ = a.decide(mild, {}, {"count": 1, "max_count": 5}, 1000, 120, 30)
    assert d["to"] == 2


def test_down_needs_three_consecutive_low_rebalances_with_a_known_rate():
    w = {"count": 3, "max_count": 3}
    pods = lambda t: [wk(f"p{i}", "low", 0, total=t) for i in range(3)]  # noqa: E731
    st = {}
    d, st = a.decide(pods(0), st, w, 1000, 120)
    assert d["action"] == "hold" and d["reason"] == "rate not known yet"
    for i, want in ((1, "low 1/3"), (2, "low 2/3")):
        d, st = a.decide(pods(i), st, w, 1000 + 30 * i, 120)
        assert d["action"] == "hold" and d["reason"] == want
    d, st = a.decide(pods(3), st, w, 1090, 120)
    assert d["action"] == "down" and d["to"] == 2 and st["low_streak"] == 0


def test_an_even_or_high_reading_resets_the_low_streak():
    w = {"count": 2, "max_count": 3}
    st = {"low_streak": 2, "totals": {"p1": [0, 990], "p2": [0, 990]}}
    d, st = a.decide([wk("p1", "even", 10, total=1), wk("p2", "low", 0, total=1)], st, w, 1000, 120)
    assert d["action"] == "hold" and st["low_streak"] == 0 and d["reason"] == "load even"


def test_down_holds_when_one_worker_fewer_would_be_busy():
    w = {"count": 2, "max_count": 2}
    st = {"low_streak": 5, "totals": {"p1": [0, 990], "p2": [0, 990]}}  # 2 × 300 calls in 10 s = 60/s, 30/s each
    d, st = a.decide([wk("p1", "low", 0, total=300), wk("p2", "low", 0, total=300)], st, w, 1000, 120)
    assert d["action"] == "hold" and "busy" in d["reason"] and st["low_streak"] == 0


def test_down_waits_for_the_cooldown_but_keeps_the_streak():
    w = {"count": 2, "max_count": 2}
    st = {"low_streak": 2, "last_change": 990, "totals": {"p1": [0, 990], "p2": [0, 990]}}
    d, st = a.decide([wk("p1", "low", 0, total=1), wk("p2", "low", 0, total=1)], st, w, 1000, 120)
    assert d["action"] == "hold" and d["reason"] == "low, cooling down" and st["low_streak"] == 3


def test_never_below_one():
    st = {"low_streak": 9, "totals": {"p1": [0, 990]}}
    d, _ = a.decide([wk("p1", "low", 0, total=0)], st, {"count": 1, "max_count": 3}, 1000, 120)
    assert d["action"] == "hold" and d["reason"] == "at the minimum of 1"


def test_a_lowered_max_clamps_at_once_even_while_cooling():
    d, st = a.decide([wk("p1", "high", 30)], {"last_change": 999}, {"count": 4, "max_count": 2}, 1000, 120)
    assert d["action"] == "down" and d["from"] == 4 and d["to"] == 2 and "lowered" in d["reason"]


def test_a_count_below_one_is_raised():
    d, _ = a.decide([], {}, {"count": 0, "max_count": 2}, 1000, 120)
    assert d["action"] == "up" and d["to"] == 1


def test_down_workers_never_count_as_low():
    w = {"count": 2, "max_count": 2}
    d, st = a.decide([wk("p1", "down"), wk("p2", "down")], {"low_streak": 2}, w, 1000, 120)
    assert d["action"] == "hold" and d["reason"] == "no ready workers" and st["low_streak"] == 0
    # one down, one low: the low one alone decides, and with a known rate it may still count
    st = {"totals": {"p2": [0, 990]}}
    d, _ = a.decide([wk("p1", "down"), wk("p2", "low", 0, total=0)], st, w, 1000, 120)
    assert d["action"] == "hold" and d["reason"] == "low 1/3"


def test_a_restarted_pod_gives_no_rate_this_time():
    st = {"totals": {"p1": [500, 990]}}
    ready, rate, totals = a.observe([wk("p1", "low", 0, total=3)], st, 1000)
    assert rate is None and totals == {"p1": [3, 1000]} and len(ready) == 1


def test_rate_sums_pods_and_ignores_missing_totals():
    st = {"totals": {"p1": [0, 990], "p2": [100, 995]}}
    _, rate, _ = a.observe([wk("p1", "low", 0, total=100), wk("p2", "low", 0, total=150), wk("p3")], st, 1000)
    assert rate == pytest.approx(10.0 + 10.0)


# --- §35.1 (1.0.0c, D83): the p50 latency trigger --------------------------------------------------------------------


def test_latency_bounds_come_from_the_environment(monkeypatch):
    assert a.p50_bound_ms() == 500 and a.min_rps() == 2
    monkeypatch.setenv("RAMEN_AUTOSCALE_P50_MS", "250")
    monkeypatch.setenv("RAMEN_AUTOSCALE_MIN_RPS", "0.5")
    assert a.p50_bound_ms() == 250 and a.min_rps() == 0.5
    assert a.limits() == {"target_rps": 50.0, "p50_ms": 250.0, "min_rps": 0.5}


def test_latency_is_read_from_the_nodes_that_report_it_and_none_from_older_ones():
    assert a.latency([wk("p1"), wk("p2", "down")]) == {"p50_ms": None, "p95_ms": None, "rps": None}
    # the zone p50 is the sample-weighted mean of the pods' p50s; rps = samples of the last minute / 60
    lat = a.latency([wk("p1", p50=100, n=60), wk("p2", p50=400, n=180), wk("p3")])
    assert lat == {"p50_ms": 325.0, "p95_ms": 650.0, "rps": 4.0}
    assert a.latency([wk("p1", p50=100, n=0)]) == {"p50_ms": None, "p95_ms": None, "rps": 0.0}


def test_a_high_p50_with_enough_rps_scales_up_and_names_it():
    d, st = a.decide([wk("p1", "low", 0, p50=812, n=300)], {}, {"count": 1, "max_count": 3}, 1000, 120)
    assert d["action"] == "up" and d["to"] == 2 and d["reason"] == "p50 812 ms ≥ 500"
    assert d["p50_ms"] == 812 and d["rps"] == 5.0 and st["last_change"] == 1000


def test_the_p50_step_is_proportional_within_max():
    d, _ = a.decide([wk("p1", "low", 0, p50=1500, n=300)], {}, {"count": 1, "max_count": 5}, 1000, 120)
    assert d["action"] == "up" and d["to"] == 3  # 1500 / 500 → three workers' worth
    d, _ = a.decide([wk("p1", "low", 0, p50=1500, n=300)], {}, {"count": 1, "max_count": 2}, 1000, 120)
    assert d["to"] == 2


def test_a_high_p50_below_the_minimum_rps_never_scales_up():
    """An idle zone's one slow call a minute is noise, not load."""
    d, st = a.decide([wk("p1", "low", 0, p50=812, n=30)], {}, {"count": 1, "max_count": 3}, 1000, 120)
    assert d["action"] == "hold" and d["reason"] == "p50 812 ms, rps 0.5 < 2: no step up"
    assert st["low_streak"] == 0


def test_a_high_rate_still_scales_up_when_the_p50_is_fine():
    st = {"totals": {"p1": [0, 990]}}
    d, _ = a.decide([wk("p1", "low", 0, total=600, p50=40)], st, {"count": 1, "max_count": 3}, 1000, 120)
    assert d["action"] == "up" and d["reason"] == "60.0 calls/s per worker ≥ 50" and d["p50_ms"] == 40


def test_down_needs_both_a_low_rate_and_a_p50_under_half_the_bound():
    w = {"count": 2, "max_count": 2}
    pods = lambda t, p50: [wk(f"p{i}", "low", 0, total=t, p50=p50, n=120) for i in range(2)]  # noqa: E731
    _, st = a.decide(pods(0, 100), {}, w, 1000, 120)
    d, st = a.decide(pods(1, 100), st, w, 1010, 120)
    assert d["action"] == "hold" and d["reason"] == "p50 ok, rps low 1/3"
    d, st = a.decide(pods(2, 300), st, w, 1020, 120)  # p50 over half the bound: not low, the streak restarts
    assert d["action"] == "hold" and d["reason"] == "p50 300 ms ≥ 250, not low" and st["low_streak"] == 0
    d, st = a.decide(pods(3, 100), st, w, 1030, 120)
    assert d["reason"] == "p50 ok, rps low 1/3"
    d, st = a.decide(pods(4, 100), st, w, 1040, 120)
    assert d["reason"] == "p50 ok, rps low 2/3"
    d, st = a.decide(pods(5, 100), st, w, 1050, 120)
    assert d["action"] == "down" and d["to"] == 1 and d["reason"] == "p50 ok, low for 3 rebalances"


def test_older_nodes_keep_the_rps_only_decisions():
    st = {"totals": {"p1": [0, 990], "p2": [0, 990]}}
    d, _ = a.decide([wk("p1", total=1), wk("p2", total=1)], st, {"count": 2, "max_count": 2}, 1000, 120)
    assert d["reason"] == "low 1/3" and d["p50_ms"] is None and d["rps"] is None
