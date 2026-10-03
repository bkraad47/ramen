from fastapi import APIRouter, BackgroundTasks, Depends, Request
from fastapi.responses import PlainTextResponse

from .. import deploy as dep
from ..audit import note, write_audit
from ..errors import invalid, not_found
from ..rbac import Principal, can, require
from . import models as m
from .helpers import respond, svc, tabular

r = APIRouter(prefix="/api/v1")
viewer, viewer_g = require("viewer"), require("viewer", "group")
admin_g, super_ = require("group_admin", "group"), require("super_admin")


@r.get("/me")
async def me(p: Principal = Depends(viewer)):
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
    note(request, "group.update", group, [f"group:{group}"])
    return respond(request, await svc(request).update_group(group, **body.model_dump()))


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


@r.post("/zones", status_code=201)
async def create_zone(request: Request, body: m.ZoneIn, p: Principal = Depends(super_)):
    note(request, "zone.create", body.name, [f"zone:{body.name}"])
    return respond(request, await svc(request).create_zone(body.name, body.provider, body.region), 201)


@r.delete("/zones/{zone}")
async def delete_zone(request: Request, zone: str, p: Principal = Depends(super_)):
    note(request, "zone.delete", zone, [f"zone:{zone}"])
    await svc(request).delete_zone(zone)
    return respond(request, {"ok": True})


@r.get("/environments")
async def environments(
    request: Request, group: str | None = None, format: str | None = None, p: Principal = Depends(viewer)
):
    envs = [e for e in await svc(request).environments(group) if can(p, "viewer", e["group"])]
    return tabular(envs, format, "environments")


@r.post("/groups/{group}/environments", status_code=201)
async def create_env(request: Request, group: str, body: m.EnvIn, p: Principal = Depends(admin_g)):
    note(request, "environment.create", f"{group}/{body.name}", [f"group:{group}"])
    return respond(request, await svc(request).create_env(group, body.name, body.ref, body.zones), 201)


@r.get("/groups/{group}/environments/{env}")
async def get_environment(request: Request, group: str, env: str, p: Principal = Depends(viewer_g)):
    return respond(request, await svc(request).get_env(group, env))


@r.put("/groups/{group}/environments/{env}")
async def update_env(request: Request, group: str, env: str, body: m.EnvUpdate, p: Principal = Depends(admin_g)):
    note(request, "environment.update", f"{group}/{env}", [f"group:{group}"])
    return respond(request, await svc(request).update_env(group, env, **body.model_dump()))


@r.post("/groups/{group}/environments/{env}/verbose")
async def set_verbose(request: Request, group: str, env: str, body: m.Verbose, p: Principal = Depends(admin_g)):
    note(request, "environment.verbose", f"{group}/{env}", [f"group:{group}", f"verbose:{body.verbose}"])
    return respond(request, await svc(request).update_env(group, env, verbose=body.verbose))


@r.put("/groups/{group}/environments/{env}/blocked")
async def set_blocked(request: Request, group: str, env: str, body: m.Blocked, p: Principal = Depends(admin_g)):
    """Blocked tools/resources/prompts (F5.6); applied to workers by the next deploy as RAMEN_BLOCKED."""
    note(request, "environment.blocked", f"{group}/{env}", [f"group:{group}"] + [f"blocked:{n}" for n in body.blocked])
    return respond(request, await svc(request).update_env(group, env, blocked=body.blocked))


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
    await svc(request).delete_env(group, env)
    return respond(request, {"ok": True})


@r.post("/groups/{group}/environments/{env}/deploy", status_code=202)
async def deploy(
    request: Request, group: str, env: str, body: m.DeployIn, bg: BackgroundTasks, p: Principal = Depends(admin_g)
):
    s = svc(request)
    e = await s.get_env(group, env)
    if body.zone and body.zone not in e.get("zones", []):
        raise invalid(f"Zone {body.zone} is not attached to {env}")
    note(request, "deploy.start", f"{group}/{env}", [f"group:{group}", f"canary:{body.canary}"])
    job = request.app.state.jobs.create("deploy", f"{group}/{env}")
    from ..audit import client_ip

    ip = client_ip(request)

    async def audit(action, target, ok, tags):
        await write_audit(s.store, user=p.name, ip=ip, action=action, target=target, ok=ok, tags=tags)

    bg.add_task(dep.run_deploy, s, job, group, env, body.zone, body.canary, audit)
    html = request.app.state.templates.get_template("partials/job.html").render(job=job)
    return respond(request, job, 202, hx_html=html)


@r.get("/jobs/{jid}")
async def job(request: Request, jid: str, p: Principal = Depends(viewer)):
    j = request.app.state.jobs.get(jid)
    if not j:
        raise not_found("job")
    return j


@r.get("/groups/{group}/zones/{zone}/workers")
async def workers(request: Request, group: str, zone: str, p: Principal = Depends(viewer_g)):
    from ..util import public

    s = svc(request)
    cfg = public(await s.worker_config(group, zone))
    return {**cfg, "live": await s.cloud.workers(group, zone)}


@r.put("/groups/{group}/zones/{zone}/workers")
async def set_workers(request: Request, group: str, zone: str, body: m.WorkersIn, p: Principal = Depends(admin_g)):
    note(request, "workers.scale", f"{group}/{zone}", [f"group:{group}"])
    return respond(request, await svc(request).set_workers(group, zone, p, body.count, body.size, body.allowed_sizes))


@r.post("/groups/{group}/zones/{zone}/rebalance")
async def rebalance(request: Request, group: str, zone: str, p: Principal = Depends(admin_g)):
    note(request, "rebalance", f"{group}/{zone}", [f"group:{group}"])
    return respond(request, await svc(request).rebalance(group, zone))


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


@r.get("/groups/{group}/secrets")
async def secrets(
    request: Request,
    group: str,
    env: str | None = None,
    zone: str | None = None,
    format: str | None = None,
    p: Principal = Depends(viewer_g),
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


def _once(label: str, value: str) -> str:
    from markupsafe import escape

    return f'<div class="once"><strong>{label} (shown once):</strong> <code>{escape(value)}</code></div>'
