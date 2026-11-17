import os

from fastapi import APIRouter, Depends, Request

from .. import (
    alerts,
    apps,
    autoscale,
    baseuri,
    compat,
    configs,
    drift,
    evals,
    github_app,
    guardrails,
    imagebuild,
    oauth_server,
    otel,
    regions,
    toolaccess,
)
from .. import deploy as dep
from .. import llm_providers as lp
from .. import mail as mail_mod
from .. import scheduler as scheduler_mod
from .. import skillspector as sk
from ..auth import providers as providers_mod
from ..logview import parse_log
from ..policy import permissions as perm
from ..rbac import Principal, can, require
from ..redact import redact
from ..security import scrub
from .api_admin import masked_env
from .auth_routes import auth_settings, base_url
from .helpers import accounts, backups, svc

r = APIRouter()
viewer, admin, super_ = require("viewer"), require("group_admin"), require("super_admin")


def render(request: Request, name: str, **ctx):
    ctx.setdefault("p", request.state.principal)
    ctx.setdefault("page", name.split(".")[0])
    return request.app.state.templates.TemplateResponse(request, name, ctx)


@r.get("/")
async def dashboard(request: Request, p: Principal = Depends(require("mcp_user"))):
    if p.role == "mcp_user":  # 0.5.92: their only page — which groups they may connect a client to, and how
        # §28.1: a zone on a satellite is reached at that cluster's host
        satellites = [
            (z["id"], z["host"])
            for z in await svc(request).zones()
            if z.get("host") and set(z.get("groups") or []) & set(p.groups)
        ]
        return render(
            request, "mcp_user.html", groups=sorted(p.groups), public_url=base_url(request), satellites=satellites
        )
    return render(request, "dashboard.html")


@r.get("/ui/dashboard")
async def dashboard_grid(request: Request, p: Principal = Depends(viewer)):
    s = svc(request)
    return render(request, "partials/dashboard.html", data=await dep.dashboard(s, await s.visible_groups(p)))


def protocol_stats(live: list[dict]) -> dict:
    """§23.1: what the zone's workers report in `Admin/Metrics` since 1.0.0, summed over the zone — calls per protocol
    version (newest first), open subscription streams, live tasks. None for a key no worker reports (0.8 workers)."""
    calls, counts = None, {"subscriptions": None, "tasks": None}
    for x in live:
        m = x.get("metrics") or {}
        if isinstance(m.get("calls_by_protocol"), dict):
            calls = calls or {}
            for v, n in m["calls_by_protocol"].items():
                if isinstance(n, int):
                    calls[str(v)] = calls.get(str(v), 0) + n
        for k in counts:
            if isinstance(m.get(k), int):
                counts[k] = (counts[k] or 0) + m[k]
    return {"calls_by_protocol": None if calls is None else sorted(calls.items(), reverse=True), **counts}


@r.get("/ui/groups/{group}/zones/{zone}/workers")
async def workers_partial(request: Request, group: str, zone: str, p: Principal = Depends(require("viewer", "group"))):
    s = svc(request)
    w = await s.worker_config(group, zone)
    try:
        w["live"] = await s.cloud.workers(group, zone)
    except Exception as e:  # noqa: BLE001 - shown inline
        w["live"] = [{"id": "?", "load": "down", "metrics": {}, "error": str(e)}]
    canary = [x for x in w["live"] if x.get("track") == "canary"]
    live = w.get("live_state") or {}
    w["canary"] = (
        {"replicas": live.get("canary_replicas", len(canary)), "ready": len([x for x in canary if x["load"] != "down"])}
        if canary or live
        else None
    )
    w["namespace"] = live.get("namespace")
    # B4: every worker of a zone must serve the same manifest (C2 `manifest_hash`; absent on pre-0.7.0 workers)
    hashes = {x.get("metrics", {}).get("manifest_hash") for x in w["live"]} - {None, ""}
    w["manifest_mismatch"] = len(hashes) > 1
    w["protocol"] = protocol_stats(w["live"])
    w["latency"] = autoscale.latency(w["live"])  # §35.1: the zone's last-minute p50/p95 (1.0.0c nodes)
    # §38.3: workers whose throttle Redis did not answer (throttling is off on them); absent on pre-1.0.0c nodes
    w["redis_down"] = [x["id"] for x in w["live"] if (x.get("metrics") or {}).get("redis") == "unreachable"]
    group_admin = can(p, "group_admin", group)  # §21.7: only they see and set the max; others see the count
    return render(
        request,
        "partials/workers.html",
        w=w,
        group=group,
        zone=zone,
        can_scale=group_admin,
        limits=autoscale.limits(),
    )


@r.get("/ui/groups/{group}/zones/{zone}/drift")
async def drift_partial(request: Request, group: str, zone: str, p: Principal = Depends(require("viewer", "group"))):
    """C12: the group page's Drift card for one zone (the API's report, rendered)."""
    s = svc(request)
    text = await s.cloud.logs(group, zone, None, 500)
    return render(request, "partials/drift.html", d=await drift.report(s.store, group, zone, text, 500))


@r.get("/ui/jobs/{jid}")
async def job_partial(request: Request, jid: str, p: Principal = Depends(viewer)):
    from fastapi import HTTPException

    from .api import job_group

    job = await request.app.state.jobs.get(jid)
    if job and not can(p, "viewer", job_group(job)):  # §33.4: as the API — a job is its group's
        raise HTTPException(403, f"Requires Viewer on group {job_group(job)}")
    return render(request, "partials/job.html", job=job)


@r.get("/groups")
async def groups(request: Request, p: Principal = Depends(viewer)):
    return render(request, "groups.html", groups=await svc(request).visible_groups(p))


@r.get("/groups/{group}")
async def group_detail(request: Request, group: str, p: Principal = Depends(require("viewer", "group"))):
    s = svc(request)
    g = await s.get_group(group)
    bound = await s.group_zones(group)  # §26.1: the group sees and uses the zones bound to it
    zones = [z for z in await s.zones() if z["id"] in bound]
    workers = {z["id"]: await s.worker_config(group, z["id"]) for z in zones}
    envs = await s.environments(group)
    used = {z for e in envs for z in e.get("zones") or []}
    can_admin = can(p, "group_admin", group)
    # §31.1: the LLM configs version, who accepted it (admins), and the version each group key was minted under
    lc = await configs.load(s, group)
    mcp_keys = await s.secrets(group, kind="mcp_key")
    for k in mcp_keys:
        kv = k.get("accepted_configs")
        k["configs"] = None if lc is None else (kv or "—") + ("" if kv in (None, lc["version"]) else " (stale)")
    return render(
        request,
        "group.html",
        group={**g, "repo_url": redact(g.get("repo_url") or "")},  # §39.5a N1: a token in the URL is masked
        zone_by={z["id"]: z for z in zones},
        unused_zones=[z for z in bound if z not in used],
        envs=envs,
        kinds=toolaccess.kinds(),
        kind_labels=toolaccess.LABELS,
        kind_hints=toolaccess.HINTS,
        tool_rows={e["name"]: toolaccess.rows(e) for e in envs},
        is_task=compat.is_task,  # §23.5 / §23.2: the `task` and `headers` badges of "Packages per zone"
        header_params=compat.header_params,  # C10: last deploy's tools + restricted names
        known_tools={e["name"]: toolaccess.known_tools(e) for e in envs},
        zones=zones,
        workers=workers,
        limits=autoscale.limits(),  # §35.1: the bounds next to the last decision
        apps_by={e["name"]: await apps.per_zone(request.app.state.jobs, e) for e in envs},  # §33.1 / §33.2
        mcp_keys=mcp_keys,
        configs=lc,
        acceptance=await configs.acceptance(accounts(request), s, group, lc) if lc and can_admin else [],
        images=await s.images(group),
        builds=await s.builds_images(g),  # §24.2
        builder=await imagebuild.builder_available(s.cloud),
        otel=otel.public_config(await otel.get_config(s.store)),  # §24.3
        jobs=await request.app.state.jobs.recent(f"{group}/"),
        can_admin=can_admin,
        permissions=perm.table(),
        requests=[
            q
            for q in await request.app.state.accounts.list_requests(p)
            if q.get("group") == group and q.get("type") == "permission"
        ],
        providers=request.app.state.oauth.providers(),
        public_url=base_url(request),
        signin=await mcp_signin(request, group, envs),
        # §36.2: the group's own LLM providers and what its guardrails / scans may name (group's or org's)
        llm_group=lp.card(await lp.load(s.store, group)) if can_admin else None,
        **(await policy_context(s, group, g) if can_admin else {"llm_choices": []}),
    )


async def policy_context(s, group: str, g: dict) -> dict:
    """D84: what the group page's three policy cards share — the scope-resolved providers as (name, scope) pairs (the
    group's first, then the org's), the ones SkillSpector can use, what evals and each select resolve to, and every
    policy's version (None without the repo file / engine)."""
    from .. import llm_scope

    group_cfg, org_cfg = await lp.load(s.store, group), await lp.load(s.store)
    mine = lp.configured(group_cfg)
    scoped = [(n, "group") for n in mine] + [(n, "org") for n in lp.configured(org_cfg) if n not in mine]
    gg, gs = guardrails.settings(g), sk.settings(g)
    return {
        "llm_scoped": scoped,
        "llm_choices": [n for n, _ in scoped],
        "scan_choices": [(n, sc) for n, sc in scoped if sk.PROVIDER.get(n)],  # D73: no Vertex in SkillSpector
        "evals_resolved": llm_scope.options(group_cfg, org_cfg, None),
        "scan_models": llm_scope.options(group_cfg, org_cfg, gs["llm"]) if gs["llm"] else None,
        "guardrail_models": llm_scope.options(group_cfg, org_cfg, gg["llm"] or None),
        "policy_versions": {
            "evals": (evals.client_info(await evals.load(s, group), g) or {}).get("version"),
            "skillspector": (sk.client_info(await sk.load_baseline(s, group), g) or {}).get("version"),
            "guardrails": (guardrails.policy(await guardrails.load(s, group), g) or {}).get("version"),
        },
    }


async def guardrail_options(s, group: str, llm) -> dict | None:
    """1.0.0c: the models the group's guardrails may name for `llm` (the resolved scope's allowed models)."""
    from .. import llm_scope

    return llm_scope.options(await lp.load(s.store, group), await lp.load(s.store), (llm or "").strip() or None)


@r.get("/ui/groups/{group}/guardrails/models")
async def guardrail_models_partial(
    request: Request,
    group: str,
    llm: str = "",
    kind: str = "guardrails",
    p: Principal = Depends(require("group_admin", "group")),
):
    """A policy card's Model slot, re-rendered when its provider select changes: `kind=guardrails` posts the model,
    `kind=scans` shows the provider's default read-only (D84)."""
    from ..errors import invalid

    if kind not in ("guardrails", "scans"):
        raise invalid("kind must be guardrails or scans")
    s = svc(request)
    g = await s.get_group(group)
    gg = g.get("guardrails") or {}
    if kind != "guardrails" or llm.strip() != (gg.get("llm") or ""):  # another provider: its default, not the saved
        gg = {**gg, "model": ""}
    models = await guardrail_options(s, group, llm) if kind == "guardrails" or llm.strip() else None
    return render(
        request, "partials/guardrail_models.html", group=g, gg=gg, guardrail_models=models, kind=kind, llm=llm
    )


async def mcp_signin(request: Request, group: str, envs: list[dict]) -> dict:
    """The data behind the group page's "How MCP users sign in" card (0.7.5), see `signin_guide`."""
    from . import signin_guide

    zones = sorted({z for e in envs for z in e.get("zones") or []})
    hosts = {z["id"]: z["host"] for z in await svc(request).zones() if z.get("host") and z["id"] in zones}  # §28.1
    return signin_guide.build(
        base_url(request),
        group,
        zones,
        await request.app.state.oauth_server.clients(),
        await auth_settings(request),
        await accounts(request).list_members(group),
        request.app.state.oauth.providers(),
        hosts,
    )


@r.get("/environments")
async def environments(request: Request, p: Principal = Depends(admin)):
    """§27.4: Environments, Zones and Workers — for super admins and group admins; a row the caller may not edit
    (D65: a group not theirs is in it) is read-only."""
    s = svc(request)
    mine = {g["id"] for g in await s.visible_groups(p) if can(p, "group_admin", g["id"])}
    docs = await s.environments()
    full = {}
    for e in docs:
        full.setdefault(e["name"], set()).add(e["group"])
    envs = await s.environments_by_name(p)
    zones = [z for z in await s.zones() if p.role == "super_admin" or mine & set(z.get("groups") or [])]
    has_doc = {(e["group"], e["name"]) for e in docs}
    blocked = await s.blocked_regions()
    home = s.cloud.home_region()
    ck = os.environ.get("RAMEN_CLOUD", "local")
    # §28.0: the console's own cloud offers every region grouped by continent (1.0.0c); only a region that has a
    # cluster — home or a satellite's — is selectable, the others say so
    sat = {c["region"]: c["name"] for c in await s.clusters() if c["provider"] == ck}

    def rows(prov):
        zs = regions.zones(prov, blocked[prov])
        out = []
        for cont, regs in regions.grouped(prov, blocked[prov]):
            items = []
            for reg in regs:
                if not home or prov != ck:
                    items.append({"region": reg, "zones": zs[reg], "cluster": None})
                elif reg == home or reg in sat:
                    items.append({"region": reg, "zones": zs[reg], "cluster": "home" if reg == home else sat[reg]})
                else:
                    items.append({"region": reg, "zones": [], "cluster": None})
            out.append((cont, items))
        return out

    return render(
        request,
        "environments.html",
        envs=envs,
        env_ok={n: all(g in mine for g in gs) for n, gs in full.items()},
        env_opts=[n for n in sorted(full) if all(g in mine for g in full[n])],
        groups=sorted(mine),
        zones=zones,
        zone_ok={z["id"]: all(g in mine for g in z.get("groups") or []) for z in zones},
        pair_envs={
            (g, z["id"]): [n for n in z.get("environments") or [] if (g, n) in has_doc]
            for z in zones
            for g in z.get("groups") or []
        },
        blocked=blocked,
        region_groups={prov: rows(prov) for prov in regions.PROVIDERS},
    )


@r.get("/zones")
async def zones(request: Request):
    from fastapi.responses import RedirectResponse

    from ..baseuri import link

    return RedirectResponse(link(request, "/environments"), 301)  # §27.4: one page since 1.0.0b


@r.get("/secrets")
async def secrets(request: Request, group: str | None = None, p: Principal = Depends(admin)):
    """N18: secrets are for super and group admins; the picker offers the groups the person administers."""
    s = svc(request)
    groups = [g for g in await s.visible_groups(p) if can(p, "group_admin", g["id"])]
    group = group or (groups[0]["id"] if groups else None)
    items = await s.secrets(group) if group and can(p, "group_admin", group) else []
    envs = await s.environments(group) if group else []
    return render(
        request,
        "secrets.html",
        groups=groups,
        group=group,
        secrets=items,
        envs=envs,
        zones=await s.zones(),
        can_admin=bool(group) and can(p, "group_admin", group),
    )


@r.get("/users")
async def users(request: Request, p: Principal = Depends(viewer)):
    """D41/N22: everyone sees the users of their groups, mapped per group and role; admins edit their groups."""
    a = accounts(request)
    users = await a.list_users(p)
    groups = await svc(request).visible_groups(p)
    members = {g["id"]: [u for u in users if g["id"] in u.get("memberships", {})] for g in groups}
    manageable = {u["id"] for u in users if await a.can_manage(p, u)}
    return render(
        request,
        "users.html",
        users=users,
        groups=groups,
        members=members,
        admin_groups=[g["id"] for g in groups if can(p, "group_admin", g["id"])],
        manageable=manageable,
        is_admin=can(p, "group_admin"),
        requests=await a.list_requests(p) if can(p, "group_admin") else [],
    )


@r.get("/api-keys")
async def api_keys(request: Request, p: Principal = Depends(super_)):
    """Groups are picked from the ones the caller may grant, never typed (U8)."""
    return render(
        request,
        "api_keys.html",
        keys=await accounts(request).list_keys(p),
        groups=await svc(request).visible_groups(p),
    )


@r.get("/logs")
async def logs(
    request: Request,
    group: str | None = None,
    zone: str | None = None,
    worker: str | None = None,
    tail: int = 200,
    p: Principal = Depends(viewer),
):
    """Two panes (U4): entries newest first on the left, the selected entry's body on the right."""
    s = svc(request)
    text, entries, live = "", [], []
    if group and zone and can(p, "viewer", group):
        text = scrub(await s.cloud.logs(group, zone, worker or None, tail) or "")  # N1: as the logs API
        entries = parse_log(text, await s.mcp_key_names(group))
        try:
            live = [w["id"] for w in await s.cloud.workers(group, zone) if w.get("id") and w["id"] != "?"]
        except Exception:  # noqa: BLE001 - the selector is a convenience, never a reason to fail the page
            live = []
    return render(
        request,
        "logs.html",
        groups=await s.visible_groups(p),
        zones=await s.zones(),
        group=group,
        zone=zone,
        worker=worker,
        tail=tail,
        text=text,
        entries=entries,
        live_workers=sorted({*live, *([worker] if worker else [])}),
    )


@r.get("/audit")
async def audit(request: Request, limit: int = 100, p: Principal = Depends(super_)):
    """W3 (§14): the newest `limit` rows (100 by default, 500 at most) in a scrollable frame; the search and the
    outcome filter narrow what is already rendered, so neither costs a round trip."""
    import json

    from ..errors import invalid
    from .api_admin import audit as api_audit

    if limit < 1 or limit > 500:
        raise invalid("Limit must be between 1 and 500")
    rows = json.loads((await api_audit(request, limit, None, p)).body)
    return render(request, "audit.html", rows=rows, limit=limit)


@r.get("/ui/audit")
async def audit_rows(request: Request, limit: int = 100, p: Principal = Depends(super_)):
    import json

    from ..errors import invalid
    from .api_admin import audit as api_audit

    if limit < 1 or limit > 500:
        raise invalid("Limit must be between 1 and 500")
    rows = json.loads((await api_audit(request, limit, None, p)).body)
    return render(request, "partials/audit_rows.html", rows=rows)


@r.get("/backups")
async def backups_page(request: Request, p: Principal = Depends(super_)):
    return render(request, "backups.html", backups=await backups(request).list())


@r.get("/config")
async def config(request: Request, p: Principal = Depends(super_)):
    store = svc(request).store
    rules = (await store.get("config", "sa_rules") or {}).get("rules", [])
    blocked = await svc(request).blocked_regions()
    ck = os.environ.get("RAMEN_CLOUD", "local")
    return render(
        request,
        "config.html",
        users=await accounts(request).list_users(p),
        smtp=mail_mod.public_smtp_config(await mail_mod.get_smtp_config(store)),
        notify_user_ids=(await alerts.get_notify_config(store))["user_ids"],
        env=masked_env(),
        config_file=os.environ.get("RAMEN_CONFIG"),
        rules=rules,
        providers=request.app.state.oauth.providers(),
        provider_details=await providers_mod.describe(store),  # D50
        auth=await auth_settings(request),
        oauth_clients=await request.app.state.oauth_server.clients(),
        mail=request.app.state.mailer.backend,
        permissions=perm.table(),
        scheduler=await scheduler_mod.get_config(svc(request).store),
        limits=autoscale.limits(),  # §35.1: the autoscale bounds under the scheduler
        base_uri=await baseuri.load(store),
        public_url_env=os.environ.get("RAMEN_PUBLIC_URL", ""),
        github_app=github_app.public_app_config(await github_app.get_app_config(svc(request).store)),
        regions=regions.REGIONS,
        blocked_regions=blocked,
        region_groups=regions.grouped(ck, blocked[ck]) if ck in regions.PROVIDERS else [],  # 1.0.0c Clusters card
        clusters=await svc(request).clusters(),  # §28.1
        home_region=svc(request).cloud.home_region(),
        regions_note=regions.NOTE,
        oauth_ttl={"access": oauth_server.access_ttl(), "refresh": oauth_server.refresh_ttl()},
        drift={"warn_pct": await drift.threshold(store)},
        otel=otel.public_config(await otel.get_config(store)),
        otel_ui_url=os.environ.get("RAMEN_OTEL_UI_URL", "").strip(),  # §26.3: the chart's trace UI (Jaeger or Phoenix)
        ingest_url=f"{base_url(request)}/otlp",  # §31.4: where pushed telemetry settings point clients at
        llm=lp.card(await lp.load(store)),  # §29.1 / §36.2: the one LLM providers card (1.0.0c)
        evals_ui_url=evals.ui_url(),  # §29.2: the chart's promptfoo server, through the port-forward
        evals_port_forward=evals.PORT_FORWARD,
        evals_image=evals.image(),  # §30.0 "Evals & scans": the engines and images this console runs
        scan_image=sk.image(),
    )


@r.get("/.well-known/ramen-policy")
async def ramen_policy(request: Request, group: str = "", zone: str = ""):
    """§29.3 (public, no secrets): the telemetry and evals policy of one group/zone for the bridge's offline check;
    404 for a group or zone this console does not serve."""
    from ..errors import not_found

    s = svc(request)
    g = await s.store.get("groups", group) if group else None
    if not g or not zone or zone not in await s.group_zones(group):
        raise not_found("group or zone")
    cfg = await otel.get_config(s.store)
    ev = evals.client_info(await evals.load(s, group), g)
    scan = sk.client_info(await sk.load_baseline(s, group), g)
    out = {
        "telemetry": {"version": otel.telemetry_version(cfg), "required": bool(g.get("require_telemetry_consent"))},
        "evals": {"version": ev["version"], "required": ev["required"]} if ev else None,
    }
    if scan:  # §30.3: absent when the engine is off and the group never required it (nothing to say)
        out["skillspector"] = {"version": scan["version"], "required": scan["required"]}
    if (lc := configs.policy(await configs.load(s, group))) is not None:
        out["configs"] = lc  # §31.1: absent without the directory
    if (gr := guardrails.policy(await guardrails.load(s, group), g)) is not None:
        out["guardrails"] = gr  # §36.3: absent without mcp/guardrails.yaml
    return out


@r.get("/groups/{group}/scans/{jid}/{zone}")
async def scan_report(
    request: Request, group: str, jid: str, zone: str, p: Principal = Depends(require("viewer", "group"))
):
    """§30.2: the full SkillSpector report of one deploy job's zone — a table per skill (rule, severity, file:line,
    message); 404 for a job of another group, or a zone it did not scan."""
    from ..errors import not_found

    job = await request.app.state.jobs.get(jid)
    rep = ((job or {}).get("reports") or {}).get(zone, {}).get("skillspector") if job else None
    if not job or not job.get("target", "").startswith(f"{group}/") or not rep:
        raise not_found("scan report")
    return render(request, "scan_report.html", group=group, zone=zone, job=job, report=rep, summary=sk.summary(rep))


@r.get("/groups/{group}/evals/{jid}/{zone}")
async def evals_report(
    request: Request, group: str, jid: str, zone: str, p: Principal = Depends(require("viewer", "group"))
):
    """§29.2: one deploy job's evals run on one zone — every case (pass / fail / error, what was sent, what came back,
    why it failed) and, when the chart runs promptfoo's UI, the run's address there through a port-forward; 404 for a
    job of another group, or a zone it did not run."""
    from ..errors import not_found

    job = await request.app.state.jobs.get(jid)
    rep = ((job or {}).get("reports") or {}).get(zone, {}).get("evals") if job else None
    if not job or not job.get("target", "").startswith(f"{group}/") or not rep:
        raise not_found("evals report")
    return render(
        request,
        "evals_report.html",
        group=group,
        zone=zone,
        job=job,
        report=rep,
        summary=evals.summary(rep),
        run_ui=evals.run_ui(rep.get("id")),
        port_forward=evals.PORT_FORWARD,
    )


@r.get("/groups/{group}/apps/{zone}/{name}")
async def app_preview(
    request: Request, group: str, zone: str, name: str, p: Principal = Depends(require("viewer", "group"))
):
    """§33.1: one MCP app of a zone, read through the zone's worker with the group's key and shown in a sandboxed
    iframe under the app's CSP (`_meta["ui/csp"]`, else the strict default); 404 for a name the last deploy did not
    report as an app, 403 for a non-member (the dependency), no cookies reach the app."""
    from ..errors import ApiError

    s = svc(request)
    g = await s.get_group(group)
    try:
        doc, csp = await apps.read_app(s, request.app.state.jobs, group, g, zone, name)
    except apps.AppError as e:
        raise ApiError(e.status, str(e)) from None
    resp = render(request, "app_preview.html", group=group, zone=zone, app=name, doc=doc, csp=csp)
    resp.headers["Content-Security-Policy"] = apps.META_FRAME
    resp.headers["Cache-Control"] = "no-store"
    resp.headers["Referrer-Policy"] = "no-referrer"
    return resp
