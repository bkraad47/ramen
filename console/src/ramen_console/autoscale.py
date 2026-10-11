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
"""

from __future__ import annotations

import math
import os

MIN_COUNT = 1
DOWN_STREAK = 3
UP_MEAN_PCT = 50.0
DEFAULT_MAX_INFLIGHT = 32


def cooldown_secs() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_COOLDOWN_SECS", "120"))


def up_cooldown_secs() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_UP_COOLDOWN_SECS", "30"))


def target_rps() -> float:
    return float(os.environ.get("RAMEN_AUTOSCALE_TARGET_RPS", "50"))


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
    """→ (decision, next_state). decision = {action: up|down|hold, from, to, reason, rate, mean_pct, at}."""
    state = dict(state or {})
    cooldown = cooldown_secs() if cooldown is None else cooldown
    up_cooldown = up_cooldown_secs() if up_cooldown is None else up_cooldown
    count, hi = bounds(w)
    raw = int(w["count"]) if w.get("count") is not None else MIN_COUNT
    ready, rate, totals = observe(workers, state, now)
    nxt = {**state, "totals": totals}
    mean = sum(_pct(x.get("metrics") or {}) for x in ready) / len(ready) if ready else 0.0
    per_worker = rate / max(len(ready), 1) if rate is not None else None

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
    rps = target_rps()
    up_why = (
        f"{len(hot)} worker(s) high"
        if hot
        else f"mean inflight {mean:.0f} % ≥ {UP_MEAN_PCT:.0f} %"
        if mean >= UP_MEAN_PCT
        else f"{per_worker:.1f} calls/s per worker ≥ {rps:g}"
        if per_worker is not None and per_worker >= rps
        else ""
    )
    if up_why:
        nxt["low_streak"] = 0
        if count >= hi:
            return out("hold", count, f"{up_why}, at max_count {hi}")
        if since < up_cooldown:
            return out("hold", count, f"{up_why}, cooling down")
        pressure = max(mean / UP_MEAN_PCT, (per_worker / rps) if per_worker is not None else 0.0, 1.0)
        want = min(hi, max(count + 1, math.ceil(len(ready) * pressure)))
        return out("up", want, up_why)
    all_low = all(x.get("load") == "low" for x in ready)
    if not all_low or rate is None or count <= MIN_COUNT:
        nxt["low_streak"] = 0
        why = "at the minimum of 1" if count <= MIN_COUNT else ("rate not known yet" if rate is None else "load even")
        return out("hold", count, why)
    n = len(ready)
    projected_pct = mean * n / max(n - 1, 1)
    projected_rps = rate / max(n - 1, 1)
    if projected_pct >= UP_MEAN_PCT or projected_rps >= rps:
        nxt["low_streak"] = 0
        return out("hold", count, "one worker fewer would be busy")
    streak = int(state.get("low_streak") or 0) + 1
    nxt["low_streak"] = streak
    if streak < DOWN_STREAK:
        return out("hold", count, f"low {streak}/{DOWN_STREAK}")
    if cooling:
        return out("hold", count, "low, cooling down")
    return out("down", count - 1, f"low for {DOWN_STREAK} rebalances")
