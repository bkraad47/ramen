import asyncio
import json
import logging
import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

from . import (
    baseuri,
    compat,
    configs,
    evals,
    github_app,
    golden,
    guardrails,
    imagebuild,
    llm_proxy,
    llm_scope,
    otel,
    regions,
    roles,
    sleeping,
    toolaccess,
)
from . import llm_providers as lp
from . import skillspector as sk
from .cloud.base import GateError
from .security import ScrubbedLog, remember_secret, scrub
from .services import Services
from .util import now, uid

log = logging.getLogger("ramen.deploy")
# The dashboard names the load, never the colour (U11); the colour is only the visual signal.
LOADS = ("low", "even", "high", "down")
COLOR = {"low": "blue", "even": "green", "high": "red", "down": "grey"}


STALE_SECS = "RAMEN_JOB_STALE_SECS"  # §26.2: a `running` job untouched this long is lost
KEEP = 50  # per target
LOST = "lost (console restarted)"


def scrub_errors(v, key=""):
    """Every string under an `error` key, at any depth, scrubbed (§24.5): what job records and `last_deploy` keep."""
    if isinstance(v, dict):
        return {k: scrub_errors(x, k) for k, x in v.items()}
    if isinstance(v, list):
        return [scrub_errors(x, key) for x in v]
    return scrub(v) if key == "error" and isinstance(v, str) else v


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
        doc = {
            k: scrub_errors(v, k) for k, v in job.items() if k != "log"
        }  # N1: an error may quote a URL with a password
        return {**doc, "log": list(job.get("log") or []), "updated": now()}

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


async def zone_config(
    svc: Services,
    g: dict,
    env: dict,
    z: str,
    otel_cfg: dict,
    custom_roles,
    issuer_: str,
    warn=None,
    deploy_job: str | None = None,
):
    """The deploy config of one zone of an environment — what the deploy writes (`deploy_job` = its job id) and what
    §26.3's push rewrites (the judge token the zone's stable track already holds, §38.1)."""
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
    elif default := default_redis():  # §33.4a: the chart's Redis (password in its Secret) when the group names none
        cfg["RAMEN_REDIS_SCOPE_URL"] = default
    if g.get("scope_ip_per_min"):
        cfg["RAMEN_THROTTLE_SCOPE_IP"] = str(g["scope_ip_per_min"])
    if g.get("scope_token_per_min"):
        cfg["RAMEN_THROTTLE_SCOPE_TOKEN"] = str(g["scope_token_per_min"])
    # §16.2 / §16.3: stateless session ids and console-issued tokens need the group's secret on every pod
    cfg["RAMEN_SESSION_SECRET"] = await svc.session_secret(group)
    if job_id := (env.get("last_deploy") or {}).get("job"):
        # §31.4a: a config push keeps the current deploy's own key (the deploy itself sets the new one over this)
        cfg["RAMEN_EVALS_KEY"] = evals.evals_key(cfg["RAMEN_SESSION_SECRET"], job_id)
        remember_secret(cfg["RAMEN_EVALS_KEY"])
    if issuer_:
        cfg["RAMEN_OAUTH_ISSUER"] = issuer_
        # the workers sit behind the same address: with it the 401 challenge names an absolute
        # resource_metadata URL (RFC 9728); found relative on the 0.5.0 GKE run
        cfg["RAMEN_PUBLIC_URL"] = issuer_
    elif warn:
        warn(f"{now()} zone {z}: no base URI or RAMEN_PUBLIC_URL, OAuth tokens are off for this worker")
    cfg.update(otel.worker_env(otel_cfg, g, group, z))  # §24.3 / §26.3: export, what agents are told, consent
    cfg.update(evals.worker_env(await evals.load(svc, group), g))  # §29.3: the evals version gated calls must name
    cfg.update(sk.worker_env(await sk.load_baseline(svc, group), g))  # §30.3: the same for the scan policy
    cfg.update(configs.worker_env(await configs.load(svc, group)))  # §31.2: the LLM configs version, always required
    gr = await guardrails.load(svc, group)
    cfg.update(guardrails.worker_env(gr, g))  # §36.3: the guardrails policy version
    # §36.2 / §38.1: RAMEN_LLM_* only when a rail needs a model — the console's judge proxy, never a provider key
    judge_job = deploy_job or next(iter(llm_proxy.live_jobs(env, z)), None) or "-"
    token = llm_proxy.judge_token(cfg["RAMEN_SESSION_SECRET"], group, z, judge_job)
    try:
        cfg.update(await guardrails.deploy_env(svc, g, gr, llm_proxy.proxy_url(issuer_), token))
    except (llm_scope.NoProvider, llm_scope.BadModel, llm_proxy.NoUrl) as e:  # refused at sync; a push warns
        if warn:
            warn(f"{now()} zone {z}: {guardrails.PATH}: {e}; the rails keep no credential")
    return await svc.secrets_backend.resolve_config(cfg)


def default_redis() -> str:
    """`RAMEN_REDIS_URL`: the chart's Redis, `redis://:<password>@redis.<ns>:6379` from its Secret (§33.4a)."""
    url = os.environ.get("RAMEN_REDIS_URL", "").strip()
    if url and (pw := urlsplit(url).password):
        remember_secret(pw)
    return url


async def push_config(
    svc: Services, warnings: dict[str, str] | None = None, group: str | None = None
) -> dict[str, str]:
    """§26.3: rewrite every deployed zone's worker config and reload its workers — only `group`'s zones when given (a
    group's own settings never roll another group's workers). `{"<g>/<z>": "ok" | "<error>"}`;
    a zone that cannot be reached keeps the old config until its next deploy. When two environments of a group
    share a zone, the one deployed last is the zone's config. P4-B1: a zone whose stable track took the push but
    whose canary could not be reloaded or rolled is "ok" and named in `warnings` (`{"<g>/<z>": "<pod>: <code>"}`)."""
    otel_cfg = await otel.get_config(svc.store)
    custom_roles = await roles.load(svc.store, force=True)
    issuer_ = await issuer(svc)
    targets: dict[tuple[str, str], tuple[str, dict]] = {}
    for env in await svc.environments():
        if group is not None and env.get("group") != group:
            continue
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
            if r.get("warning") and warnings is not None:
                warnings[f"{group}/{z}"] = str(r["warning"])
        except Exception as e:  # noqa: BLE001 - reported per zone, never raised to the settings route
            out[f"{group}/{z}"] = f"{type(e).__name__}: {e}"
    return out


def pushed_answer(pushed: dict[str, str], warnings: dict[str, str], group: str | None = None) -> dict:
    """The `pushed` part of a settings answer — one group's when `group` is given — plus `warning` only when a canary
    could not be reached (P4-B1): never silent, never a failed zone."""
    keep = (lambda k: k.startswith(f"{group}/")) if group else (lambda k: True)
    out = {"pushed": {k: v for k, v in pushed.items() if keep(k)}}
    if w := {k: v for k, v in warnings.items() if keep(k)}:
        out["warning"] = w
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
        await _env_file(svc, group)
        await _llm_configs(svc, group, job)
        await _rails(svc, g, job)
        # §36.2: the group's own LLM providers win over the org's for evals and scans (the whole scope's env)
        group_cfg, org_cfg = await lp.load(svc.store, group), await lp.load(svc.store)
        # §29.2: the repo's promptfoo config runs after each zone's golden cases; no engine = said once, per deploy
        ev = {"text": await evals.load(svc, group), "env": env_name, "providers": {}}
        if ev["text"] is not None:
            _, providers = llm_scope.pick_scope(group_cfg, org_cfg) or ("none", {})
            # §33.4a: the allowlist — what the file may run, wherever it runs — fails the deploy here, like §31.0
            if bad := evals.validate(ev["text"], lp.configured(providers), lp.base_urls(providers)):
                raise RuntimeError(f"{evals.PATH}: {bad[0]}")
            if not evals.image():
                job["log"].append(f"{now()} {evals.PATH}: skipped, no evals engine (RAMEN_EVALS_IMAGE is not set)")
            else:
                ev["providers"] = lp.env(providers)
        # §30.2: the repo's skills (+ what each zone serves) are scanned after the evals; no engine = said once
        sc = await _scan_setup(svc, g, env_name, job, group_cfg, org_cfg)
        # §24.2: the group's own image (base + mcp/requirements.txt) exists before any canary starts; a failed build
        # stops the deploy here, with the build's log tail above, and the stable track is untouched
        await imagebuild.ensure(svc, group, job["log"].append)
        otel_cfg = await otel.get_config(svc.store)
        g = await svc.get_group(group)
        prev = env.get("last_deploy") or {}
        blocked = await svc.blocked_regions()
        custom_roles = await roles.load(svc.store, force=True)
        issuer_ = await issuer(svc)
        # §31.4a: Ramen's own evals client, rotated per deploy (derived from this job, §33.4a); scrubbed from every log
        evals_key = evals.evals_key(await svc.session_secret(group), job["id"])
        remember_secret(evals_key)
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
            cfg = await zone_config(svc, g, env, z, otel_cfg, custom_roles, issuer_, job["log"].append, job["id"])
            cfg["RAMEN_EVALS_KEY"] = evals_key  # every zone's deploy Secret / env file: canary and stable accept it
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
                gate=_gate(job, group, env_name, z, breaking, stable, cases, cfg, gates[z], svc, g, ev, sc),
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
        judge = llm_proxy.judge_record(env.get("last_deploy") or {}, job.get("result") or {}, job["id"])
        env["last_deploy"] = {
            "job": job["id"],
            "status": job["status"],
            "at": job["finished"],
            "error": scrub(job["error"]) if job["error"] else job["error"],
            "packages": _packages(job.get("result") or {}),
            "manifest": manifest,  # C3: full definitions + hash per worker
            "stable": stable,  # zone → the manifest key the next canary is diffed against (B3)
            "judge": judge,  # §38.1: zone → the deploy jobs whose judge tokens its workers may hold
            # §29.2: the group page's per-zone evals badge
            "evals": {
                z: r["evals"] for z, r in (job.get("result") or {}).items() if isinstance(r, dict) and "evals" in r
            },
            # §30.2: the per-zone scan badge and its link to the report page
            "skillspector": {
                z: r["skillspector"]
                for z, r in (job.get("result") or {}).items()
                if isinstance(r, dict) and "skillspector" in r
            },
        }
        await svc.store.put("environments", env["id"], env)
    except Exception:  # noqa: BLE001
        log.exception("could not record last_deploy")
    await audit("deploy", f"{group}/{env_name}", job["status"] == "ok", [f"group:{group}", f"job:{job['id']}"])
    # §24.3: one span per deploy job; a collector that is down is a warning in the log, never a failed deploy
    await otel.export_deploy(svc.store, job, group, env_name, zones, await svc.image_for(group))


async def _llm_configs(svc: Services, group: str, job: dict) -> None:
    """§31.0: the synced repo's `mcp/llm-config/` must validate (allowed keys, hook commands, sizes) or the deploy
    fails here, before any zone, with the violation named."""
    lc = await configs.load(svc, group)
    if lc is None:
        return
    if lc.get("error"):
        raise RuntimeError(f"{configs.DIR}: {lc['error']}")
    job["log"].append(f"{now()} {configs.DIR}: version {lc['version']}, {len(lc['files'])} files")


async def _rails(svc: Services, g: dict, job: dict) -> None:
    """§36.2: when the synced `mcp/guardrails.yaml` names an LLM rail, the group's or the org's provider must resolve
    (and the group's `guardrails.model` be allowed) or the deploy fails here, before any zone; the chosen scope is
    logged, never the key."""
    gr = await guardrails.load(svc, g["name"])
    if gr is None:
        return
    if gr.get("error"):
        job["log"].append(f"{now()} {guardrails.PATH}: warning, {gr['error']}; clients get the version only")
    if not guardrails.needs_llm(gr):
        return
    try:
        res = guardrails.resolve(g, await lp.load(svc.store, g["name"]), await lp.load(svc.store))
        if not llm_proxy.proxy_url(await issuer(svc)):
            raise llm_proxy.NoUrl()
    except (llm_scope.NoProvider, llm_scope.BadModel, llm_proxy.NoUrl) as e:
        raise RuntimeError(f"{guardrails.PATH}: {e}") from None
    rails = ", ".join(f"{t}: {'/'.join(r)}" for t, r in sorted(gr["llm_rails"].items()))
    who = f"{res['provider']} {res['model']} ({res['scope']} scope)"
    job["log"].append(f"{now()} {guardrails.PATH}: LLM rails ({rails}) run with {who} through the console's LLM proxy")


ENV_FILES = ("mcp/env.yaml", "mcp/env.yml", "mcp/.env")


async def _env_file(svc: Services, group: str) -> None:
    """§38.1: `RAMEN_*` names are Ramen's (the judge's URL and token, the policy versions): a repo's `mcp/env.yaml`
    (or `.env`) that sets one fails the deploy here, naming the key — the worker applies the file over its config."""
    for path in ENV_FILES:
        raw = await svc.cloud.read_file(group, path)
        if raw is None:
            continue
        sep = "=" if path.endswith(".env") else ":"
        for n, line in enumerate(raw.decode("utf-8", "replace").splitlines(), 1):
            line = line.strip().removeprefix("export ").strip()
            if not line or line.startswith("#") or sep not in line:
                continue
            key = line.split(sep, 1)[0].strip()
            if key.upper().startswith("RAMEN_"):
                raise RuntimeError(f"{path}:{n}: {key} is a Ramen name: RAMEN_* keys come from the console")


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


async def _scan_setup(svc: Services, g: dict, env_name: str, job: dict, group_cfg: dict, org_cfg: dict) -> dict:
    """§30.2, once per deploy: the repo's skill files, the baseline and the provider env; `{"skills": {name: [paths]},
    "baseline", "env", "providers", "on", "llm_error"?}`. No engine with skills in the repo is said once here. §36.2:
    the group's `llm` resolves group → org; set but configured nowhere is the run's error (never a silent skip)."""
    group = g["name"]
    skills = sk.skill_files(await svc.cloud.list_files(group, sk.PATH + "/"))
    sc = {
        "skills": skills,
        "baseline": await sk.load_baseline(svc, group),
        "env": env_name,
        "providers": {},
        "on": True,
    }
    if not sk.image():
        sc["on"] = False
        if skills:
            job["log"].append(f"{now()} {sk.PATH}: scan skipped, no scan engine (RAMEN_SKILLSPECTOR_IMAGE is not set)")
        return sc
    llm = sk.settings(g)["llm"] or None
    try:
        sc["providers"] = llm_scope.resolve(g, group_cfg, org_cfg, "skillspector", llm)["env"]
    except llm_scope.NoProvider as e:
        if llm:
            sc["llm_error"] = str(e)
    return sc


def _gate(job, group, env_name, zone, breaking, stable, cases, cfg, info, svc=None, g=None, ev=None, sc=None):
    """The deploy gate the adapter awaits on the canary (CONTRACTS C4/C5): schema compatibility against the stable
    track's stored manifest, then every golden case, then the evals Job (§29.2), then the scan Job (§30.2). `info`
    collects `compat` / `golden` / `evals` / `skillspector` for the job result."""
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
        for w in (result or {}).get("warnings") or []:  # 1.0.0c: deprecations the worker found, never a refusal
            log_.append(f"{now()} zone {zone}: warning: {w}")
        if (result or {}).get("redis") == "unreachable":  # §38.3 (D85): the node's throttle Redis status
            log_.append(f"{now()} zone {zone}: warning: throttling off: Redis unreachable from the canary")
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
        await _golden(call)
        if ev is not None and ev.get("text") is not None:  # §29.2: after the golden cases, the evals Job
            await _evals(svc, job, group, zone, g, ev, call, cfg.get("RAMEN_EVALS_KEY"), info)
        if sc is not None:  # §30.2: then the scan Job
            await _scan(svc, job, group, zone, g, sc, call, result, info)

    async def _golden(call):
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


async def _evals(svc, job, group, zone, g, ev, call, evals_key, info):
    """§29.2: one promptfoo Job against the canary; `info["evals"]` = counts + the report page's URL, `{"error"}` or
    `{"skipped"}`; the cases go to `job.reports[zone].evals`. Failures and errors fail the deploy only when the group
    gates; otherwise a warning line. §31.4a: the Job calls as `RAMEN_EVALS_KEY` (no policy gate), never as a group
    key."""
    log_ = job["log"]
    if not evals.image():
        info["evals"] = {"skipped": "no evals engine"}
        return
    target = getattr(call, "target", None)
    if not target or not evals_key:  # the adapter addresses the canary only when the group has an MCP key
        log_.append(f"{now()} zone {zone}: evals skipped: no MCP key to reach the canary with")
        info["evals"] = {"skipped": "no MCP key"}
        return
    env = {
        "RAMEN_MCP_URL": evals.mcp_url(target),
        "RAMEN_MCP_KEY": evals_key,
        "RAMEN_GROUP": group,
        "RAMEN_ENV": ev["env"],
        "RAMEN_ZONE": zone,
        **ev["providers"],
    }
    from .tls import require_tls

    worker_ip = target.rsplit(":", 1)[0].strip("[]") if require_tls() else None
    spec = evals.job_spec(
        group, zone, ev["text"], env, image=evals.image(), api_url=evals.api_url() or None, worker_ip=worker_ip
    )
    log_.append(f"{now()} zone {zone}: evals: running {spec['name']} against the canary")
    gate_on = evals.settings(g or {})["gate"]
    try:
        out = await svc.cloud.run_evals_job(group, zone, spec)
        body = evals.extract(out.get("log") or "")
        if body is None:
            tail = imagebuild.tail(out.get("log"), 1)
            raise RuntimeError(f"exit {out.get('exit_code')}: no results" + (f" ({tail[0][:200]})" if tail else ""))
        try:
            parsed = evals.parse_results(body)
        except ValueError as e:  # the file was never written: `cat` complained; promptfoo's own words come first
            raise ValueError(f"{e}; log: {evals.before_markers(out.get('log'))}; {body[:160]}") from None
    except Exception as e:  # noqa: BLE001 - the run's failure is the result, the deploy decides by `gate`
        err = str(e) if isinstance(e, (RuntimeError, ValueError)) else f"{type(e).__name__}: {e}"
        info["evals"] = {"error": err[:300]}
        log_.append(f"{now()} zone {zone}: evals error: {err}")
        if gate_on:
            raise GateError(f"evals error: {err}") from None
        log_.append(f"{now()} zone {zone}: evals: warning, the run failed (reported only; the group does not gate)")
        return
    info["evals"] = evals.result_of(parsed, f"/groups/{group}/evals/{job['id']}/{zone}")
    job.setdefault("reports", {}).setdefault(zone, {})["evals"] = evals.stored_report(parsed)
    log_.append(f"{now()} zone {zone}: evals: {evals.summary(parsed)}")
    for line in evals.log_lines(parsed):
        log_.append(f"{now()} zone {zone}: {line}")
    if parsed["failed"] or parsed["errors"]:
        msg = f"evals: {parsed['failed']} failed, {parsed['errors']} errors"
        if gate_on:
            raise GateError(msg)
        log_.append(f"{now()} zone {zone}: warning: {msg} (reported only; the group does not gate on evals)")


async def _served(call, result: dict | None) -> tuple[list, list]:
    """The canary's `tools/list` and `prompts/list`; the reload result's manifest when the canary answers no list."""
    out = []
    for i, (method, key) in enumerate((("tools/list", "tools"), ("prompts/list", "prompts")), 1):
        items = None
        try:
            r = await call({"jsonrpc": "2.0", "id": i, "method": method, "params": {}})
            items = (r.get("result") or {}).get(key) if isinstance(r, dict) else None
        except Exception:  # noqa: BLE001 - a worker without the method still has a manifest
            items = None
        if not isinstance(items, list) or not items:
            items = (result or {}).get(key) if isinstance(result, dict) else None
        out.append([x for x in (items or []) if isinstance(x, dict)])
    return out[0], out[1]


async def _scan(svc, job, group, zone, g, sc, call, result, info):
    """§30.2: one SkillSpector Job over the repo's skills and the skill synthesized from what the canary serves;
    `info["skillspector"]` = the summary (+ the report page URL), `{"error"}` or `{"skipped"}`; the full report on
    `job["reports"][zone]`. DO_NOT_INSTALL and errors fail the deploy only when the group gates."""
    log_ = job["log"]
    if not sc["on"]:
        if sc["skills"]:
            info["skillspector"] = {"skipped": "no scan engine"}
        return
    if sc.get("llm_error"):  # §36.2: the group named an LLM nobody configures — an error, gated like a run error
        info["skillspector"] = {"error": sc["llm_error"][:300]}
        log_.append(f"{now()} zone {zone}: scan error: {sc['llm_error']}")
        if sk.settings(g or {})["gate"]:
            raise GateError(f"scan error: {sc['llm_error']}")
        log_.append(f"{now()} zone {zone}: scan: warning, not run (reported only; the group does not gate)")
        return
    files: dict[str, bytes] = {}
    served = None
    if call is not None:  # the console's own call (admin key next to the group key, §24.3): no policy gate
        tools, prompts = await _served(call, result)
        served = sk.synthesize_skill(tools, prompts, zone)
    elif not sc["skills"]:
        log_.append(f"{now()} zone {zone}: scan skipped: no MCP key to read the canary's tools with")
        info["skillspector"] = {"skipped": "no MCP key"}
        return
    if served is None and not sc["skills"]:
        info["skillspector"] = {"skipped": "nothing to scan"}
        return
    gate_on = sk.settings(g or {})["gate"]
    url = f"/groups/{group}/scans/{job['id']}/{zone}"
    try:
        for name, paths in sc["skills"].items():
            for p in paths:
                if (raw := await svc.cloud.read_file(group, p)) is not None:
                    files[f"{name}/{p[len(sk.PATH) + 1 + len(name) + 1 :]}"] = raw
        if served is not None:
            files[f"ramen-served-{zone}/SKILL.md"] = served.encode()
        env = {"RAMEN_GROUP": group, "RAMEN_ENV": sc["env"], "RAMEN_ZONE": zone, **sc["providers"]}
        llm = sk.settings(g or {})["llm"] or None
        spec = sk.job_spec(group, zone, files, env, image=sk.image(), llm=llm, baseline=sc["baseline"])
        n = len(files)
        log_.append(f"{now()} zone {zone}: scan: running {spec['name']} over {n} file{'s' if n != 1 else ''}")
        out = await svc.cloud.run_scan_job(group, zone, spec)
        body = sk.extract(out.get("log") or "")
        if body is None:
            tail = imagebuild.tail(out.get("log"), 1)
            raise RuntimeError(f"exit {out.get('exit_code')}: no report" + (f" ({tail[0][:200]})" if tail else ""))
        try:
            parsed = sk.parse_report(body)
        except ValueError as e:
            raise ValueError(f"{e}; log: {evals.before_markers(out.get('log'))}; {body[:160]}") from None
        if parsed.get("error"):
            raise RuntimeError(f"exit {out.get('exit_code')}: {parsed['error']}")
    except Exception as e:  # noqa: BLE001 - the run's failure is the result, the deploy decides by `gate`
        err = str(e) if isinstance(e, (RuntimeError, ValueError)) else f"{type(e).__name__}: {e}"
        info["skillspector"] = {"error": err[:300]}
        log_.append(f"{now()} zone {zone}: scan error: {err}")
        if gate_on:
            raise GateError(f"scan error: {err}") from None
        log_.append(f"{now()} zone {zone}: scan: warning, the run failed (reported only; the group does not gate)")
        return
    info["skillspector"] = sk.result_of(parsed, url)
    job.setdefault("reports", {}).setdefault(zone, {})["skillspector"] = sk.stored_report(parsed)
    log_.append(f"{now()} zone {zone}: scan: {sk.summary(parsed)}")
    for line in sk.log_lines(parsed):
        log_.append(f"{now()} zone {zone}: {line}")
    if parsed["recommendation"] == "DO_NOT_INSTALL":
        msg = f"scan: DO_NOT_INSTALL, score {parsed['score']}/100"
        if gate_on:
            raise GateError(msg)
        log_.append(f"{now()} zone {zone}: warning: {msg} (reported only; the group does not gate on scans)")
    elif parsed["recommendation"] != "SAFE":
        log_.append(f"{now()} zone {zone}: warning: scan: {parsed['recommendation']}, score {parsed['score']}/100")


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
