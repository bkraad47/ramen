import os

from fastapi import APIRouter, Depends, Request

from .. import alerts, baseuri, github_app
from .. import deploy as dep
from .. import mail as mail_mod
from .. import scheduler as scheduler_mod
from ..logview import parse_log
from ..policy import permissions as perm
from ..rbac import Principal, can, require
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
        return render(request, "mcp_user.html", groups=sorted(p.groups), public_url=base_url(request))
    return render(request, "dashboard.html")


@r.get("/ui/dashboard")
async def dashboard_grid(request: Request, p: Principal = Depends(viewer)):
    s = svc(request)
    return render(request, "partials/dashboard.html", data=await dep.dashboard(s, await s.visible_groups(p)))


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
    return render(request, "partials/workers.html", w=w)


@r.get("/ui/jobs/{jid}")
async def job_partial(request: Request, jid: str, p: Principal = Depends(viewer)):
    return render(request, "partials/job.html", job=request.app.state.jobs.get(jid))


@r.get("/groups")
async def groups(request: Request, p: Principal = Depends(viewer)):
    return render(request, "groups.html", groups=await svc(request).visible_groups(p))


@r.get("/groups/{group}")
async def group_detail(request: Request, group: str, p: Principal = Depends(require("viewer", "group"))):
    s = svc(request)
    g = await s.get_group(group)
    zones = await s.zones()
    workers = {z["id"]: await s.worker_config(group, z["id"]) for z in zones}
    return render(
        request,
        "group.html",
        group=g,
        envs=await s.environments(group),
        zones=zones,
        workers=workers,
        mcp_keys=await s.secrets(group, kind="mcp_key"),
        images=await s.images(group),
        jobs=request.app.state.jobs.recent(f"{group}/"),
        can_admin=can(p, "group_admin", group),
        permissions=perm.table(),
        requests=[
            q
            for q in await request.app.state.accounts.list_requests(p)
            if q.get("group") == group and q.get("type") == "permission"
        ],
        providers=request.app.state.oauth.providers(),
        public_url=base_url(request),
    )


@r.get("/environments")
async def environments(request: Request, p: Principal = Depends(viewer)):
    s = svc(request)
    envs = [e for e in await s.environments() if can(p, "viewer", e["group"])]
    return render(request, "environments.html", envs=envs, groups=await s.visible_groups(p), zones=await s.zones())


@r.get("/zones")
async def zones(request: Request, p: Principal = Depends(viewer)):
    return render(request, "zones.html", zones=await svc(request).zones(), groups=await svc(request).visible_groups(p))


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
        text = await s.cloud.logs(group, zone, worker or None, tail)
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
        auth=await auth_settings(request),
        oauth_clients=await request.app.state.oauth_server.clients(),
        mail=request.app.state.mailer.backend,
        permissions=perm.table(),
        scheduler=await scheduler_mod.get_config(svc(request).store),
        base_uri=await baseuri.load(store),
        public_url_env=os.environ.get("RAMEN_PUBLIC_URL", ""),
        github_app=github_app.public_app_config(await github_app.get_app_config(svc(request).store)),
    )
