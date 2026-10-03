"""N1: background job that watches zone load and rebalances when it skews to one worker."""

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
    """One scan: rebalance any (group, zone) pair whose load has skewed to one worker."""
    cfg = await get_config(svc.store)
    if not cfg["enabled"]:
        return []
    envs = await svc.environments()
    pairs = {(e["group"], z) for e in envs for z in e.get("zones", [])}
    fired = []
    for group, zone in sorted(pairs):
        try:
            load = cell_load(await svc.cloud.workers(group, zone))
        except Exception as e:  # noqa: BLE001 - a dead worker/API must not stop the scan
            log.warning("scheduler: workers(%s, %s) failed: %s", group, zone, e)
            continue
        if load != "high":
            continue
        result = await svc.rebalance(group, zone)
        await write_audit(
            svc.store,
            user="scheduler",
            ip="-",
            action="scheduler.rebalance",
            target=f"{group}/{zone}",
            ok=bool(result.get("ok", True)),
            tags=["scheduler", "auto"],
        )
        fired.append({"group": group, "zone": zone, "result": result})
    return fired


async def run_forever(svc: Services) -> None:
    """Thin loop: re-reads config every pass so a super-admin toggle takes effect without a restart."""
    while True:
        cfg = await get_config(svc.store)
        with contextlib.suppress(Exception):
            await tick(svc)
        await asyncio.sleep(max(cfg["interval_seconds"], 1))
