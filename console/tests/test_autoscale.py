"""CONTRACTS §21.7: the pure scaling decision."""

import pytest

from ramen_console import autoscale as a


def wk(name, load="low", inflight=0, total=None, max_inflight=32):
    m = {"inflight": inflight, "max_inflight": max_inflight}
    if total is not None:
        m["total"] = total
    return {"id": name, "load": load, "metrics": m if load != "down" else {}}


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("RAMEN_AUTOSCALE_TARGET_RPS", "50")
    monkeypatch.delenv("RAMEN_AUTOSCALE_COOLDOWN_SECS", raising=False)


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


def test_up_waits_for_the_cooldown():
    st = {"last_change": 950}
    d, _ = a.decide([wk("p1", "high", 30)], st, {"count": 1, "max_count": 3}, 1000, 120)
    assert d["action"] == "hold" and "cooling down" in d["reason"]
    d, _ = a.decide([wk("p1", "high", 30)], st, {"count": 1, "max_count": 3}, 1071, 120)
    assert d["action"] == "up"


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
