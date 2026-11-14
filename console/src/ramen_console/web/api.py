from fastapi import APIRouter, BackgroundTasks, Depends, Request, Response
from fastapi.responses import PlainTextResponse

from .. import deploy as dep
from .. import drift, toolaccess
from ..audit import note, write_audit
from ..baseuri import prefix
from ..errors import conflict, invalid, not_found
from ..rbac import Principal, can, require
from ..util import now
from . import models as m
from .helpers import accounts, is_hx, respond, svc, tabular

r = APIRouter(prefix="/api/v1")
viewer, viewer_g = require("viewer"), require("viewer", "group")
admin, admin_g, super_ = require("group_admin"), require("group_admin", "group"), require("super_admin")


@r.get("/me")
async def me(p: Principal = Depends(require("mcp_user"))):
    return p.to_dict()


@r.get("/groups")
async def groups(request: Request, format: str | None = None, p: Principal = Depends(viewer)):
    return tabular(await svc(request).visible_groups(p), format, "groups")


@r.post("/groups", status_code=201)
async def create_group(request: Request, body: m.GroupIn, p: Principal = Depends(super_)):
    note(request, "group.create", body.name, [f"group:{body.name}"])
    return respond(
        request,
        await svc(request).create_group(
            body.name, body.repo_url, body.ref, p.name, body.github_token, body.github_app_installation_id
        ),
        201,
    )


@r.get("/groups/{group}")
async def get_group(request: Request, group: str, p: Principal = Depends(viewer_g)):
    from ..util import public

    return public(await svc(request).get_group(group))


@r.put("/groups/{group}")
async def update_group(request: Request, group: str, body: m.GroupUpdate, p: Principal = Depends(admin_g)):
    tags = [f"{k}:{v}" for k in ("build_image", "require_telemetry_consent") if (v := getattr(body, k)) is not None]
    note(request, "group.update", group, [f"group:{group}", *tags])
    return respond(request, await svc(request).update_group(group, **body.model_dump()))


@r.get("/groups/{group}/zones")
async def group_zones(request: Request, group: str, p: Principal = Depends(viewer_g)):
    """§26.1: the group's zones are the zones listing it (read-only; a super admin binds on the Zones page)."""
    return {"zones": await svc(request).group_zones(group)}


@r.delete("/groups/{group}")
async def delete_group(request: Request, group: str, p: Principal = Depends(super_)):
    note(request, "group.delete", group, [f"group:{group}"])
    await svc(request).delete_group(group)
    return respond(request, {"ok": True})


@r.put("/groups/{group}/sa-restrictions")
async def sa_restrictions(request: Request, group: str, body: m.Rules, p: Principal = Depends(admin_g)):
    note(request, "group.sa_restrictions", group, [f"group:{group}"])
    return respond(request, await svc(request).set_sa_restrictions(group, body.rules))


# --- per-group worker images (§13.3, F9.3): the console records and recalls references, it never builds ----------
@r.get("/groups/{group}/images")
async def images(request: Request, group: str, format: str | None = None, p: Principal = Depends(viewer_g)):
    return tabular(await svc(request).images(group), format, "images")


@r.post("/groups/{group}/images", status_code=201)
async def record_image(request: Request, group: str, body: m.ImageIn, p: Principal = Depends(super_)):
    note(request, "image.record", f"{group}:{body.tag}", [f"group:{group}"])
    doc = await svc(request).record_image(group, body.tag, body.digest, body.note, p.name)
    return respond(request, doc, 201)


@r.put("/groups/{group}/images/current")
async def recall_image(request: Request, group: str, body: m.ImageRecall, p: Principal = Depends(super_)):
    note(request, "image.recall", f"{group}:{body.id}", [f"group:{group}"])
    return respond(request, await svc(request).recall_image(group, body.id))


@r.delete("/groups/{group}/images/current")
async def unpin_image(request: Request, group: str, p: Principal = Depends(super_)):
    note(request, "image.unpin", group, [f"group:{group}"])
    return respond(request, await svc(request).unpin_image(group))


@r.get("/groups/{group}/mcp-keys")
async def mcp_keys(request: Request, group: str, p: Principal = Depends(viewer_g)):
    return await svc(request).secrets(group, kind="mcp_key")


@r.post("/groups/{group}/mcp-keys", status_code=201)
async def mint_mcp_key(request: Request, group: str, body: m.Named, p: Principal = Depends(admin_g)):
    note(request, "mcp_key.create", f"{group}/{body.name}", [f"group:{group}"])
    doc = await svc(request).mint_mcp_key(group, body.name, p.name)
    return respond(request, doc, 201, hx_html=_once("MCP key", doc["key"]))


@r.delete("/groups/{group}/mcp-keys/{sid}")
async def delete_mcp_key(request: Request, group: str, sid: str, p: Principal = Depends(admin_g)):
    note(request, "mcp_key.delete", f"{group}/{sid}", [f"group:{group}"])
    await svc(request).delete_secret(group, sid, kind="mcp_key")
    return respond(request, {"ok": True})


@r.get("/zones")
async def zones(request: Request, format: str | None = None, p: Principal = Depends(viewer)):
    return tabular(await svc(request).zones(), format, "zones")


@r.get("/zones/{zone}")
async def get_zone(request: Request, zone: str, p: Principal = Depends(viewer)):
    z = await svc(request).store.get("zones", zone)
    if not z:
        raise not_found("zone")
    return respond(request, z)


def _ip(request: Request) -> str:
    from ..audit import client_ip

    return client_ip(request)


async def _deploy_pairs(request: Request, bg: BackgroundTasks, p: Principal, pairs) -> list[str]:
    """§27.3: a pair that appeared is deployed at once — one canary job per `<g>/<env>` over the zones that appeared.
    A running deploy of the same target skips the pair's job with the error on it."""
    s, jobs, ip = svc(request), request.app.state.jobs, _ip(request)
    by: dict[tuple[str, str], list[str]] = {}
    for g, env, z in pairs:
        by.setdefault((g, env), []).append(z)

    async def audit(action, target, ok, tags):
        await write_audit(s.store, user=p.name, ip=ip, action=action, target=target, ok=ok, tags=tags)

    ids = []
    for (g, env), zones in by.items():
        target = f"{g}/{env}"
        running = [j for j in await jobs.recent(target) if j["target"] == target and j["status"] == "running"]
        job = await jobs.create("deploy", target)
        if running:
            job.update(status="error", error="a deploy is already running", finished=now())
            await jobs.save(job)
        else:
            await audit("deploy.start", target, True, [f"group:{g}", "canary:True", "trigger:bind", *zones])
            bg.add_task(dep.run_deploy, s, job, g, env, zones, True, audit, False, p.name, ip)
        ids.append(job["id"])
    return ids


def _no_content(request: Request) -> Response:
    return respond(request, {"ok": True}) if is_hx(request) else Response(status_code=204)


@r.post("/zones", status_code=201)
async def create_zone(request: Request, body: m.ZoneIn, bg: BackgroundTasks, p: Principal = Depends(admin)):
    """§27.2: a zone names its environments; every derived (group, zone) pair is bound and deployed."""
    note(request, "zone.create", body.name, [f"zone:{body.name}"] + [f"env:{e}" for e in body.environments])
    z, pairs = await svc(request).create_zone(body.name, body.provider, body.region, body.environments, p)
    return respond(request, {**z, "jobs": await _deploy_pairs(request, bg, p, pairs)}, 201)


@r.put("/zones/{zone}")
async def update_zone(
    request: Request, zone: str, body: m.ZoneUpdate, bg: BackgroundTasks, p: Principal = Depends(admin)
):
    """§27.2: `environments` replaces the set (bind + deploy / detach); `region` moves every pair (D66)."""
    before = await svc(request).store.get("zones", zone) or {}
    tags = [f"zone:{zone}"]
    if body.region is not None and body.region != before.get("region"):
        tags.append(f"region:{body.region}")
    if body.environments is not None:  # a tag per environment that appears or goes
        tags += [f"env:{e}" for e in sorted(set(body.environments) ^ set(before.get("environments") or []))]
    note(request, "zone.update", zone, tags)
    z, pairs = await svc(request).update_zone(zone, body.region, body.environments, body.provider, p)
    return respond(request, {**z, "jobs": await _deploy_pairs(request, bg, p, pairs)})


@r.delete("/zones/{zone}")
async def delete_zone(request: Request, zone: str, p: Principal = Depends(admin)):
    note(request, "zone.delete", zone, [f"zone:{zone}"])
    await svc(request).delete_zone(zone, p)
    return respond(request, {"ok": True})


@r.get("/environments/by-name")
async def environments_by_name(request: Request, p: Principal = Depends(viewer)):
    return await svc(request).environments_by_name(p)


@r.post("/environments", status_code=201)
async def create_environment(request: Request, body: m.EnvByNameIn, p: Principal = Depends(admin)):
    """§27.2: one environment name across groups; a doc per group, zones none yet."""
    note(request, "environment.create", body.name, [f"group:{g}" for g in body.groups])
    obj, _ = await svc(request).create_environment(body.name, body.groups, body.ref, p)
    return respond(request, obj, 201)


@r.put("/environments/{name}")
async def update_environment(
    request: Request, name: str, body: m.EnvByNameUpdate, bg: BackgroundTasks, p: Principal = Depends(admin)
):
    note(request, "environment.update", name, [f"group:{g}" for g in body.groups or []])
    obj, pairs = await svc(request).update_environment(name, body.ref, body.groups, p)
    return respond(request, {**obj, "jobs": await _deploy_pairs(request, bg, p, pairs)})


@r.delete("/environments/{name}", status_code=204)
async def delete_environment(request: Request, name: str, p: Principal = Depends(admin)):
    note(request, "environment.delete", name, [f"env:{name}"])
    await svc(request).delete_environment(name, p)
    return _no_content(request)


@r.get("/environments")
async def environments(
    request: Request, group: str | None = None, format: str | None = None, p: Principal = Depends(viewer)
):
    envs = [e for e in await svc(request).environments(group) if can(p, "viewer", e["group"])]
    return tabular(envs, format, "environments")


@r.post("/groups/{group}/environments", status_code=201)
async def create_env(request: Request, group: str, body: m.EnvIn, bg: BackgroundTasks, p: Principal = Depends(admin_g)):
    note(request, "environment.create", f"{group}/{body.name}", [f"group:{group}"])
    doc, pairs = await svc(request).create_env(group, body.name, body.ref, body.zones, p)
    return respond(request, {**doc, "jobs": await _deploy_pairs(request, bg, p, pairs)}, 201)


@r.get("/groups/{group}/environments/{env}")
async def get_environment(request: Request, group: str, env: str, p: Principal = Depends(viewer_g)):
    return respond(request, await svc(request).get_env(group, env))


@r.put("/groups/{group}/environments/{env}")
async def update_env(
    request: Request, group: str, env: str, body: m.EnvUpdate, bg: BackgroundTasks, p: Principal = Depends(admin_g)
):
    note(request, "environment.update", f"{group}/{env}", [f"group:{group}"])
    s, ids = svc(request), []
    if body.zones is not None:  # §27.2: attach or detach this environment on those zones
        _, pairs = await s.set_env_zones(group, env, body.zones, p)
        ids = await _deploy_pairs(request, bg, p, pairs)
    doc = await s.update_env(group, env, **body.model_dump(exclude={"zones"}))
    return respond(request, {**doc, "jobs": ids})


@r.post("/groups/{group}/environments/{env}/verbose")
async def set_verbose(request: Request, group: str, env: str, body: m.Verbose, p: Principal = Depends(admin_g)):
    note(request, "environment.verbose", f"{group}/{env}", [f"group:{group}", f"verbose:{body.verbose}"])
    return respond(request, await svc(request).update_env(group, env, verbose=body.verbose))


@r.put("/groups/{group}/environments/{env}/blocked")
async def set_blocked(request: Request, group: str, env: str, body: m.Blocked, p: Principal = Depends(admin_g)):
    """Blocked tools/resources/prompts (F5.6); applied to workers by the next deploy as RAMEN_BLOCKED."""
    note(request, "environment.blocked", f"{group}/{env}", [f"group:{group}"] + [f"blocked:{n}" for n in body.blocked])
    return respond(request, await svc(request).update_env(group, env, blocked=body.blocked))


@r.put("/groups/{group}/environments/{env}/tool-access")
async def set_tool_access(request: Request, group: str, env: str, body: m.ToolAccess, p: Principal = Depends(admin_g)):
    """C10: who may list and call each tool, by caller kind; the whole map every time. The next deploy hands the
    workers `RAMEN_TOOL_ACCESS`."""
    try:
        access = toolaccess.clean(body.tool_access)
    except ValueError as e:
        raise invalid(str(e)) from e
    note(request, "environment.tool_access", f"{group}/{env}", [f"group:{group}"] + [f"tool:{n}" for n in access])
    s = svc(request)
    e = await s.get_env(group, env)
    e["tool_access"] = access
    return respond(request, await s.store.put("environments", e["id"], e))


@r.put("/groups/{group}/environments/{env}/zones/{zone}/blocked")
async def set_zone_blocked(
    request: Request, group: str, env: str, zone: str, body: m.Blocked, p: Principal = Depends(admin_g)
):
    """Per-zone blocking (U5). Adds to the environment-wide list; the next deploy writes the union of both into
    that zone's `RAMEN_BLOCKED`, so a language model on this zone stops seeing those names."""
    note(
        request,
        "environment.blocked_zone",
        f"{group}/{env}/{zone}",
        [f"group:{group}", f"zone:{zone}"] + [f"blocked:{n}" for n in body.blocked],
    )
    return respond(request, await svc(request).set_zone_blocked(group, env, zone, body.blocked))


@r.delete("/groups/{group}/environments/{env}")
async def delete_env(request: Request, group: str, env: str, p: Principal = Depends(admin_g)):
    note(request, "environment.delete", f"{group}/{env}", [f"group:{group}"])
    await svc(request).delete_env(group, env, p)
    return respond(request, {"ok": True})


@r.post("/groups/{group}/environments/{env}/deploy", status_code=202)
async def deploy(
    request: Request, group: str, env: str, body: m.DeployIn, bg: BackgroundTasks, p: Principal = Depends(admin_g)
):
    s = svc(request)
    e = await s.get_env(group, env)
    if body.zone and body.zone not in e.get("zones", []):
        raise invalid(f"Zone {body.zone} is not attached to {env}")
    target = f"{group}/{env}"
    recent = await request.app.state.jobs.recent(target)
    running = [j for j in recent if j["target"] == target and j["status"] == "running"]
    if running:  # 0.8.0 SC5: two deploys would drive one canary Deployment and race `last_deploy`
        raise conflict(f"A deploy of {target} is already running (job {running[0]['id']}); wait for it to finish")
    tags = [f"group:{group}", f"canary:{body.canary}"] + ([f"breaking:{body.breaking}"] if body.breaking else [])
    note(request, "deploy.start", target, tags)
    job = await request.app.state.jobs.create("deploy", target)
    ip = _ip(request)

    async def audit(action, target, ok, tags):
        await write_audit(s.store, user=p.name, ip=ip, action=action, target=target, ok=ok, tags=tags)

    bg.add_task(dep.run_deploy, s, job, group, env, body.zone, body.canary, audit, body.breaking, p.name, ip)
    html = request.app.state.templates.get_template("partials/job.html").render(job=job, base=prefix(request))
    return respond(request, job, 202, hx_html=html)


@r.get("/jobs/{jid}")
async def job(request: Request, jid: str, p: Principal = Depends(viewer)):
    j = await request.app.state.jobs.get(jid)
    if not j:
        raise not_found("job")
    return j


def _admin_or_refuse(p: Principal, group: str) -> None:
    """Scaling is for super admins and the group's admins; viewers reach the handler so the refusal is audited under
    the scaling action itself (§21.7) rather than as a bare route."""
    from fastapi import HTTPException

    if not can(p, "group_admin", group):
        raise HTTPException(403, f"Requires Group Admin on group {group}")


@r.get("/groups/{group}/zones/{zone}/workers")
async def workers(request: Request, group: str, zone: str, p: Principal = Depends(viewer_g)):
    from ..util import public

    s = svc(request)
    cfg = public(await s.worker_config(group, zone))
    return {**cfg, "live": await s.cloud.workers(group, zone)}


@r.put("/groups/{group}/zones/{zone}/workers")
async def set_workers(request: Request, group: str, zone: str, body: m.WorkersIn, p: Principal = Depends(viewer_g)):
    """§21.7: `max_count` (min is always 1), a manual `count` inside [1, max], sizes. Each is audited with from/to,
    a refused attempt too (under the same action, `error:forbidden`), so the audit page's filter finds it."""
    s = svc(request)
    allowed = can(p, "group_admin", group)
    # a refused caller never reaches the zone lookup, so a viewer cannot learn which zones exist (403, never 404)
    before = await s.worker_config(group, zone) if allowed else None
    tags = [f"group:{group}", f"zone:{zone}"]
    if body.max_count is not None:
        old = [f"from:{before.get('max_count', before.get('count', 1))}"] if before else []
        note(request, "workers.max_count", f"{group}/{zone}", [*tags, *old, f"to:{body.max_count}"])
    elif body.count is not None:
        old = [f"from:{before.get('count', 1)}"] if before else []
        note(request, "workers.count", f"{group}/{zone}", [*tags, *old, f"to:{body.count}"])
    elif body.sleep_after_secs is not None:  # §24.1
        old = [f"from:{before.get('sleep_after_secs', 0)}"] if before else []
        note(request, "workers.sleep_after", f"{group}/{zone}", [*tags, *old, f"to:{body.sleep_after_secs}"])
    else:
        note(request, "workers.scale", f"{group}/{zone}", tags)
    _admin_or_refuse(p, group)
    from ..audit import client_ip
    from ..util import public

    doc = await s.set_workers(
        group,
        zone,
        p,
        body.count,
        body.size,
        body.allowed_sizes,
        body.max_count,
        body.sleep_after_secs,
        client_ip(request),
    )
    return respond(request, public(doc))


@r.post("/groups/{group}/zones/{zone}/sleep")
async def sleep_zone(request: Request, group: str, zone: str, p: Principal = Depends(viewer_g)):
    """§24.1: put the zone to sleep now (activator up, workers to 0). Same roles as the setting."""
    from ..util import public

    note(request, "workers.sleep", f"{group}/{zone}", [f"group:{group}", f"zone:{zone}", "trigger:manual"])
    _admin_or_refuse(p, group)
    return respond(request, public(await svc(request).sleep(group, zone, "manual")))


@r.post("/groups/{group}/zones/{zone}/wake")
async def wake_zone(request: Request, group: str, zone: str, p: Principal = Depends(viewer_g)):
    from ..util import public

    note(request, "workers.wake", f"{group}/{zone}", [f"group:{group}", f"zone:{zone}", "trigger:manual"])
    _admin_or_refuse(p, group)
    return respond(request, public(await svc(request).wake(group, zone, "manual")))


@r.post("/groups/{group}/zones/{zone}/rebalance")
async def rebalance(request: Request, group: str, zone: str, p: Principal = Depends(viewer_g)):
    note(request, "workers.rebalance", f"{group}/{zone}", [f"group:{group}", f"zone:{zone}"])
    _admin_or_refuse(p, group)
    from ..audit import client_ip

    return respond(request, await svc(request).rebalance(group, zone, user=p.name, ip=client_ip(request)))


@r.put("/groups/{group}/zones/{zone}/ip-rules")
async def ip_rules(request: Request, group: str, zone: str, body: m.Cidrs, p: Principal = Depends(admin_g)):
    note(request, "ip_rules", f"{group}/{zone}", [f"group:{group}"])
    return respond(request, await svc(request).set_ip_rules(group, zone, body.cidrs))


@r.put("/groups/{group}/zones/{zone}/item-throttle")
async def item_throttle(request: Request, group: str, zone: str, body: m.ThrottleIn, p: Principal = Depends(admin_g)):
    """N7: zone-local Redis throttling repeat calls to the same tool/resource/prompt."""
    note(request, "throttle.item", f"{group}/{zone}", [f"group:{group}", f"zone:{zone}"])
    return respond(request, await svc(request).set_item_throttle(group, zone, **body.model_dump()))


@r.put("/groups/{group}/throttle")
async def scope_throttle(request: Request, group: str, body: m.ThrottleIn, p: Principal = Depends(admin_g)):
    """N7: one Redis shared by every zone of the group, throttling repeat calls to the group/environment."""
    note(request, "throttle.scope", group, [f"group:{group}"])
    return respond(request, await svc(request).set_scope_throttle(group, **body.model_dump()))


@r.post("/groups/{group}/zones/{zone}/service-account")
async def service_account(request: Request, group: str, zone: str, p: Principal = Depends(super_)):
    note(request, "service_account.create", f"{group}/{zone}", [f"group:{group}"])
    s = svc(request)
    sa = await s.cloud.create_service_account(group, zone)
    w = await s.worker_config(group, zone)
    w["service_account"] = sa.get("name")
    await s.store.put("workers", w["id"], w)
    return respond(request, sa)


@r.delete("/groups/{group}/zones/{zone}/permissions/{permission}")
async def revoke_permission(request: Request, group: str, zone: str, permission: str, p: Principal = Depends(super_)):
    """§13.2: revoke one granted service-account permission; the adapter re-applies the remaining set."""
    note(request, "permission.revoke", f"{group}/{zone}:{permission}", [f"group:{group}", f"permission:{permission}"])
    return respond(request, await svc(request).revoke_sa_permission(group, zone, permission))


@r.get("/groups/{group}/members")
async def members(request: Request, group: str, p: Principal = Depends(viewer_g)):
    return await accounts(request).list_members(group)


@r.post("/groups/{group}/members")
async def add_member(request: Request, group: str, body: m.MemberAdd, p: Principal = Depends(admin_g)):
    found = await accounts(request).store.list("users", {"email": body.email.strip().lower()})
    if not found:
        from ..errors import not_found

        raise not_found("user")
    note(
        request,
        "member.set",
        f"{group}/{found[0]['id']}",
        [f"group:{group}", f"role:{body.role}", f"user:{found[0]['id']}"],
    )
    return respond(request, await accounts(request).set_membership(p, group, found[0]["id"], body.role))


@r.put("/groups/{group}/members/{uid}")
async def set_member(request: Request, group: str, uid: str, body: m.MemberIn, p: Principal = Depends(admin_g)):
    """D41/R4: a person's role in this group. Group admins grant viewer or mcp_user; super admins any group role."""
    note(request, "member.set", f"{group}/{uid}", [f"group:{group}", f"role:{body.role}", f"user:{uid}"])
    return respond(request, await accounts(request).set_membership(p, group, uid, body.role))


@r.delete("/groups/{group}/members/{uid}")
async def remove_member(request: Request, group: str, uid: str, p: Principal = Depends(admin_g)):
    note(request, "member.remove", f"{group}/{uid}", [f"group:{group}", f"user:{uid}"])
    return respond(request, await accounts(request).set_membership(p, group, uid, None))


@r.get("/groups/{group}/secrets")
async def secrets(
    request: Request,
    group: str,
    env: str | None = None,
    zone: str | None = None,
    format: str | None = None,
    p: Principal = Depends(admin_g),
):
    return tabular(await svc(request).secrets(group, env, zone), format, "secrets")


@r.post("/groups/{group}/secrets", status_code=201)
async def add_secret(request: Request, group: str, body: m.SecretIn, p: Principal = Depends(admin_g)):
    note(request, "secret.create", f"{group}/{body.name}", [f"group:{group}"])
    return respond(
        request, await svc(request).add_secret(group, body.name, body.value, body.env, body.zone, p.name), 201
    )


@r.delete("/groups/{group}/secrets/{sid}")
async def delete_secret(request: Request, group: str, sid: str, p: Principal = Depends(admin_g)):
    note(request, "secret.delete", f"{group}/{sid}", [f"group:{group}"])
    await svc(request).delete_secret(group, sid)
    return respond(request, {"ok": True})


@r.get("/dashboard")
async def dashboard(request: Request, p: Principal = Depends(viewer)):
    s = svc(request)
    return await dep.dashboard(s, await s.visible_groups(p))


@r.get("/logs")
async def logs(
    request: Request,
    group: str,
    zone: str,
    worker: str | None = None,
    tail: int = 500,
    download: int = 0,
    p: Principal = Depends(viewer),
):
    if not can(p, "viewer", group):
        from ..errors import forbidden

        raise forbidden(f"No access to group {group}")
    s = svc(request)
    await s.get_group(group)
    text = await s.cloud.logs(group, zone, worker, tail)
    headers = {"Content-Disposition": f'attachment; filename="{group}-{zone}.log"'} if download else {}
    return PlainTextResponse(text, headers=headers)


@r.get("/groups/{group}/zones/{zone}/drift")
async def zone_drift(request: Request, group: str, zone: str, tail: int = 500, p: Principal = Depends(viewer_g)):
    """C12: repeat rate per tool over the zone's last `tail` log lines, plus the warning threshold."""
    if tail < 1:
        raise invalid("Tail must be at least 1")
    s = svc(request)
    await s.get_group(group)
    return await drift.report(s.store, group, zone, await s.cloud.logs(group, zone, None, tail), tail)


def _once(label: str, value: str) -> str:
    from markupsafe import escape

    # data-hold-secs: the page keeps this on screen for a minute, counting down, then reloads (0.7.5)
    return (
        f'<div class="once" data-hold-secs="60"><strong>{label} (shown once — copy it now):</strong> '
        f"<code>{escape(value)}</code></div>"
    )
