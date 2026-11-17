"""Unit tests of the load harness's pure parts (CONTRACTS §35): percentile math, outcome codes, the verdicts, the
markdown row, the ramp, access-log reason counts, and the runner against `FakeCaller`. No env needed."""

import json

import pytest

from ramen_tests import load as L

OK = ("ok", {"jsonrpc": "2.0", "id": 1, "result": {"resultType": "complete", "content": []}})
CONSENT_PROMPT = (
    "ok",
    {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"resultType": "input_required", "inputRequests": {L.CONSENT: {}}, "requestState": "rs1"},
    },
)


def stats_of(codes: dict[str, int], latencies: list[float] | None = None, seconds: float = 10.0) -> L.Stats:
    s = L.Stats(started=0.0, finished=seconds)
    n = sum(codes.values())
    lat = latencies or [10.0] * n
    i = 0
    for code, count in codes.items():
        for _ in range(count):
            s.record(lat[i % len(lat)], code)
            i += 1
    return s


def test_percentile_is_nearest_rank():
    values = [float(v) for v in range(1, 101)]
    assert (L.percentile(values, 50), L.percentile(values, 95), L.percentile(values, 99)) == (50, 95, 99)
    assert L.percentile(values, 100) == 100 and L.percentile([7.0], 99) == 7.0
    assert L.percentile([], 95) == 0.0
    assert L.percentile([3.0, 1.0, 2.0], 50) == 2.0  # any order


def test_stats_count_errors_without_the_expected_throttles():
    s = stats_of({"ok": 990, "429": 20, "grpc:RESOURCE_EXHAUSTED": 5, "503": 3, "rpc-32024": 2, "exc:ReadTimeout": 1})
    assert s.requests == 1021 and s.errors() == 6 and s.throttled() == 25 and s.fives() == 3
    assert s.error_rate() == pytest.approx(6 / 1021) and s.rps() == pytest.approx(102.1)
    assert s.errors_text() == "429:20, 503:3, exc:ReadTimeout:1, grpc:RESOURCE_EXHAUSTED:5, rpc-32024:2"
    assert stats_of({"ok": 3}).errors_text() == "none" and L.Stats().error_rate() == 0.0


def test_grpc_server_statuses_count_as_5xx():
    assert L.is_5xx("503") and L.is_5xx("grpc:UNAVAILABLE") and L.is_5xx("grpc:INTERNAL")
    assert not L.is_5xx("429") and not L.is_5xx("grpc:RESOURCE_EXHAUSTED") and not L.is_5xx("rpc-32603")


def test_classify_codes():
    assert L.classify(200, {"result": {}}) == "ok" and L.classify(202, None) == "ok" and L.classify(None, None) == "ok"
    assert L.classify(429, None) == "429" and L.classify(428, {"error": {"code": -32024}}) == "428"
    assert L.classify(200, {"error": {"code": -32024}}) == "rpc-32024" and L.classify(None, {"error": {}}) == "rpcNone"


def test_latency_tags_can_be_left_out_of_the_percentile():
    s = L.Stats()
    for ms in (10.0, 20.0, 30.0):
        s.record(ms, "ok", "tools/call")
    for ms in (2000.0, 2500.0):
        s.record(ms, "ok", "task")
    assert s.requests == 5 and len(s.latencies_ms) == 5 and s.p(95) == 2500.0
    assert s.p(95, {"task"}) == 30.0 and s.p(50, {"task"}) == 20.0 and s.latencies({"task", "tools/call"}) == []
    assert L.verdict("mixed", s) == ("FAIL", ["p95 2500 ms ≥ 1500"])
    assert L.verdict("mixed", s, exclude={"task"}) == ("PASS", [])
    slow = L.Stats()
    slow.record(1600.0, "ok", "tools/list")
    slow.record(9000.0, "ok", "task")
    assert L.verdict("mixed", slow, exclude={"task"}) == ("FAIL", ["p95 1600 ms ≥ 1500 (excluding task)"])
    s.time("task round trip", 1200.0)
    s.time("task round trip", 1400.0)
    assert s.timings_text() == "task round trip p50/p95 1200/1400 ms (2)"
    row = L.markdown_row("mixed", "16", s, "1→1", None, "PASS", "x", exclude={"task"})
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert cells[6] == "20 / 30 / 30" and cells[9] == "PASS — x; task round trip p50/p95 1200/1400 ms (2)"


def test_burst_limit_is_proportional_to_what_the_run_spends():
    assert L.burst_limit(159 / 2, fast=True) == 60  # the FAST shakedown: 159 requests cannot exceed 1000/min
    assert L.burst_limit(400, fast=True) == 200 and L.burst_limit(4000, fast=True) == 2000
    assert L.burst_limit(4000) == 1000 and L.burst_limit(1001) == 1000  # §35's limit once the run exceeds it
    assert L.burst_limit(1000) == 500 and L.burst_limit(700) == 350 and L.burst_limit(0) == 60


def test_scale_dry_run_judges_errors_only():
    s = stats_of({"ok": 150, "429": 3})
    assert L.verdict("scale", s, 1, 1, 1, None, dry_run=True) == ("DRY RUN", [])
    v, misses = L.verdict("scale", stats_of({"ok": 150, "503": 2}), 1, 1, 1, None, dry_run=True)
    assert v == "FAIL" and misses == ["2 errors (503:2)"]
    assert L.verdict("scale", L.Stats(), dry_run=True) == ("FAIL", ["no requests completed"])
    assert L.verdict("scale", s, 1, 1, 1, None) == ("FAIL", ["workers 1→1: no scale-up"])  # full mode unchanged


def test_steady_and_mixed_verdicts():
    good = stats_of({"ok": 1000, "429": 50}, latencies=[100.0] * 95 + [1400.0] * 5)
    assert L.verdict("steady", good) == ("PASS", []) and L.verdict("mixed", good) == ("PASS", [])
    slow = stats_of({"ok": 100}, latencies=[100.0] * 90 + [2000.0] * 10)
    v, misses = L.verdict("steady", slow)
    assert v == "FAIL" and misses == ["p95 2000 ms ≥ 1500"]
    bad = stats_of({"ok": 990, "503": 5, "rpc-32603": 5})
    v, misses = L.verdict("mixed", bad)
    assert v == "FAIL" and misses == ["error rate 1.00% ≥ 0.5 %"]
    assert L.verdict("steady", L.Stats()) == ("FAIL", ["no requests completed"])


def test_burst_verdict_wants_429s_and_no_5xx():
    assert L.verdict("burst", stats_of({"ok": 900, "429": 4000, "rpc-32603": 100})) == ("PASS", [])  # errors not judged
    v, misses = L.verdict("burst", stats_of({"ok": 900, "429": 4000, "503": 1, "grpc:UNAVAILABLE": 2}))
    assert v == "FAIL" and misses == ["3 5xx under burst"]
    v, misses = L.verdict("burst", stats_of({"ok": 5000}))
    assert v == "FAIL" and "no 429" in misses[0]


def test_scale_verdict_needs_a_scale_up_and_the_way_back_down():
    s = stats_of({"ok": 5000})
    assert L.verdict("scale", s, workers_before=1, workers_peak=3, workers_after=1, back_to_min=True) == ("PASS", [])
    assert L.verdict("scale", s, 1, 4, 4, None) == ("PASS", [])  # RAMEN_LOAD_FAST: no idle wait, nothing judged
    v, misses = L.verdict("scale", s, workers_before=1, workers_peak=1, workers_after=1, back_to_min=True)
    assert v == "FAIL" and misses == ["workers 1→1: no scale-up"]
    v, misses = L.verdict("scale", s, workers_before=2, workers_peak=2, workers_after=2, back_to_min=False)
    assert v == "FAIL" and misses == ["workers 2→2: no scale-up", "not back to 1 after idle (count 2)"]
    assert L.verdict("scale", s, None, 2, 1, True) == ("PASS", [])


def test_markdown_row_has_the_ten_columns():
    s = stats_of({"ok": 1200, "429": 12}, latencies=[45.0, 120.0, 300.0], seconds=120.0)
    row = L.markdown_row("steady", "32 (16 http / 16 grpc)", s, "1→1", 2424, "PASS")
    cells = [c.strip() for c in row.strip("|").split("|")]
    assert len(cells) == 10 == len(L.HEADER.splitlines()[0].strip("|").split("|"))
    assert cells[:6] == ["steady", "32 (16 http / 16 grpc)", "120 s", "1212", "10.1", "429:12"]
    assert cells[6] == "120 / 300 / 300" and cells[7:] == ["1→1", "+2424", "PASS"]
    fail = L.markdown_row("scale", "64", s, "1→4", None, "FAIL", "p95 1800 ms ≥ 1500")
    assert fail.endswith("| n/a | FAIL — p95 1800 ms ≥ 1500 |")


def test_ramp_offsets_start_n0_at_once_and_spread_the_rest():
    offsets = L.ramp_offsets(8, 64, 240)
    assert len(offsets) == 64 and offsets[:8] == [0.0] * 8 and offsets[8] == 0.0
    assert offsets == sorted(offsets) and offsets[-1] < 240 and offsets[9] == pytest.approx(240 / 56)
    assert L.ramp_offsets(4, 4, 60) == [0.0] * 4 and L.ramp_offsets(8, 2, 60) == [0.0, 0.0]


def test_access_log_reasons_are_counted_from_json_lines():
    text = "\n".join(
        [
            json.dumps({"method": "tools/call", "reason": "throttled"}),
            json.dumps({"method": "tools/call", "reason": "throttled"}),
            json.dumps({"method": "tools/call", "status": "ok"}),
            json.dumps({"reason": "telemetry_required"}),
            "not json",
            "",
        ]
    )
    assert L.reasons_of(text) == {"throttled": 2, "telemetry_required": 1}
    assert L.reasons_of("") == {}


def test_bodies_carry_meta_report_and_required_policies():
    policy = {
        "telemetry": {"version": "abc123abc123", "required": True},
        "evals": {"version": "e" * 12, "required": True},
        "configs": {"version": "c" * 12, "required": False},
    }
    b = L.Bodies(policy)
    body = b.body("tools/call", {"name": "demo_calculator_tool", "arguments": {}})
    m = body["params"]["_meta"]
    assert m["io.modelcontextprotocol/protocolVersion"] == "2026-07-28" and "elicitation" in str(m)
    report = m[L.TELEMETRY_META]
    assert report["version"] == "abc123abc123" and len(report["events"]) == 2 and isinstance(report["seq"], int)
    assert m["io.ramen/evals"] == {"version": "e" * 12} and "io.ramen/configs" not in m
    listing = b.body("tools/list")
    assert L.TELEMETRY_META not in listing["params"]["_meta"] and listing["id"] != body["id"]
    second = b.body("tools/call", {"name": "x", "arguments": {}})
    assert second["params"]["_meta"][L.TELEMETRY_META]["seq"] > report["seq"]
    assert L.TELEMETRY_META not in L.Bodies(None).body("tools/call", {"name": "x"})["params"]["_meta"]


async def test_run_records_every_message_and_accepts_the_consent_prompt():
    fast = L.FakeCaller([OK, ("429", None)])
    asking = L.FakeCaller([CONSENT_PROMPT, OK], delay=0.01)
    stats = L.Stats()
    bodies = L.Bodies(None)
    await L.run(
        [fast, asking], L.calls(lambda i, k: bodies.body("tools/call", {"name": "t", "arguments": {}})), 0.1, stats
    )
    assert stats.requests == len(fast.sent) + len(asking.sent) > 4
    assert stats.codes["429"] >= 1 and stats.codes["ok"] >= 2 and stats.duration() >= 0.1
    accepted = [b for b in asking.sent if "inputResponses" in b["params"]]
    assert accepted and accepted[0]["params"]["requestState"] == "rs1"
    assert accepted[0]["params"]["inputResponses"] == L.ACCEPT


async def test_run_honours_the_ramp_offsets():
    late = L.FakeCaller([OK])
    early = L.FakeCaller([OK])
    await L.run([early, late], L.calls(lambda i, k: {"jsonrpc": "2.0", "id": k, "method": "ping", "params": {}}),
                0.05, L.Stats(), starts=[0.0, 10.0])  # fmt: skip
    assert early.sent and not late.sent


def test_a_scale_run_whose_final_count_is_back_at_one_is_not_failed_for_the_idle_poll():
    # GCP b27: the idle poll timed out at 2, the final read said 1 — the verdict must not call that "not back to 1"
    from ramen_tests.load import Stats, verdict

    s = Stats()
    for _ in range(50):
        s.record(100.0, "ok")
    v, misses = verdict("scale", s, workers_before=1, workers_peak=3, workers_after=1, back_to_min=False)
    assert v == "PASS", misses
