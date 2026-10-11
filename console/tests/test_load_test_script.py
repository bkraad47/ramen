"""scripts/load_test.py (§21.7): the pure timeline helpers; the script itself runs against a live install."""

import importlib.util
from pathlib import Path

SPEC = importlib.util.spec_from_file_location("load_test", Path(__file__).parents[2] / "scripts" / "load_test.py")
lt = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lt)


def doc(count, live, d=None, mx=3):
    return {"count": count, "max_count": mx, "live": live, "last_autoscale": d}


def test_sample_counts_ready_stable_pods_only_and_formats_the_decision():
    live = [
        {"id": "a", "track": "stable", "load": "high"},
        {"id": "b", "track": "stable", "load": "down"},
        {"id": "c", "track": "canary", "load": "low"},
    ]
    row = lt.sample(doc(2, live, {"action": "up", "from": 1, "to": 2, "reason": "1 worker(s) high"}), 100.0, 112.34)
    assert row["t"] == 12.3 and row["count"] == 2 and row["max"] == 3 and row["pods"] == 1
    assert row["loads"] == ["down", "high", "low"] and row["decision"] == "up 1->2 1 worker(s) high"
    assert lt.sample(doc(1, []), 0, 1)["decision"] == ""


def test_summarize_finds_the_peak_when_max_was_reached_and_when_it_came_back():
    rows = [{"t": t, "count": c} for t, c in ((0, 1), (20, 2), (40, 3), (60, 3), (200, 2), (330, 1))]
    s = lt.summarize(rows, 3)
    assert s == {"peak": 3, "reached_max_at": 40, "back_to_1_at": 330, "final": 1}
    assert lt.summarize([{"t": 0, "count": 1}], 3)["reached_max_at"] is None
    assert lt.summarize([], 3)["final"] is None


def test_timeline_is_one_line_per_row():
    rows = [lt.sample(doc(1, []), 0, 5), lt.sample(doc(2, []), 0, 15)]
    out = lt.timeline(rows)
    assert out.count("\n") == 1 and "count=2" in out
