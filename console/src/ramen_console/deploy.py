import asyncio
import json
import logging
import os
from datetime import UTC, datetime

from . import baseuri, compat, github_app, golden, imagebuild, otel, regions, roles, sleeping, toolaccess
from .cloud.base import GateError
from .security import ScrubbedLog
from .services import Services
from .util import now, uid

log = logging.getLogger("ramen.deploy")
# The dashboard names the load, never the colour (U11); the colour is only the visual signal.
LOADS = ("low", "even", "high", "down")
COLOR = {"low": "blue", "even": "green", "high": "red", "down": "grey"}


STALE_SECS = "RAMEN_JOB_STALE_SECS"  # §26.2: a `running` job untouched this long is lost
KEEP = 50  # per target
LOST = "lost (console restarted)"


class JobLog(ScrubbedLog):
    """A job's log: scrubbed (§24.5) and, since §26.2, written to the store as it grows."""

    def __init__(self, job: dict, jobs: "Jobs | None"):
        super().__init__()
        self.job, self.jobs = job, jobs

    def append(self, line) -> None:
        super().append(line)
        if self.jobs is not None:
            self.jobs.touch(self.job)


class Jobs:
    """§26.2: deploy jobs in the store's `jobs` collection — written at creation, as the log grows (one write per
    second of lines) and at the end — so a job is visible after a restart and from every replica. Running jobs of
    this process are served live; a stored `running` job nobody touched for RAMEN_JOB_STALE_SECS is lost."""

    def __init__(self, store):
        self.store = store
        self._live: dict[str, dict] = {}
        self._pending: set[str] = set()
        self._loop: asyncio.AbstractEventLoop | None = None

    async def create(self, kind, target) -> dict:
        self._loop = asyncio.get_running_loop()
        job = {
            "id": uid(),
            "kind": kind,
            "target": target,
            "status": "running",
            "started": now(),
            "finished": None,
            "result": None,
            "error": None,
        }
        job["log"] = JobLog(job, self)
        self._live[job["id"]] = job
        await self.save(job)
        await self._prune(target)
        return job

    @staticmethod
    def snapshot(job: dict) -> dict:
        return {**{k: v for k, v in job.items() if k != "log"}, "log": list(job.get("log") or []), "updated": now()}

    async def save(self, job: dict) -> None:
        await self.store.put("jobs", job["id"], self.snapshot(job))
        if job["status"] != "running":
            self._live.pop(job["id"], None)

    def touch(self, job: dict) -> None:
        """From a log append, on the loop or off it: one write per burst of lines, a second later."""
        if job["id"] in self._pending or self._loop is None or self._loop.is_closed():
            return
        self._pending.add(job["id"])

        async def later():
            await asyncio.sleep(1)
            self._pending.discard(job["id"])
            try:
                await self.save(job)
            except Exception:  # noqa: BLE001 - the end-of-job write carries the lines
                log.exception("could not write job %s", job["id"])

        self._loop.call_soon_threadsafe(lambda: self._loop.create_task(later()))

    @staticmethod
    def _aged(doc: dict) -> dict:
        if doc.get("status") == "running":
            try:
                secs = int(os.environ.get(STALE_SECS, "1800"))
                at = datetime.fromisoformat(doc.get("updated") or doc.get("started"))
                if at.tzinfo is None:
                    at = at.replace(tzinfo=UTC)
                stale = (datetime.now(UTC) - at).total_seconds() > secs
            except (TypeError, ValueError):
                stale = True
            if stale:
                doc.update(status="error", error=LOST)
        return doc

    async def get(self, jid) -> dict | None:
        if j := self._live.get(jid):
            return j
        doc = await self.store.get("jobs", jid)
        return self._aged(doc) if doc else None

    async def recent(self, target_prefix="") -> list[dict]:
        docs = await self.store.list("jobs")
        out = {d["id"]: self._aged(d) for d in docs if d.get("target", "").startswith(target_prefix)}
        out.update({jid: j for jid, j in self._live.items() if j["target"].startswith(target_prefix)})
        return sorted(out.values(), key=lambda j: j["started"], reverse=True)[:20]

    async def _prune(self, target) -> None:
        docs = sorted(await self.store.list("jobs", {"target": target}), key=lambda d: d["started"], reverse=True)
        for d in docs[KEEP:]:
            await self.store.delete("jobs", d["id"])
            self._live.pop(d["id"], None)


async def _save(job: dict) -> None:
    if isinstance(job.get("log"), JobLog) and job["log"].jobs is not None:
        await job["log"].jobs.save(job)


async def issuer(svc: Services) -> str:
    """The console's public URL: the Config page's base URI (0.6.1), else RAMEN_PUBLIC_URL — a background job has
    no request to read it from."""
    return (await baseuri.load(svc.store) or os.environ.get("RAMEN_PUBLIC_URL") or "").strip().rstrip("/")


async def zone_config(svc: Services, g: dict, env: dict, z: str, otel_cfg: dict, custom_roles, issuer_: str, warn=None):
    """The deploy config of one zone of an environment — what the deploy writes and what §26.3's push rewrites."""
    group, env_name = g["name"], env["name"]
    vars_, _, mcp = await svc.secrets_for(group, env_name, z)
    cfg = {
        "RAMEN_VERBOSE": "1" if env.get("verbose") else "0",
        "RAMEN_BLOCKED": ",".join(Services.blocked_for_zone(env, z)),  # U5: env-wide plus this zone's
        "RAMEN_TOOL_ACCESS": toolaccess.compact(env.get("tool_access")),  # C10: env-wide, every zone
        "RAMEN_ROLES": roles.compact(custom_roles),  # D48: custom role → base, so the node can rank a kind
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
    # §16.2 / §16.3: stateless session ids and console-issued tokens need the group's secret on every pod
    cfg["RAMEN_SESSION_SECRET"] = await svc.session_secret(group)
    if issuer_:
        cfg["RAMEN_OAUTH_ISSUER"] = issuer_
        # the workers sit behind the same address: with it the 401 challenge names an absolute
        # resource_metadata URL (RFC 9728); found relative on the 0.5.0 GKE run
        cfg["RAMEN_PUBLIC_URL"] = issuer_
    elif warn:
        warn(f"{now()} zone {z}: no base URI or RAMEN_PUBLIC_URL, OAuth tokens are off for this worker")
    cfg.update(otel.worker_env(otel_cfg, g, group, z))  # §24.3 / §26.3: export, what agents are told, consent
    return await svc.secrets_backend.resolve_config(cfg)


async def push_config(svc: Services) -> dict[str, str]:
    """§26.3: rewrite every deployed zone's worker config and reload its workers. `{"<g>/<z>": "ok" | "<error>"}`;
    a zone that cannot be reached keeps the old config until its next deploy. When two environments of a group
    share a zone, the one deployed last is the zone's config."""
    otel_cfg = await otel.get_config(svc.store)
    custom_roles = await roles.load(svc.store, force=True)
    issuer_ = await issuer(svc)
    targets: dict[tuple[str, str], tuple[str, dict]] = {}
    for env in await svc.environments():
        last = env.get("last_deploy") or {}
        deployed = {k.split(" ")[0] for k in last.get("packages") or {}} | set(last.get("stable") or {})
        for z in env.get("zones") or []:
            if z in deployed and (last.get("at") or "") >= targets.get((env["group"], z), ("", {}))[0]:
                targets[(env["group"], z)] = (last.get("at") or "", env)
    out = {}
    for (group, z), (_, env) in sorted(targets.items()):
        try:
            g = await svc.get_group(group)
            cfg = await zone_config(svc, g, env, z, otel_cfg, custom_roles, issuer_)
            r = await svc.cloud.push_config(group, env["name"], z, cfg)
            out[f"{group}/{z}"] = "ok" if r.get("ok") else str(r.get("error") or "workers failed to reload")
        except Exception as e:  # noqa: BLE001 - reported per zone, never raised to the settings route
            out[f"{group}/{z}"] = f"{type(e).__name__}: {e}"
    return out


async def run_deploy(
    svc: Services,
    job: dict,
    group: str,
    env_name: str,
    zone: str | list[str] | None,
    canary: bool,
    audit,
    breaking: bool = False,
    user: str = "-",
    ip: str = "-",
):
    zones: list[str] = []
    try:
        g = await svc.get_group(group)
        env = await svc.get_env(group, env_name)
        zones = list(zone) if isinstance(zone, list) else ([zone] if zone else env.get("zones", []))  # §27.3: a bind
        results = {}
        _, token, _ = await svc.secrets_for(group, env_name, None)
        token = await github_app.resolve_token(svc.store, g) or await svc.secrets_backend.resolve(
            token or g.get("github_token")
        )
        job["log"].append(f"{now()} syncing repo {g['repo_url']}")
        await svc.cloud.sync_repo(group, g["repo_url"], env.get("ref") or g.get("ref", "main"), token)
        cases = await _golden_cases(svc, group, job)
        # §24.2: the group's own image (base + mcp/requirements.txt) exists before any canary starts; a failed build
        # stops the deploy here, with the build's log tail above, and the stable track is untouched
        await imagebuild.ensure(svc, group, job["log"].append)
        otel_cfg = await otel.get_config(svc.store)
        g = await svc.get_group(group)
        prev = env.get("last_deploy") or {}
        blocked = await svc.blocked_regions()
        custom_roles = await roles.load(svc.store, force=True)
        issuer_ = await issuer(svc)
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
            cfg = await zone_config(svc, g, env, z, otel_cfg, custom_roles, issuer_, job["log"].append)
            w = await svc.worker_config(group, z)
            if sleeping.state_of(w) != sleeping.AWAKE:  # §24.1: a deploy wakes a sleeping zone first
                job["log"].append(f"{now()} zone {z}: {sleeping.state_of(w)}, waking it before the deploy")
                await svc.wake(group, z, "deploy", user=user, ip=ip)
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
            if results[z].get("ok"):
                await svc.touch_zone(group, z)
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
        await _save(job)
    except Exception as e:  # noqa: BLE001 - surfaced to the UI
        log.exception("deploy failed")
        for z in zones:  # a failure before the adapter ran (e.g. bad git ref) must still tear down any canary (§7)
            try:
                job["log"].append(f"{now()} zone {z}: aborting, canary scaled to 0")
                await svc.cloud.abort_deploy(group, z)
            except Exception as e2:  # noqa: BLE001
                job["log"].append(f"{now()} zone {z}: could not abort: {type(e2).__name__}: {e2}")
        job.update(status="error", error=f"{type(e).__name__}: {e}", finished=now())
        await _save(job)
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
    # §24.3: one span per deploy job; a collector that is down is a warning in the log, never a failed deploy
    await otel.export_deploy(svc.store, job, group, env_name, zones, await svc.image_for(group))


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
        # §21.2: a group that opted into guardrails never deploys with a broken guardrails file — the worker reports
        # the load error (`errors: [{"package": "guardrails", ...}]`), the console refuses the deploy
        for e in (result or {}).get("errors") or []:
            if isinstance(e, dict) and e.get("package") == "guardrails":
                log_.append(f"{now()} zone {zone}: guardrails did not load: {e.get('reason')}")
                raise GateError(f"guardrails did not load on the canary: {e.get('reason')}")
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
        access = json.loads(cfg.get("RAMEN_TOOL_ACCESS") or "{}")
        for i, c in enumerate(cases, 1):
            # §9 / §20: a blocked name answers -32601 on purpose, and the cases run as the group key, so a tool the key
            # may not call (hidden -32601 or forbidden -32003) is not a verdict either — skipped, never failed
            why_skip = "blocked" if c["tool"] in blocked else None
            if why_skip is None and c["tool"] in access and "key" not in access[c["tool"]].get("call", []):
                why_skip = "not callable for key"
            if why_skip:
                skipped.append(f"{c['name']}: {why_skip}")
                log_.append(f"{now()} zone {zone}: golden {c['name']}: skipped ({why_skip})")
                continue
            try:
                args = golden.resolve_args(c["args"], secret)
            except golden.GoldenError as e:
                raise GateError(f"golden case {c['name']}: {e}") from None
            why = await golden.run({**c, "args": args}, call, i)
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
