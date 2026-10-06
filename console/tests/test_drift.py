"""C12 (0.7.2): repeated identical tool calls in a zone's log tail are the behavioural drift signal."""

import json
import logging

import pytest
from fastapi.testclient import TestClient

from ramen_console import drift
from ramen_console.logview import parse_log
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

URL = "/api/v1/config/drift"


def call(ts, name="calc", kid="k1", args="abc123abc123", method="tools/call", **extra):
    doc = {"ts": ts, "msg": "mcp", "method": method, "name": name, "status": "ok", "key_id": kid, **extra}
    if args is not None:
        doc["args"] = args
    return doc


def t(sec: float, day=1) -> str:
    whole, frac = divmod(sec, 1)
    m, s = divmod(int(whole), 60)
    ms = f".{round(frac * 1000):03d}" if frac else ""
    return f"2026-10-0{day}T00:{m:02d}:{s:02d}{ms}Z"


def rate(lines, **kw):
    return drift.repeat_rate(lines, **kw)


def test_no_calls_is_an_empty_map():
    assert rate([]) == {}
    assert rate([call(t(0), method="tools/list"), {"raw": "garbage", "method": None}]) == {}


def test_a_single_call_is_never_a_repeat():
    assert rate([call(t(0))]) == {"calc": {"calls": 1, "repeats": 0, "rate": 0.0}}


def test_same_key_name_and_args_within_the_window_is_a_repeat():
    out = rate([call(t(0)), call(t(10)), call(t(20))])
    assert out == {"calc": {"calls": 3, "repeats": 2, "rate": 2 / 3}}


def test_order_of_the_lines_does_not_matter():
    assert rate([call(t(20)), call(t(0)), call(t(10))]) == rate([call(t(0)), call(t(10)), call(t(20))])


def test_window_edge_is_inclusive_and_one_millisecond_past_it_is_not():
    assert rate([call(t(0)), call(t(60))])["calc"]["repeats"] == 1
    assert rate([call(t(0)), call(t(60.001))])["calc"]["repeats"] == 0
    assert rate([call(t(0)), call(t(30))], window_s=29)["calc"]["repeats"] == 0
    assert rate([call(t(0)), call(t(30))], window_s=30)["calc"]["repeats"] == 1


def test_a_repeat_is_measured_against_the_previous_identical_call_not_the_first():
    # 0 → 50 → 100: each is within 60 s of the one before, so the chain counts two repeats
    assert rate([call(t(0)), call(t(50)), call(t(100))])["calc"]["repeats"] == 2
    # 0 → 70: nothing within the window
    assert rate([call(t(0)), call(t(70))])["calc"]["repeats"] == 0


def test_different_key_name_or_args_are_not_repeats():
    out = rate([call(t(0)), call(t(1), kid="k2"), call(t(2), args="other"), call(t(3), name="sum")])
    assert out == {
        "calc": {"calls": 3, "repeats": 0, "rate": 0.0},
        "sum": {"calls": 1, "repeats": 0, "rate": 0.0},
    }


def test_an_empty_args_hash_is_a_real_value_but_a_missing_one_is_unknown():
    assert rate([call(t(0), args=""), call(t(1), args="")])["calc"]["repeats"] == 1
    # pre-0.7.2 workers log no `args`: those calls count, but they can never be called repeats
    assert rate([call(t(0), args=None), call(t(1), args=None)])["calc"] == {"calls": 2, "repeats": 0, "rate": 0.0}


def test_anonymous_calls_are_grouped_together_and_bad_timestamps_only_count():
    assert rate([call(t(0), kid=None), call(t(1), kid=None)])["calc"]["repeats"] == 1
    out = rate([call("not-a-time"), call(t(0)), call("")])["calc"]
    assert out == {"calls": 3, "repeats": 0, "rate": 0.0}


def test_works_on_parsed_log_entries_too():
    text = "\n".join(json.dumps(call(t(i))) for i in range(3)) + "\nsidecar noise\n"
    assert rate(parse_log(text)) == {"calc": {"calls": 3, "repeats": 2, "rate": 2 / 3}}


def test_tools_over_the_threshold():
    rates = {"a": {"calls": 2, "repeats": 1, "rate": 0.5}, "b": {"calls": 10, "repeats": 2, "rate": 0.2}}
    assert drift.over(rates, 30) == ["a"]
    assert drift.over(rates, 50) == ["a"]
    assert drift.over(rates, 51) == []
    assert drift.over({}, 0) == []


# --- config/drift ------------------------------------------------------------------------------------------------


def test_config_api_round_trips_validates_and_audits(demo):
    assert demo.get(URL).json() == {"warn_pct": 30}
    r = demo.put(URL, json={"warn_pct": 45})
    assert r.status_code == 200 and r.json() == {"warn_pct": 45}
    assert demo.get(URL).json() == {"warn_pct": 45}
    for bad in (-1, 101, "x"):
        assert demo.put(URL, json={"warn_pct": bad}).status_code == 422, bad
    assert demo.put(URL, json={"warn_pct": "0"}).json() == {"warn_pct": 0}  # a form posts strings
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "config.drift"]
    assert any("warn_pct:45" in a["tags"] for a in rows)


def test_only_a_super_admin_reads_or_writes_the_threshold(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get(URL).status_code == 403
        assert ga.put(URL, json={"warn_pct": 10}).status_code == 403


def test_config_page_has_the_threshold_card(demo):
    demo.put(URL, json={"warn_pct": 42})
    page = demo.get("/config").text
    assert "Drift warning threshold" in page
    assert 'hx-put="/api/v1/config/drift"' in page and 'name="warn_pct" value="42"' in page


# --- the zone endpoint and the group page card ---------------------------------------------------------------------


@pytest.fixture
def logfile(demo, tmp_path):
    def write(docs, zone="zone-a"):
        p = tmp_path / "logs" / "demo" / zone / "worker.log"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("\n".join(json.dumps(d) for d in docs) + "\n")
        return p

    return write


def test_zone_drift_runs_on_the_log_tail_and_names_the_threshold(demo, logfile):
    logfile([call(t(0)), call(t(5)), call(t(6), name="sum"), call(t(7), name="sum", args="zzz")])
    r = demo.get("/api/v1/groups/demo/zones/zone-a/drift")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "tools": {
            "calc": {"calls": 2, "repeats": 1, "rate": 0.5},
            "sum": {"calls": 2, "repeats": 0, "rate": 0.0},
        },
        "threshold": 30,
        "window_s": 60,
        "warn": ["calc"],
        "tail": 500,
    }
    assert demo.get("/api/v1/groups/demo/zones/zone-b/drift").json()["tools"] == {}
    assert demo.get("/api/v1/groups/nope/zones/zone-a/drift").status_code in (403, 404)


def test_tail_limits_the_lines_considered(demo, logfile):
    logfile([call(t(0)), call(t(1)), call(t(2), name="sum")])
    out = demo.get("/api/v1/groups/demo/zones/zone-a/drift?tail=1").json()["tools"]
    assert out == {"sum": {"calls": 1, "repeats": 0, "rate": 0.0}}
    assert demo.get("/api/v1/groups/demo/zones/zone-a/drift?tail=0").status_code == 422


def test_a_rate_at_or_over_the_threshold_logs_a_ramen_drift_warning(demo, logfile, caplog):
    logfile([call(t(0)), call(t(5)), call(t(6), name="sum")])
    with caplog.at_level(logging.WARNING, logger="ramen.drift"):
        assert demo.get("/api/v1/groups/demo/zones/zone-a/drift").json()["warn"] == ["calc"]
    warns = [r for r in caplog.records if r.name == "ramen.drift" and r.levelno == logging.WARNING]
    assert len(warns) == 1 and "demo/zone-a" in warns[0].getMessage() and "calc" in warns[0].getMessage()
    assert "50%" in warns[0].getMessage() and "30%" in warns[0].getMessage()
    caplog.clear()
    demo.put(URL, json={"warn_pct": 51})
    with caplog.at_level(logging.WARNING, logger="ramen.drift"):
        assert demo.get("/api/v1/groups/demo/zones/zone-a/drift").json()["warn"] == []
    assert not [r for r in caplog.records if r.name == "ramen.drift"]


def test_viewers_read_drift_but_only_for_their_groups(demo, logfile):
    logfile([call(t(0))])
    make_user(demo, "v@x", "viewer", ["other"])
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.get("/api/v1/groups/demo/zones/zone-a/drift").status_code == 403
        assert v.get("/api/v1/groups/other/zones/zone-a/drift").status_code == 200
        assert v.get("/ui/groups/demo/zones/zone-a/drift").status_code == 403


def test_group_page_loads_a_drift_card_per_zone_over_htmx(demo, logfile):
    page = demo.get("/groups/demo").text
    assert "<h2>Drift</h2>" in page
    for z in ("zone-a", "zone-b"):
        assert f'hx-get="/ui/groups/demo/zones/{z}/drift" data-auto hx-trigger="load, every 60s"' in page
    logfile([call(t(0)), call(t(5)), call(t(6), name="sum")])
    html = demo.get("/ui/groups/demo/zones/zone-a/drift").text
    assert '<span class="badge error">calc</span>' in html and "50%" in html
    assert "<code>sum</code>" in html and "0%" in html
    assert "threshold 30%" in html
    assert "No tool calls" in demo.get("/ui/groups/demo/zones/zone-b/drift").text
