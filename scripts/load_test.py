#!/usr/bin/env python3
"""CONTRACTS §21.7 load test: does the console scale a zone's workers up to its max under load and back to 1 after?

    python scripts/load_test.py <console_url> <admin_pw> <edge host:port> [ca.pem] \\
        --group demo --zone a --max 3 --concurrency 64 --duration 300 --scheduler-interval 20

Signs in as the bootstrap super admin, mints a fresh group key, sets the zone's max, turns the Config page's scheduler
on at the given interval, then runs the scenarios below against the edge over Streamable HTTP (demo_calculator_tool),
polling `GET /api/v1/groups/<g>/zones/<z>/workers` to record the worker count, the live pods and the last autoscale
decision over time. Prints a timeline and PASS/FAIL per scenario and writes `<out>.json` (default load-test-<ts>.json).

Scenarios: `sustained` (load until the zone reaches --max, then idle until it is back at 1), `burst` (a short burst,
then idle: back to 1, never above --max), `max1` (max 1: load never scales), `lower` (load at max, the max is lowered
mid-run: the count follows at once), `viewer` (a viewer's PUT of max_count is refused 403). `--only` picks some.
Needs httpx (the console's venv: `cd console && uv run python ../scripts/load_test.py …`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
import time

import httpx

CALL = {"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": "add"}}


# --- pure helpers (unit-tested) ---------------------------------------------------------------------------------
def sample(workers_doc: dict, t0: float, now: float) -> dict:
    """One timeline row from the workers API document."""
    live = workers_doc.get("live") or []
    d = workers_doc.get("last_autoscale") or {}
    return {
        "t": round(now - t0, 1),
        "count": workers_doc.get("count"),
        "max": workers_doc.get("max_count"),
        "pods": len([x for x in live if x.get("track", "stable") == "stable" and x.get("load") != "down"]),
        "loads": sorted(x.get("load", "?") for x in live),
        "decision": f"{d.get('action')} {d.get('from')}->{d.get('to')} {d.get('reason', '')}".strip() if d else "",
    }


def summarize(rows: list[dict], want_max: int) -> dict:
    """When the count first reached `want_max`, its peak, and when it was last back at 1 after the peak."""
    peak = max((r["count"] or 0 for r in rows), default=0)
    reached = next((r["t"] for r in rows if (r["count"] or 0) >= want_max), None)
    after = [r for r in rows if reached is not None and r["t"] >= reached]
    back = next((r["t"] for r in after if r["count"] == 1), None)
    return {"peak": peak, "reached_max_at": reached, "back_to_1_at": back, "final": rows[-1]["count"] if rows else None}


def timeline(rows: list[dict]) -> str:
    return "\n".join(
        f"{r['t']:>7.1f}s  count={r['count']} max={r['max']} ready_pods={r['pods']} loads={','.join(r['loads'])}"
        f"  {r['decision']}"
        for r in rows
    )


# --- the client ---------------------------------------------------------------------------------------------------
class Ramen:
    def __init__(self, console: str, edge: str, verify, group: str, zone: str):
        self.console, self.edge, self.verify, self.group, self.zone = console.rstrip("/"), edge, verify, group, zone
        self.c = httpx.Client(base_url=self.console, verify=verify, timeout=60, follow_redirects=False)
        self.workers_url = f"/api/v1/groups/{group}/zones/{zone}/workers"

    def login(self, email: str, pw: str) -> int:
        return self.c.post("/login", data={"email": email, "password": pw}).status_code

    def api(self, method: str, path: str, body=None) -> httpx.Response:
        h = {"X-Ramen-CSRF": self.c.cookies.get("ramen_csrf", "")}
        return self.c.request(method, "/api/v1" + path, json=body, headers=h)

    def workers(self) -> dict:
        return self.c.get(self.workers_url).json()

    def set_max(self, n: int) -> httpx.Response:
        return self.api("PUT", f"/groups/{self.group}/zones/{self.zone}/workers", {"max_count": n})


async def load(edge: str, verify, key: str, group: str, zone: str, concurrency: int, seconds: float) -> dict:
    """`concurrency` clients calling the tool back to back for `seconds`; returns call/error counts."""
    url = f"https://{edge}/mcp"
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {key}",
        "ramen-group": group,
        "ramen-zone": zone,
    }
    stats = {"ok": 0, "errors": 0, "status": {}}
    stop = time.monotonic() + seconds

    async def one(i: int):
        limits = httpx.Limits(max_connections=2, max_keepalive_connections=2)
        async with httpx.AsyncClient(verify=verify, timeout=30, limits=limits, http2=False) as c:
            init = {
                "jsonrpc": "2.0",
                "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "load", "version": "1"},
                },
            }
            h = dict(headers)
            try:
                r = await c.post(url, json=init, headers=h)
                if sid := r.headers.get("mcp-session-id"):
                    h["Mcp-Session-Id"] = sid
            except httpx.HTTPError:
                pass
            n = 0
            while time.monotonic() < stop:
                n += 1
                try:
                    r = await c.post(
                        url, json={"jsonrpc": "2.0", "id": n, "method": "tools/call", "params": CALL}, headers=h
                    )
                    stats["status"][r.status_code] = stats["status"].get(r.status_code, 0) + 1
                    if r.status_code == 200 and "error" not in r.text[:200]:
                        stats["ok"] += 1
                    else:
                        stats["errors"] += 1
                except httpx.HTTPError:
                    stats["errors"] += 1

    await asyncio.gather(*(one(i) for i in range(concurrency)))
    return stats


async def watch(r: Ramen, rows: list, t0: float, every: float, until) -> None:
    while not until():
        try:
            rows.append(sample(r.workers(), t0, time.time()))
            print(timeline(rows[-1:]), flush=True)
        except Exception as e:  # noqa: BLE001 - a console blip must not end the test
            print(f"  poll failed: {type(e).__name__}: {e}", flush=True)
        await asyncio.sleep(every)


async def phase(r: Ramen, key: str, rows: list, t0: float, args, seconds: float, concurrency: int) -> dict:
    """Load for `seconds` (0 = idle) while polling."""
    done = False
    w = asyncio.create_task(watch(r, rows, t0, args.poll, lambda: done))
    stats = {"ok": 0, "errors": 0}
    if seconds and concurrency:
        stats = await load(r.edge, r.verify, key, r.group, r.zone, concurrency, seconds)
    done = True
    await w
    return stats


async def idle_until(r: Ramen, rows: list, t0: float, args, cond, timeout: float) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        rows.append(sample(r.workers(), t0, time.time()))
        print(timeline(rows[-1:]), flush=True)
        if cond(rows[-1]):
            return True
        await asyncio.sleep(args.poll)
    return False


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("console")
    ap.add_argument("admin_pw")
    ap.add_argument("edge", help="host:port of the edge (load balancer) serving POST /mcp")
    ap.add_argument("ca", nargs="?", help="PEM to verify the console and the edge (self-signed edges)")
    ap.add_argument("--admin", default="admin@ramen.local")
    ap.add_argument("--group", default="demo")
    ap.add_argument("--zone", default="a")
    ap.add_argument("--max", type=int, default=3)
    ap.add_argument("--concurrency", type=int, default=64)
    ap.add_argument("--duration", type=float, default=300, help="seconds of sustained load at most")
    ap.add_argument("--burst", type=float, default=45, help="seconds of the burst scenario")
    ap.add_argument("--scheduler-interval", type=int, default=20)
    ap.add_argument("--poll", type=float, default=10)
    ap.add_argument("--down-timeout", type=float, default=1200, help="seconds to wait for the zone to be back at 1")
    ap.add_argument("--only", default="sustained,burst,max1,lower,viewer")
    ap.add_argument("--out", default=f"load-test-{int(time.time())}.json")
    a = ap.parse_args()
    verify = a.ca or True
    r = Ramen(a.console, a.edge, verify, a.group, a.zone)
    if r.login(a.admin, a.admin_pw) not in (200, 303):
        print("FAIL: admin sign-in")
        return 2
    key = r.api("POST", f"/groups/{a.group}/mcp-keys", {"name": f"load-{int(time.time())}"}).json().get("key")
    if not key:
        print("FAIL: could not mint a group key")
        return 2
    envs = [
        e
        for e in r.api("GET", "/environments").json()
        if e.get("group") == a.group and a.zone in (e.get("zones") or [])
    ]
    if not envs:
        print(f"FAIL: no environment of {a.group} runs on zone {a.zone}")
        return 2
    job = r.api("POST", f"/groups/{a.group}/environments/{envs[0]['name']}/deploy", {"canary": True}).json().get("id")
    for _ in range(180):  # the new key reaches the workers with the deploy
        time.sleep(5)
        st = r.api("GET", f"/jobs/{job}").json().get("status")
        if st and st != "running":
            break
    print(f"deploy for the new key: {st}", flush=True)
    if st != "ok":
        return 2
    sched = r.api("GET", "/config/scheduler").json()
    r.api("PUT", "/config/scheduler", {"enabled": True, "interval_seconds": a.scheduler_interval})
    want = set(a.only.split(","))
    results, report = [], {"args": vars(a), "scenarios": {}}
    t0 = time.time()
    # 0.7.5: the console must stay reachable the whole time — an autoscaler node scale-down once evicted its only pod
    # and the edge answered "unconditional drop overload" (503) to the UI while workers were scaling down.
    import threading

    console_seen = {"ok": 0, "bad": [], "stop": False}

    def watch_console():
        with httpx.Client(verify=verify, timeout=10) as w:
            while not console_seen["stop"]:
                try:
                    code = w.get(f"{a.console.rstrip('/')}/readyz").status_code
                except httpx.HTTPError as e:
                    code = type(e).__name__
                if code == 200:
                    console_seen["ok"] += 1
                else:
                    console_seen["bad"].append([round(time.time() - t0, 1), code])
                time.sleep(2)

    threading.Thread(target=watch_console, daemon=True).start()

    def record(name, ok, detail, rows=None, extra=None):
        if extra and "load" in extra and not extra["load"].get("ok"):
            ok, detail = False, detail + " — no call succeeded, so nothing was tested"
        results.append((name, ok))
        print(f"{'PASS' if ok else 'FAIL'}: {name} — {detail}", flush=True)
        report["scenarios"][name] = {"ok": ok, "detail": detail, "timeline": rows or [], **(extra or {})}

    try:
        if "viewer" in want:
            email, pw = f"load-viewer-{secrets.token_hex(3)}@ramen.local", "Load-Test-9!" + secrets.token_urlsafe(10)
            u = r.api("POST", "/users", {"email": email, "password": pw, "memberships": {a.group: "viewer"}})
            if u.status_code >= 300:
                record("viewer", False, f"could not create the viewer: {u.status_code} {u.text[:120]}")
            v = Ramen(a.console, a.edge, verify, a.group, a.zone)
            v.login(email, pw)
            code = v.set_max(9).status_code
            record("viewer", code == 403, f"a viewer's PUT max_count → {code}")
            if u.status_code < 300:
                r.api("DELETE", f"/users/{u.json().get('id')}")

        if "max1" in want:
            r.set_max(1)
            rows: list = []
            stats = await phase(r, key, rows, t0, a, min(a.duration, 4 * a.scheduler_interval + 30), a.concurrency)
            peak = max((x["count"] or 0 for x in rows), default=0)
            record(
                "max1", peak == 1, f"max 1 under load: peak count {peak}, calls ok {stats['ok']}", rows, {"load": stats}
            )

        if "sustained" in want:
            r.set_max(a.max)
            rows = []
            reached = False

            async def until_max():
                nonlocal reached
                end = time.time() + a.duration
                while time.time() < end and not reached:
                    await asyncio.sleep(a.poll)
                    reached = (rows[-1]["count"] or 0) >= a.max if rows else False

            stats_task = asyncio.create_task(phase(r, key, rows, t0, a, a.duration, a.concurrency))
            await until_max()
            stats = await stats_task
            up_ok = any((x["count"] or 0) >= a.max for x in rows)
            back = await idle_until(r, rows, t0, a, lambda x: x["count"] == 1, a.down_timeout)
            s = summarize(rows, a.max)
            record(
                "sustained",
                up_ok and back,
                f"peak {s['peak']} (max {a.max}) at {s['reached_max_at']}s, back to 1 at "
                f"{s['back_to_1_at']}s; calls ok {stats['ok']} errors {stats['errors']}",
                rows,
                {"summary": s, "load": stats},
            )

        if "burst" in want:
            r.set_max(a.max)
            rows = []
            stats = await phase(r, key, rows, t0, a, a.burst, a.concurrency)
            back = await idle_until(r, rows, t0, a, lambda x: x["count"] == 1, a.down_timeout)
            s = summarize(rows, a.max)
            record(
                "burst",
                back and s["peak"] <= a.max,
                f"burst {a.burst:g}s: peak {s['peak']}, back to 1 at {s['back_to_1_at']}s",
                rows,
                {"summary": s, "load": stats},
            )

        if "lower" in want and a.max >= 2:
            r.set_max(a.max)
            rows = []
            task = asyncio.create_task(phase(r, key, rows, t0, a, a.duration, a.concurrency))
            end = time.time() + a.duration
            while time.time() < end and not (rows and (rows[-1]["count"] or 0) >= 2):
                await asyncio.sleep(a.poll)
            r.set_max(1)
            clamped = r.workers()["count"] == 1
            await task
            record(
                "lower",
                clamped,
                "max lowered to 1 under load: count clamped to 1 at once"
                if clamped
                else "count did not follow the lowered max",
                rows,
            )
        console_seen["stop"] = True
        bad = console_seen["bad"]
        record(
            "console",
            not bad,
            f"console /readyz answered 200 {console_seen['ok']} times"
            + (f", and not 200 {len(bad)} times, first at {bad[0][0]} s ({bad[0][1]})" if bad else ", never anything else"),
            extra={"console_failures": bad},
        )
    finally:
        console_seen["stop"] = True
        r.api(
            "PUT",
            "/config/scheduler",
            {"enabled": sched.get("enabled", False), "interval_seconds": sched.get("interval_seconds", 60)},
        )
        with open(a.out, "w") as f:
            json.dump(report, f, indent=1)
    print(f"\n{'ALL PASS' if all(ok for _, ok in results) else 'SOME FAILED'} ({len(results)} scenarios) → {a.out}")
    return 0 if all(ok for _, ok in results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
