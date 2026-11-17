"""CONTRACTS §35 (D80): the four load scenarios through the public edge, both transports, one group key plus one OAuth
token, every gated call carrying the policy `_meta` and a two-event telemetry report. Opt-in: `RAMEN_LOAD=1`.

- `steady`  C=32 for 120 s, `tools/call demo_calculator_tool` → error rate < 0.5 % (429s excluded), p95 < 1.5 s
- `mixed`   C=16 for 90 s: tools/list, prompts/get, resources/read `iris_csv`, tools/call iris_describe, a task round
            trip (slow_report under the tasks extension, polled with tasks/get) → the same bounds
- `burst`   C=256 for 30 s with the group's scope throttle at 1000/min (set through the API, pushed to the live workers
            by re-saving `config/otel`, restored after)                    → 429s seen, no 5xx
- `scale`   8→64 clients over 4 min (+30 s hold) on the zone with max 4 and the scheduler every 20 s → the count grows,
            p95 < 1.5 s, and back to 1 within RAMEN_LOAD_IDLE_SECS (skipped with RAMEN_LOAD_FAST=1); max and scheduler
            restored
Each test appends one markdown row (scenario, clients, duration, requests, rps, errors by code, p50/p95/p99, workers
before→after, Phoenix span delta, verdict + access-log reason counts). Never an LLM-backed path."""

import asyncio
import contextlib
import time
from collections import Counter

import pytest

from ramen_tests import env as E
from ramen_tests import load as L
from ramen_tests.modern import ALL_INPUT, WITH_TASKS

from .conftest import CALC, FAST, GROUP, IDLE_SECS, ZONE, edge, ok

pytestmark = pytest.mark.cloud
PHOENIX = E.env("RAMEN_PHOENIX_URL")
PROJECT = E.env("RAMEN_PHOENIX_PROJECT", "demo")
THROTTLE_LIMIT = 1000  # §35: the burst runs against a 1000/min scope throttle (lowered when the run spends less)
SCALE_MAX = 4
DECISION_WAIT = 180.0  # full mode: how long after the ramp to wait for the scheduler's first autoscale decision
TASK = "task"  # the latency tag of the slow-by-design task messages (left out of the p95, timed on their own)
TASK_CAPS = {**ALL_INPUT, **WITH_TASKS}
DONE = ("completed", "failed", "cancelled")


# -- what every scenario records ---------------------------------------------------------------------------------------
def snapshot(admin) -> dict:
    return {
        "workers": L.workers(admin, GROUP, ZONE),
        "spans": L.phoenix_span_count(PHOENIX, PROJECT),
        "reasons": L.log_reasons(admin, GROUP, ZONE) or Counter(),
    }


def finish(
    admin, report, scenario: str, label: str, stats: L.Stats, before: dict, exclude: set[str] = frozenset(), **scale
) -> None:
    """Row + assertion for a scenario; `exclude` names the latency tags the p95 bound leaves out (reported on their
    own), `scale` carries the extra arguments of the scale verdict (`workers_peak`, `back_to_min`, `dry_run`)."""
    after = snapshot(admin)
    spans = None if before["spans"] is None or after["spans"] is None else after["spans"] - before["spans"]
    grown = after["reasons"] - before["reasons"]
    reasons = ", ".join(f"{k} {v}" for k, v in sorted(grown.items())) or "no new reasons"
    v, misses = L.verdict(
        scenario,
        stats,
        workers_before=before["workers"]["count"],
        workers_after=after["workers"]["count"],
        exclude=exclude,
        **scale,
    )
    w = f"{before['workers']['count']}→{after['workers']['count']}"
    if "workers_peak" in scale:
        w = f"{before['workers']['count']}→{scale['workers_peak']}→{after['workers']['count']}"
    notes = [*misses]
    if exclude:
        notes.append(f"p50/p95/p99 exclude {', '.join(sorted(exclude))}")
    notes.append(f"log reasons: {reasons}")
    report.add(L.markdown_row(scenario, label, stats, w, spans, v, "; ".join(notes), exclude))
    assert v in ("PASS", "DRY RUN"), misses


# -- scenarios -------------------------------------------------------------------------------------------------------
async def test_steady(world, admin, report):
    n, secs = (8, 10) if FAST else (32, 120)
    before = snapshot(admin)
    async with edge(world, n) as (callers, label):
        bodies = L.Bodies(world["policy"])
        stats = await L.run(callers, L.calls(lambda i, k: bodies.body("tools/call", CALC)), secs, L.Stats())
    finish(admin, report, "steady", label, stats, before)


async def test_mixed(world, admin, report):
    n, secs = (4, 8) if FAST else (16, 90)
    bodies = L.Bodies(world["policy"])
    question = {"name": "iris_analysis", "arguments": {"question": "which species has the longest petals?"}}

    async def step(caller, i, k, stats):
        kind = (i + k) % 5
        if kind == 0:
            await L.call(caller, bodies.body("tools/list"), stats)
        elif kind == 1:
            await L.call(caller, bodies.body("prompts/get", question), stats)
        elif kind == 2:
            await L.call(caller, bodies.body("resources/read", {"uri": "ramen://demo/iris.csv"}), stats)
        elif kind == 3:
            await L.call(caller, bodies.body("tools/call", {"name": "iris_describe", "arguments": {}}), stats)
        else:  # the task round trip: created under the extension, polled until done (any pod answers a poll).
            # slow_report is slow by design (it is the demo's long-running tool), so its messages carry the tag
            # TASK — left out of the §35 p95 — and the whole round trip is timed on its own
            t0 = time.perf_counter()
            body = bodies.body("tools/call", {"name": "slow_report", "arguments": {"seconds": 1}}, TASK_CAPS)
            code, msg = await L.call(caller, body, stats, tag=TASK)
            result = ((msg or {}).get("result") or {}) if code == "ok" else {}
            task_id = result.get("taskId") if result.get("resultType") == "task" else None
            for _ in range(20):
                if not task_id:
                    break
                await asyncio.sleep(0.25)
                poll_body = bodies.body("tasks/get", {"taskId": task_id}, TASK_CAPS)
                code, msg = await L.call(caller, poll_body, stats, tag=TASK)
                if code != "ok" or ((msg or {}).get("result") or {}).get("status") in DONE:
                    break
            if code == "ok":
                stats.time("task round trip", (time.perf_counter() - t0) * 1000)

    before = snapshot(admin)
    async with edge(world, n) as (callers, label):
        stats = await L.run(callers, step, secs, L.Stats())
    finish(admin, report, "mixed", label, stats, before, exclude={TASK})


def set_throttle(admin, token_per_min: int) -> dict:
    """`PUT /groups/{g}/throttle` writes the group doc; the workers read `RAMEN_THROTTLE_SCOPE_TOKEN` from their
    deploy config, which `PUT /config/otel` re-pushes to every deployed zone (§26.3) — no deploy needed."""
    ok(admin.http.put(f"/api/v1/groups/{GROUP}/throttle", json={"token_per_min": token_per_min}))
    pushed = ok(admin.put("config_otel", {})).json()
    return pushed.get("pushed") or {}


def settled(admin, timeout: float = 240) -> None:
    """The rolled workers are back: every live worker ready and the count stable for two polls (plus a short drain)."""
    seen: list[dict] = []

    def stable() -> bool:
        w = L.workers(admin, GROUP, ZONE)
        seen.append(w)
        return len(seen) >= 2 and all(x["ready"] >= (x["count"] or 1) for x in seen[-2:])

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not stable():
        time.sleep(10)
    time.sleep(5 if FAST else 20)


async def test_burst(world, admin, report):
    """The limit is proportional to what the run will spend: a short calibration measures the rps of these clients,
    `burst_limit` turns the expected requests per token (half the clients use the key, half the token) into the
    throttle — §35's 1000/min in full mode when the burst exceeds it, else half of the expected count (≥ 60)."""
    n, secs, calib = (32, 5, 2) if FAST else (256, 30, 4)
    group = ok(admin.get("group", group=GROUP)).json()
    old = int(group.get("scope_token_per_min") or 0)
    bodies = L.Bodies(world["policy"])
    calc = L.calls(lambda i, k: bodies.body("tools/call", CALC))
    async with edge(world, n) as (callers, _):
        warm = await L.run(callers, calc, calib, L.Stats())
    expected_per_token = warm.rps() * secs / 2
    limit = L.burst_limit(expected_per_token, FAST, THROTTLE_LIMIT)
    preset = 0 < old <= limit  # the operator set a limit at or under ours already (and deployed): nothing to change
    pushed = {}
    if not preset:
        pushed = set_throttle(admin, limit)
        settled(admin)
    try:
        before = snapshot(admin)
        async with edge(world, n) as (callers, label):
            stats = await L.run(callers, calc, secs, L.Stats())
    finally:
        if not preset:
            set_throttle(admin, old)
    label += (
        f"; throttle {old if preset else limit}/min per token (expected ≈ {expected_per_token:.0f} per token"
        f" at {warm.rps():.0f} rps)" + ("" if preset else f", pushed {pushed.get(f'{GROUP}/{ZONE}', '?')}")
    )
    finish(admin, report, "burst", label, stats, before)


async def watch_workers(admin, samples: list[dict], stop: asyncio.Event, every: float) -> None:
    while not stop.is_set():
        samples.append(await asyncio.to_thread(L.workers, admin, GROUP, ZONE))
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), every)


async def test_scale(world, admin, report):
    n0, n1, ramp, hold = (2, 8, 20, 10) if FAST else (8, 64, 240, 30)
    w0 = L.workers(admin, GROUP, ZONE)
    sched = ok(admin.get("config_scheduler")).json()
    ok(admin.put("workers", {"max_count": SCALE_MAX}, group=GROUP, zone=ZONE))
    ok(admin.put("config_scheduler", {"enabled": True, "interval_seconds": 20}))
    samples: list[dict] = []
    back = None
    try:
        before = snapshot(admin)
        async with edge(world, n1) as (callers, label):
            bodies = L.Bodies(world["policy"])
            stop = asyncio.Event()
            watcher = asyncio.create_task(watch_workers(admin, samples, stop, 10))
            stats = await L.run(
                callers,
                L.calls(lambda i, k: bodies.body("tools/call", CALC)),
                ramp + hold,
                L.Stats(),
                starts=L.ramp_offsets(n0, n1, ramp),
            )
            stop.set()
            await watcher
        if not FAST:
            # the scheduler's first decision may land after the ramp (20 s interval, 30 s up cooldown, a pod to start):
            # wait for one — a new decision or a count above the start — for up to 3 min before judging
            deadline = time.monotonic() + DECISION_WAIT
            while time.monotonic() < deadline and not decided(samples, w0):
                await asyncio.sleep(10)
                samples.append(await asyncio.to_thread(L.workers, admin, GROUP, ZONE))
            deadline = time.monotonic() + IDLE_SECS
            back = False
            while time.monotonic() < deadline and not back:
                await asyncio.sleep(15)
                samples.append(await asyncio.to_thread(L.workers, admin, GROUP, ZONE))
                back = samples[-1]["count"] == 1
    finally:
        ok(admin.put("workers", {"max_count": w0["max_count"] or w0["count"] or 1}, group=GROUP, zone=ZONE))
        ok(admin.put("config_scheduler", {k: sched[k] for k in ("enabled", "interval_seconds") if k in sched}))
    peak = max((s["count"] or 0 for s in samples), default=0)
    decisions = sorted({s["decision"] for s in samples if s["decision"] and s["decision"] != w0["decision"]})
    label += f"; ramp {n0}→{n1} over {ramp} s + {hold} s; max {SCALE_MAX}; decisions: {' / '.join(decisions) or 'none'}"
    if FAST:
        label += "; RAMEN_LOAD_FAST: too short for an autoscaler decision, errors judged only"
    finish(admin, report, "scale", label, stats, before, workers_peak=peak, back_to_min=back, dry_run=FAST)


def decided(samples: list[dict], w0: dict) -> bool:
    """The autoscaler spoke: a decision other than the one recorded before the run, or a count above the start."""
    return any(
        (s["decision"] and s["decision"] != w0["decision"]) or (s["count"] or 0) > (w0["count"] or 1) for s in samples
    )
