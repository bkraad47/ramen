import asyncio
import logging
import os

from .services import Services
from .util import now, uid

log = logging.getLogger("ramen.deploy")
# The dashboard names the load, never the colour (U11); the colour is only the visual signal.
LOADS = ("low", "even", "high", "down")
COLOR = {"low": "blue", "even": "green", "high": "red", "down": "grey"}


class Jobs:
    def __init__(self):
        self._jobs: dict[str, dict] = {}

    def create(self, kind, target) -> dict:
        job = {
            "id": uid(),
            "kind": kind,
            "target": target,
            "status": "running",
            "started": now(),
            "finished": None,
            "result": None,
            "error": None,
            "log": [],
        }
        self._jobs[job["id"]] = job
        return job

    def get(self, jid) -> dict | None:
        return self._jobs.get(jid)

    def recent(self, target_prefix="") -> list[dict]:
        return sorted(
            (j for j in self._jobs.values() if j["target"].startswith(target_prefix)),
            key=lambda j: j["started"],
            reverse=True,
        )[:20]


async def run_deploy(svc: Services, job: dict, group: str, env_name: str, zone: str | None, canary: bool, audit):
    zones: list[str] = []
    try:
        g = await svc.get_group(group)
        env = await svc.get_env(group, env_name)
        zones = [zone] if zone else env.get("zones", [])
        results = {}
        _, token, _ = await svc.secrets_for(group, env_name, None)
        token = await svc.secrets_backend.resolve(token or g.get("github_token"))
        job["log"].append(f"{now()} syncing repo {g['repo_url']}")
        await svc.cloud.sync_repo(group, g["repo_url"], env.get("ref") or g.get("ref", "main"), token)
        for z in zones:
            vars_, _, mcp = await svc.secrets_for(group, env_name, z)
            cfg = {
                "RAMEN_VERBOSE": "1" if env.get("verbose") else "0",
                "RAMEN_BLOCKED": ",".join(Services.blocked_for_zone(env, z)),  # U5: env-wide plus this zone's
                **vars_,
            }
            if mcp:
                cfg["RAMEN_MCP_KEYS"] = ",".join(mcp)
            # §16.2 / §16.3: stateless session ids and console-issued tokens need the group's secret on every pod;
            # the issuer is the console's public URL, which only RAMEN_PUBLIC_URL can state from a background job.
            cfg["RAMEN_SESSION_SECRET"] = await svc.session_secret(group)
            issuer = (os.environ.get("RAMEN_PUBLIC_URL") or "").strip().rstrip("/")
            if issuer:
                cfg["RAMEN_OAUTH_ISSUER"] = issuer
            else:
                job["log"].append(f"{now()} zone {z}: RAMEN_PUBLIC_URL unset, OAuth tokens are off for this worker")
            cfg = await svc.secrets_backend.resolve_config(cfg)
            job["log"].append(f"{now()} zone {z}: deploying (canary={'on' if canary else 'off'})")
            results[z] = await svc.cloud.deploy(
                group, env_name, z, canary=canary, config=cfg, spec=await svc.zone_spec(group, z), log=job["log"].append
            )
        ok = all(r.get("ok") for r in results.values()) if results else False
        failed = [
            f"{z} {w['id']}: {w.get('error') or w.get('status')}"
            for z, r in results.items()
            for w in r.get("workers", [])
            if not w.get("ok")
        ]
        for r in results.values():
            r.pop("log", None)
        job.update(
            status="ok" if ok else "error",
            result=results,
            finished=now(),
            error=None
            if ok
            else ("no zones configured" if not results else "workers failed to reload: " + "; ".join(failed)),
        )
    except Exception as e:  # noqa: BLE001 - surfaced to the UI
        log.exception("deploy failed")
        for z in zones:  # a failure before the adapter ran (e.g. bad git ref) must still tear down any canary (§7)
            try:
                job["log"].append(f"{now()} zone {z}: aborting, canary scaled to 0")
                await svc.cloud.abort_deploy(group, z)
            except Exception as e2:  # noqa: BLE001
                job["log"].append(f"{now()} zone {z}: could not abort: {type(e2).__name__}: {e2}")
        job.update(status="error", error=f"{type(e).__name__}: {e}", finished=now())
    try:
        env = await svc.get_env(group, env_name)
        env["last_deploy"] = {
            "job": job["id"],
            "status": job["status"],
            "at": job["finished"],
            "error": job["error"],
            "packages": _packages(job.get("result") or {}),
        }
        await svc.store.put("environments", env["id"], env)
    except Exception:  # noqa: BLE001
        log.exception("could not record last_deploy")
    await audit("deploy", f"{group}/{env_name}", job["status"] == "ok", [f"group:{group}", f"job:{job['id']}"])


def _packages(result: dict) -> dict:
    out = {}
    for z, r in result.items():
        for w in r.get("workers", []):
            res = w.get("result") or {}
            if isinstance(res, dict) and ("tools" in res or "errors" in res):
                out[f"{z} {w['id']}"] = {k: res.get(k, []) for k in ("tools", "resources", "prompts", "errors")}
    return out


def cell_load(workers: list[dict]) -> str:
    """How loaded a zone x group cell is: `low`, `even`, `high` or `down`."""
    loads = [w.get("load") for w in workers]
    if not loads or all(x == "down" for x in loads):
        return "down"
    if "high" in loads:
        return "high"
    return "low" if all(x in ("low", "down") for x in loads) else "even"


def cell_color(workers: list[dict]) -> str:
    """Kept for API clients that read `cells[zone][group].color`; the UI reads `load`."""
    return COLOR[cell_load(workers)]


async def dashboard(svc: Services, groups: list[dict]) -> dict:
    zones = await svc.zones()
    envs = await svc.environments()
    wanted = {(e["group"], z) for e in envs for z in e.get("zones", [])}
    cells: dict[str, dict] = {z["id"]: {} for z in zones}
    pairs = [(g["id"], z["id"]) for z in zones for g in groups if (g["id"], z["id"]) in wanted]

    async def one(g, z):
        try:
            ws = await svc.cloud.workers(g, z)
        except Exception as e:  # noqa: BLE001
            ws = [{"id": "?", "load": "down", "metrics": {}, "error": str(e)}]
        load = cell_load(ws)
        cells[z][g] = {"load": load, "color": COLOR[load], "workers": ws}

    await asyncio.gather(*(one(g, z) for g, z in pairs))
    return {"zones": [z["id"] for z in zones], "groups": [g["id"] for g in groups], "cells": cells, "at": now()}
