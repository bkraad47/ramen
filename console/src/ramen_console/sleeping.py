"""§24.1 sleeping workers: the zone state kept on the group × zone worker record, and what "idle" means. Pure.

Record fields: `sleep_after_secs` (0 = never), `state` (`awake` | `asleep` | `waking`; absent = awake), `state_since`
(epoch seconds of the last sleep, wake or deploy), `sleep_saved` (`{"count", "canary"}` while not awake),
`last_sleep` / `last_wake` (`{"trigger", "at"}`).
"""

AWAKE, ASLEEP, WAKING = "awake", "asleep", "waking"
TRIGGERS = ("idle", "manual", "call", "deploy")
MIN_SECS = 60
WAKE_STALE = 300.0  # a `waking` older than this was left behind by a console that died mid-wake: wake again


def state_of(w: dict) -> str:
    return w.get("state") or AWAKE


def idle(live: list[dict], secs: int, now: float, since: float = 0.0) -> tuple[bool, str]:
    """(idle?, why not). Idle = the zone was woken or deployed at least `secs` ago and EVERY worker pod reports
    `last_call_at` at least that old (0 = never called), `inflight` 0, `tasks` 0 and `subscriptions` 0. A pod that
    cannot be read, or one that does not report `last_call_at` (a pre-1.0 worker), keeps the zone awake."""
    if secs <= 0:
        return False, "sleeping is off"
    if now - float(since or 0) < secs:
        return False, "woken or deployed too recently"
    if not live:
        return False, "no workers reported"
    for x in live:
        m = x.get("metrics") or {}
        who = x.get("id", "?")
        if x.get("error") or x.get("load") == "down" or not m:
            return False, f"{who}: no metrics"
        if m.get("asleep"):
            return False, f"{who}: the activator answered, the zone is already asleep"
        last = m.get("last_call_at")
        if not isinstance(last, (int, float)) or isinstance(last, bool):
            return False, f"{who}: no last_call_at"
        if now - last < secs:
            return False, f"{who}: called {int(now - last)}s ago"
        for k in ("inflight", "tasks", "subscriptions"):
            if m.get(k):
                return False, f"{who}: {k} {m[k]}"
    return True, ""
