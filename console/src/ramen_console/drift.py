"""C12 (0.7.2): behavioural drift — the share of a tool's calls that repeat an identical call (same key, tool and
argument hash) within a window. `repeat_rate` is pure; `report` runs it on a zone's log tail for the API and the
group page and logs the WARNING the digest (N2) carries."""

import logging
from datetime import datetime

from .logview import parse_log

log = logging.getLogger("ramen.drift")
DOC = "drift"  # config/drift = {"warn_pct": 30}
DEFAULT_WARN_PCT = 30
WINDOW_S = 60


def _ts(value) -> float | None:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except ValueError, TypeError:
        return None


def repeat_rate(lines, window_s: int = WINDOW_S) -> dict[str, dict]:
    """`{tool: {"calls", "repeats", "rate"}}` over `tools/call` lines (raw log docs or `parse_log` entries). A
    repeat is a call whose previous identical call — same `key_id`, `name` and `args` hash — is at most
    `window_s` seconds earlier (the edge counts). Lines with no `args` (pre-0.7.2 workers) or no readable `ts`
    count as calls but are never repeats."""
    calls: dict[str, list[tuple[float | None, str | None, str | None]]] = {}
    for ln in lines:
        if not isinstance(ln, dict) or ln.get("method") != "tools/call" or not ln.get("name"):
            continue
        calls.setdefault(ln["name"], []).append((_ts(ln.get("ts")), ln.get("key_id"), ln.get("args")))
    out = {}
    for tool, items in calls.items():
        seen: dict[tuple, float] = {}
        repeats = 0
        for ts, kid, args in sorted((x for x in items if x[0] is not None and x[2] is not None), key=lambda x: x[0]):
            prev = seen.get((kid, args))
            if prev is not None and ts - prev <= window_s:
                repeats += 1
            seen[(kid, args)] = ts
        out[tool] = {"calls": len(items), "repeats": repeats, "rate": repeats / len(items)}
    return out


def over(rates: dict[str, dict], warn_pct: int) -> list[str]:
    """The tools whose repeat rate is at or over `warn_pct` percent (integer arithmetic, no float edge)."""
    return sorted(t for t, r in rates.items() if r["repeats"] * 100 >= warn_pct * r["calls"])


async def threshold(store) -> int:
    doc = await store.get("config", DOC) or {}
    return int(doc.get("warn_pct", DEFAULT_WARN_PCT))


async def report(store, group: str, zone: str, text: str, tail: int) -> dict:
    """What `GET .../zones/{zone}/drift` answers; a tool at or over the threshold logs one `ramen.drift` WARNING."""
    rates = repeat_rate(parse_log(text))
    warn_pct = await threshold(store)
    hot = over(rates, warn_pct)
    for tool in hot:
        r = rates[tool]
        log.warning(
            "drift %s/%s: %s repeated %d of %d calls (%d%% >= %d%%)",
            group,
            zone,
            tool,
            r["repeats"],
            r["calls"],
            round(r["rate"] * 100),
            warn_pct,
        )
    return {"tools": rates, "threshold": warn_pct, "window_s": WINDOW_S, "warn": hot, "tail": tail}
