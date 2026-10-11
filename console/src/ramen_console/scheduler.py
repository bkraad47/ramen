"""N1 + §21.7: background job that rebalances every group × zone each interval — the load-balancer leg and the
console's autoscaling (up to `max_count` under load, back down to 1 when quiet). Audited as user `scheduler`."""

import asyncio
import contextlib
import logging

from .audit import write_audit
from .deploy import cell_load
from .services import Services

log = logging.getLogger("ramen.scheduler")

DEFAULTS = {"enabled": False, "interval_seconds": 60}


async def get_config(store) -> dict:
    doc = await store.get("config", "scheduler") or {}
    return {k: doc.get(k, v) for k, v in DEFAULTS.items()}


async def set_config(store, **fields) -> dict:
    doc = await get_config(store)
    doc.update({k: v for k, v in fields.items() if v is not None})
    await store.put("config", "scheduler", doc)
    return doc


async def tick(svc: Services) -> list[dict]:
    """One scan: rebalance every (group, zone) pair; the autoscale decision inside it scales up or down (§21.7).
    A skewed (`high`) pair is audited `scheduler.rebalance` as before; every scale change is audited
    `workers.autoscale` by `Services.autoscale` itself."""
    cfg = await get_config(svc.store)
    if not cfg["enabled"]:
        return []
    envs = await svc.environments()
    pairs = {(e["group"], z) for e in envs for z in e.get("zones", [])}
    fired = []
    for group, zone in sorted(pairs):
        try:
            load = cell_load(await svc.cloud.workers(group, zone))
            result = await svc.rebalance(group, zone, user="scheduler")
        except Exception as e:  # noqa: BLE001 - a dead worker/API must not stop the scan
            log.warning("scheduler: rebalance(%s, %s) failed: %s", group, zone, e)
            continue
        scaled = (result.get("autoscale") or {}).get("action") in ("up", "down")
        if load == "high" or scaled:
            await write_audit(
                svc.store,
                user="scheduler",
                ip="-",
                action="scheduler.rebalance",
                target=f"{group}/{zone}",
                ok=bool(result.get("ok", True)),
                tags=["scheduler", "auto", f"load:{load}"],
            )
        fired.append({"group": group, "zone": zone, "load": load, "result": result})
    return fired


async def run_forever(svc: Services) -> None:
    """Thin loop: re-reads config every pass so a super-admin toggle takes effect without a restart."""
    while True:
        cfg = await get_config(svc.store)
        with contextlib.suppress(Exception):
            await tick(svc)
        await asyncio.sleep(max(cfg["interval_seconds"], 1))
