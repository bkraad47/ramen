"""CONTRACTS §21.7 (D55): the console scales a group × zone's stable workers between 1 and `max_count`.

`decide` is pure: the workers' metrics (from `Cloud.workers`), the state kept on the worker doc between rebalances, the
worker doc itself and the clock in, a decision and the next state out. Signals per ready worker (load not `down`):
in-flight calls against the node's limit (`max_inflight`, 32 for workers older than 0.7.5) and calls per second derived
from the node's `total` counter between two rebalances.

- **Up**: any worker `high` (inflight > 80 %), the zone's mean inflight ≥ 50 %, or the call rate per worker at or above
  `RAMEN_AUTOSCALE_TARGET_RPS` (default 50) — and `count < max_count`. The step is proportional (0.7.5 load
  tests): enough workers to bring the busiest signal back to its threshold,
  `ceil(ready × max(mean/50 %, rate per worker/target))`, at least one more, at most `max_count`.
- **Down** by one: every ready worker `low` with a known rate, the projected mean inflight and rate per worker with one
  worker fewer still under the up thresholds, for `DOWN_STREAK` (3) consecutive rebalances — and `count > 1`.
- **Cooldown**: no scale-up within `RAMEN_AUTOSCALE_UP_COOLDOWN_SECS` (30) of the previous change, no scale-down within
  `RAMEN_AUTOSCALE_COOLDOWN_SECS` (120). Up fast, down carefully: a GKE load test needed 309 s to reach 3 with a single
  120 s cooldown, and calls were refused (`inflight`) all that time.
- A `count` above `max_count` (the max was lowered) is clamped down at once, cooldown or not.
- **Latency (§35.1, D83, 1.0.0c)**: a node reports `latency_p50_ms` / `latency_p95_ms` / `latency_n` over its last
  60 s; the zone p50 is the sample-weighted mean of the ready pods' p50s and the window rate is `Σ latency_n / 60`.
  Up also when the zone p50 ≥ `RAMEN_AUTOSCALE_P50_MS` (250; 500 before the 1.0.0 perf pass, §35.1 — a new worker
  takes 1–2.5 min to serve behind a cloud LB, so the zone asks while the first one still has headroom) with at least
  `RAMEN_AUTOSCALE_MIN_RPS` (2) rps in the window (an idle zone's one slow call is noise); down only when the p50 is
  under half the bound too. Older nodes (no latency fields) keep the rps-only rules and strings.
"""

from __future__ import annotations

import math
import os

MIN_COUNT = 1
DOWN_STREAK = 3
UP_MEAN_PCT = 50.0
DEFAULT_MAX_INFLIGHT = 32
LATENCY_WINDOW_SECS = 60


def cooldown_secs() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_COOLDOWN_SECS", "120"))


def up_cooldown_secs() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_UP_COOLDOWN_SECS", "30"))


def target_rps() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_TARGET_RPS", "50"))


def p50_bound_ms() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_P50_MS", "250"))


def min_rps() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_MIN_RPS", "2"))


def limits() -> dict:
    """The three bounds the pages show next to the last decision."""
    return {"target_rps": target_rps(), "p50_ms": p50_bound_ms(), "min_rps": min_rps()}


def latency(workers: list[dict]) -> dict:
    """Zone latency from the pods that report it: `{p50_ms, p95_ms, rps}` — p50/p95 sample-weighted over the ready
    pods, rps the window's samples per second; all None when no ready pod has the fields (pre-1.0.0c nodes)."""
    n = p50 = p95 = 0.0
    seen = False
    for w in workers:
        m = w.get("metrics") or {}
        if w.get("load") in ("down", None) or not isinstance(m.get("latency_n"), int):
            continue
        seen = True
        k = m["latency_n"]
        if k > 0 and isinstance(m.get("latency_p50_ms"), int | float):
            n += k
            p50 += k * float(m["latency_p50_ms"])
            p95 += k * float(m.get("latency_p95_ms") or m["latency_p50_ms"])
    if not seen:
        return {"p50_ms": None, "p95_ms": None, "rps": None}
    if n == 0:
        return {"p50_ms": None, "p95_ms": None, "rps": 0.0}
    return {"p50_ms": round(p50 / n, 1), "p95_ms": round(p95 / n, 1), "rps": round(n / LATENCY_WINDOW_SECS, 2)}


def bounds(w: dict) -> tuple[int, int]:
    """(count, max_count) of a worker doc, clamped: max ≥ 1, count in [1, max]."""
    hi = max(MIN_COUNT, int(w.get("max_count") or w.get("count") or MIN_COUNT))
    return min(max(MIN_COUNT, int(w.get("count") or MIN_COUNT)), hi), hi


def _pct(m: dict) -> float:
    limit = int(m.get("max_inflight") or DEFAULT_MAX_INFLIGHT) or DEFAULT_MAX_INFLIGHT
    return 100.0 * float(m.get("inflight") or 0) / limit


def observe(workers: list[dict], state: dict, now: float) -> tuple[list[dict], float | None, dict]:
    """The ready workers, the zone's calls per second (None until two samples of at least one pod exist) and the
    `totals` to keep. A pod whose counter went backwards (restarted) contributes no rate this time."""
    prev = (state or {}).get("totals") or {}
    ready = [w for w in workers if w.get("load") not in ("down", None)]
    totals, rate, known = {}, 0.0, False
    for w in ready:
        total = (w.get("metrics") or {}).get("total")
        if total is None:
            continue
        totals[w["id"]] = [int(total), now]
        before = prev.get(w["id"])
        if before and int(total) >= before[0] and now > before[1]:
            rate += (int(total) - before[0]) / (now - before[1])
            known = True
    return ready, (rate if known else None), totals


def decide(
    workers: list[dict], state: dict | None, w: dict, now: float, cooldown: float | None = None, up_cooldown=None
) -> tuple:
    """→ (decision, next_state). decision = {action: up|down|hold, from, to, reason, rate, mean_pct, p50_ms, rps, at}.
    `rate` is the calls/s between two rebalances (the totals), `rps` the nodes' own last-minute rate (1.0.0c)."""
    state = dict(state or {})
    cooldown = cooldown_secs() if cooldown is None else cooldown
    up_cooldown = up_cooldown_secs() if up_cooldown is None else up_cooldown
    count, hi = bounds(w)
    raw = int(w["count"]) if w.get("count") is not None else MIN_COUNT
    ready, rate, totals = observe(workers, state, now)
    nxt = {**state, "totals": totals}
    mean = sum(_pct(x.get("metrics") or {}) for x in ready) / len(ready) if ready else 0.0
    per_worker = rate / max(len(ready), 1) if rate is not None else None
    lat = latency(ready)
    p50, win_rps = lat["p50_ms"], lat["rps"]

    def out(action, to, reason):
        if action != "hold":
            nxt["last_change"] = now
            nxt["low_streak"] = 0
        return (
            {
                "action": action,
                "from": raw,
                "to": to,
                "reason": reason,
                "rate": None if rate is None else round(rate, 2),
                "mean_pct": round(mean, 1),
                "p50_ms": p50,
                "rps": win_rps,
                "at": now,
            },
            nxt,
        )

    if raw > hi:
        return out("down", hi, f"max_count lowered to {hi}")
    if raw < MIN_COUNT:
        return out("up", MIN_COUNT, "below the minimum of 1")
    if not ready:
        nxt["low_streak"] = 0
        return out("hold", count, "no ready workers")
    since = now - float(state.get("last_change") or 0)
    cooling = since < cooldown
    hot = [x["id"] for x in ready if x.get("load") == "high"]
    rps, bound, floor = target_rps(), p50_bound_ms(), min_rps()
    slow = p50 is not None and p50 >= bound  # §35.1: the latency trigger, only with real traffic behind it
    p50_pressure = (p50 / bound) if slow and win_rps >= floor else 0.0
    up_why = (
        f"{len(hot)} worker(s) high"
        if hot
        else f"mean inflight {mean:.0f} % ≥ {UP_MEAN_PCT:.0f} %"
        if mean >= UP_MEAN_PCT
        else f"{per_worker:.1f} calls/s per worker ≥ {rps:g}"
        if per_worker is not None and per_worker >= rps
        else f"p50 {p50:g} ms ≥ {bound:g}"
        if p50_pressure
        else ""
    )
    if up_why:
        nxt["low_streak"] = 0
        if count >= hi:
            return out("hold", count, f"{up_why}, at max_count {hi}")
        if since < up_cooldown:
            return out("hold", count, f"{up_why}, cooling down")
        rps_pressure = (per_worker / rps) if per_worker is not None else 0.0
        pressure = max(mean / UP_MEAN_PCT, rps_pressure, p50_pressure, 1.0)
        want = min(hi, max(count + 1, math.ceil(len(ready) * pressure)))
        return out("up", want, up_why)
    if slow:  # high p50 under the minimum rate: noise, and certainly not a reason to step down
        nxt["low_streak"] = 0
        return out("hold", count, f"p50 {p50:g} ms, rps {win_rps:g} < {floor:g}: no step up")
    all_low = all(x.get("load") == "low" for x in ready)
    if not all_low or rate is None or count <= MIN_COUNT:
        nxt["low_streak"] = 0
        why = "at the minimum of 1" if count <= MIN_COUNT else ("rate not known yet" if rate is None else "load even")
        return out("hold", count, why)
    if p50 is not None and p50 >= bound / 2:  # §35.1: down needs the p50 under half the bound as well
        nxt["low_streak"] = 0
        return out("hold", count, f"p50 {p50:g} ms ≥ {bound / 2:g}, not low")
    n = len(ready)
    projected_pct = mean * n / max(n - 1, 1)
    projected_rps = rate / max(n - 1, 1)
    if projected_pct >= UP_MEAN_PCT or projected_rps >= rps:
        nxt["low_streak"] = 0
        return out("hold", count, "one worker fewer would be busy")
    streak = int(state.get("low_streak") or 0) + 1
    nxt["low_streak"] = streak
    tag = "p50 ok, rps low" if p50 is not None else "low"  # older nodes keep the rps-only wording
    if streak < DOWN_STREAK:
        return out("hold", count, f"{tag} {streak}/{DOWN_STREAK}")
    if cooling:
        return out("hold", count, f"{tag}, cooling down")
    return out("down", count - 1, f"{'p50 ok, ' if p50 is not None else ''}low for {DOWN_STREAK} rebalances")
