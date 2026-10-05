import asyncio
import logging
import os

from . import baseuri, compat, github_app, golden, regions
from .cloud.base import GateError
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


async def run_deploy(
    svc: Services, job: dict, group: str, env_name: str, zone: str | None, canary: bool, audit, breaking: bool = False
):
    zones: list[str] = []
    try:
        g = await svc.get_group(group)
        env = await svc.get_env(group, env_name)
        zones = [zone] if zone else env.get("zones", [])
        results = {}
        _, token, _ = await svc.secrets_for(group, env_name, None)
        token = await github_app.resolve_token(svc.store, g) or await svc.secrets_backend.resolve(
            token or g.get("github_token")
        )
        job["log"].append(f"{now()} syncing repo {g['repo_url']}")
        await svc.cloud.sync_repo(group, g["repo_url"], env.get("ref") or g.get("ref", "main"), token)
        cases = await _golden_cases(svc, group, job)
        prev = env.get("last_deploy") or {}
        blocked = await svc.blocked_regions()
        gates: dict[str, dict] = {}
        for z in zones:
            zdoc = await svc.store.get("zones", z) or {}
            if hit := regions.blocked_region(zdoc.get("provider", ""), zdoc.get("region") or "", blocked):
                # C7: a region blocked after the zone was created; the warning digest (N2) carries this line
                msg = regions.message(hit)
                log.warning("deploy %s/%s: zone %s refused, %s", group, env_name, z, msg)
                job["log"].append(f"{now()} zone {z}: refused, {msg}")
                results[z] = {"ok": False, "error": msg, "workers": []}
                continue
            vars_, _, mcp = await svc.secrets_for(group, env_name, z)
            cfg = {
                "RAMEN_VERBOSE": "1" if env.get("verbose") else "0",
                "RAMEN_BLOCKED": ",".join(Services.blocked_for_zone(env, z)),  # U5: env-wide plus this zone's
                **vars_,
            }
            if mcp:
                cfg["RAMEN_MCP_KEYS"] = ",".join(mcp)
            # N7: item throttle is zone-local (this zone's own worker_config); scope throttle is one Redis
            # shared by every zone of the group, so the limit holds across the whole group/environment.
            w = await svc.worker_config(group, z)
            if w.get("redis_item_url"):
                cfg["RAMEN_REDIS_ITEM_URL"] = w["redis_item_url"]
            if w.get("item_ip_per_min"):
                cfg["RAMEN_THROTTLE_ITEM_IP"] = str(w["item_ip_per_min"])
            if w.get("item_token_per_min"):
                cfg["RAMEN_THROTTLE_ITEM_TOKEN"] = str(w["item_token_per_min"])
            if g.get("redis_scope_url"):
                cfg["RAMEN_REDIS_SCOPE_URL"] = g["redis_scope_url"]
            if g.get("scope_ip_per_min"):
                cfg["RAMEN_THROTTLE_SCOPE_IP"] = str(g["scope_ip_per_min"])
            if g.get("scope_token_per_min"):
                cfg["RAMEN_THROTTLE_SCOPE_TOKEN"] = str(g["scope_token_per_min"])
            # §16.2 / §16.3: stateless session ids and console-issued tokens need the group's secret on every pod;
            # the issuer is the console's public URL: the Config page's base URI (0.6.1), else RAMEN_PUBLIC_URL — a
            # background job has no request to read it from.
            cfg["RAMEN_SESSION_SECRET"] = await svc.session_secret(group)
            issuer = (await baseuri.load(svc.store) or os.environ.get("RAMEN_PUBLIC_URL") or "").strip().rstrip("/")
            if issuer:
                cfg["RAMEN_OAUTH_ISSUER"] = issuer
                # the workers sit behind the same address: with it the 401 challenge names an absolute
                # resource_metadata URL (RFC 9728); found relative on the 0.5.0 GKE run
                cfg["RAMEN_PUBLIC_URL"] = issuer
            else:
                job["log"].append(
                    f"{now()} zone {z}: no base URI or RAMEN_PUBLIC_URL, OAuth tokens are off for this worker"
                )
            cfg = await svc.secrets_backend.resolve_config(cfg)
            job["log"].append(f"{now()} zone {z}: deploying (canary={'on' if canary else 'off'})")
            gates[z] = {}
            stable = (prev.get("manifest") or {}).get((prev.get("stable") or {}).get(z))
            results[z] = await svc.cloud.deploy(
                group,
                env_name,
                z,
                canary=canary,
                config=cfg,
                spec=await svc.zone_spec(group, z),
                log=job["log"].append,
                gate=_gate(job, group, env_name, z, breaking, stable, cases, cfg, gates[z]),
            )
            results[z].update(gates[z])
        ok = all(r.get("ok") for r in results.values()) if results else False
        failed = [
            f"{z} {w['id']}: {w.get('error') or w.get('status')}"
            for z, r in results.items()
            for w in r.get("workers", [])
            if not w.get("ok")
        ] + [f"{z}: {r['error']}" for z, r in results.items() if not r.get("ok") and not r.get("workers")]
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
        manifest, stable = _manifests(job.get("result") or {}, env.get("last_deploy") or {})
        env["last_deploy"] = {
            "job": job["id"],
            "status": job["status"],
            "at": job["finished"],
            "error": job["error"],
            "packages": _packages(job.get("result") or {}),
            "manifest": manifest,  # C3: full definitions + hash per worker
            "stable": stable,  # zone → the manifest key the next canary is diffed against (B3)
        }
        await svc.store.put("environments", env["id"], env)
    except Exception:  # noqa: BLE001
        log.exception("could not record last_deploy")
    await audit("deploy", f"{group}/{env_name}", job["status"] == "ok", [f"group:{group}", f"job:{job['id']}"])


async def _golden_cases(svc: Services, group: str, job: dict) -> list[dict]:
    """C4: `mcp/tests.yaml` from the synced repo; a file the console cannot run fails the deploy before any zone."""
    raw = await svc.cloud.read_file(group, golden.PATH)
    if raw is None:
        return []
    try:
        cases = golden.parse(raw.decode("utf-8", "replace"))
    except golden.GoldenError as e:
        raise RuntimeError(f"{golden.PATH}: {e}") from None
    job["log"].append(f"{now()} {golden.PATH}: {len(cases)} golden case{'s' if len(cases) != 1 else ''}")
    return cases


def _gate(job, group, env_name, zone, breaking, stable, cases, cfg, info):
    """The deploy gate the adapter awaits on the canary (CONTRACTS C4/C5): schema compatibility against the stable
    track's stored manifest, then every golden case. `info` collects `compat` / `golden` for the job result."""
    log_ = job["log"]
    prefix = f"RAMEN_SECRET_{group.upper().replace('-', '_')}__"

    def secret(ref: str):  # `{{$group.VAR}}` → this group's already-resolved deploy secret
        g, _, name = ref.partition(".")
        return cfg.get(f"{prefix}{name}") if g == group else None

    async def gate(result, call):
        if stable is not None and (new := compat.manifest_of(result)) is not None:
            d = compat.diff(stable, new)
            info["compat"] = {**d, "against": stable["hash"], "forced": False}
            log_.append(f"{now()} zone {zone}: schema vs stable {stable['hash'][:12]}: {compat.summary(d)}")
            if compat.is_breaking(d):
                if not breaking:
                    raise GateError(
                        "breaking schema change (deploy again with breaking: true to accept): "
                        + "; ".join(d["breaking"])
                    )
                info["compat"]["forced"] = True
                log_.append(f"{now()} zone {zone}: breaking change accepted (breaking: true)")
        if not cases:
            return
        if call is None:
            log.warning("deploy %s/%s: zone %s: golden cases skipped, the group has no MCP key", group, env_name, zone)
            log_.append(f"{now()} zone {zone}: golden cases skipped: no MCP key to call the canary with")
            info["golden"] = {"skipped": "no MCP key", "cases": len(cases)}
            return
        failed, skipped = [], []
        blocked = {b for b in cfg.get("RAMEN_BLOCKED", "").split(",") if b}
        for i, c in enumerate(cases, 1):
            if c["tool"] in blocked:  # §9: a blocked name answers -32601 on purpose; its cases are not a verdict
                skipped.append(f"{c['name']}: blocked")
                log_.append(f"{now()} zone {zone}: golden {c['name']}: skipped (blocked)")
                continue
            try:
                args = golden.resolve_args(c["args"], secret)
            except golden.GoldenError as e:
                raise GateError(f"golden case {c['name']}: {e}") from None
            why = golden.check_response(c, await call(golden.request({**c, "args": args}, i)))
            log_.append(f"{now()} zone {zone}: golden {c['name']}: {'ok' if why is None else 'FAILED ' + why}")
            if why is not None:
                failed.append(f"{c['name']}: {why}")
        info["golden"] = {"passed": len(cases) - len(failed) - len(skipped), "failed": failed}
        if skipped:
            info["golden"]["skipped"] = skipped
        if failed:
            raise GateError("golden cases failed: " + "; ".join(failed))

    return gate


def _manifests(result: dict, prev: dict) -> tuple[dict, dict]:
    """C3 `manifest` per worker plus `stable`: zone → key of the stable track's manifest. A zone this job did not
    deploy successfully keeps the stable manifest it had, so a refused canary never erases the baseline."""
    manifest, stable = {}, {}
    for z, r in result.items():
        for w in r.get("workers", []):
            if (m := compat.manifest_of(w.get("result"))) is None:
                continue
            key = f"{z} {w['id']}"
            manifest[key] = m
            if r.get("ok") and w.get("ok") and (z not in stable or w.get("track") == "stable"):
                stable[z] = key
    for z, key in (prev.get("stable") or {}).items():
        if z not in stable and key in (prev.get("manifest") or {}):
            manifest[key], stable[z] = prev["manifest"][key], key
    return manifest, stable


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
