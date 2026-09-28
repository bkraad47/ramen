import os

from fastapi import APIRouter, Depends, Request

from ..audit import note
from ..config import apply_config
from ..errors import forbidden, invalid
from ..mail import invite_mail
from ..policy import permissions as perm
from ..rbac import Principal, require
from . import models as m
from .api import _once
from .helpers import accounts, backups, respond, svc, tabular

r = APIRouter(prefix="/api/v1")
viewer, admin, super_ = require("viewer"), require("group_admin"), require("super_admin")
MASK = ("SECRET", "PASSWORD", "KEY", "TOKEN")
UNMASKED = {"RAMEN_SECRETS_BACKEND"}


@r.get("/users")
async def users(request: Request, format: str | None = None, p: Principal = Depends(admin)):
    return tabular(await accounts(request).list_users(p), format, "users")


@r.post("/users", status_code=201)
async def create_user(request: Request, body: m.UserIn, p: Principal = Depends(admin)):
    note(request, "user.create", body.email, [f"group:{g}" for g in body.groups])
    st = request.app.state
    doc = await st.accounts.create_user(p, body.email, body.password, body.role, body.groups)
    if st.mailer.enabled:
        from .auth_routes import base_url

        _, nonce = await st.accounts.start_token(body.email, "reset")
        base = base_url(request)
        mail = await st.mailer.send(
            body.email,
            *invite_mail(base, body.email, f"{base}/auth/reset/{st.tokens.issue('reset', doc['id'], nonce)}", p.name),
        )
        doc["invite"] = {k: v for k, v in mail.items() if k != "path"}
    return respond(request, doc, 201)


@r.put("/users/{uid}")
async def update_user(request: Request, uid: str, body: m.UserUpdate, p: Principal = Depends(super_)):
    note(request, "user.update", uid)
    return respond(request, await accounts(request).update_user(uid, body.role, body.groups))


@r.delete("/users/{uid}")
async def delete_user(request: Request, uid: str, p: Principal = Depends(admin)):
    note(request, "user.delete", uid)
    await accounts(request).delete_user(p, uid)
    return respond(request, {"ok": True})


@r.post("/users/{uid}/password")
async def set_password(request: Request, uid: str, body: m.Password, p: Principal = Depends(viewer)):
    if uid == "me":
        uid = p.id
    if uid != p.id and p.role != "super_admin":
        raise forbidden("only super admins reset other passwords")
    note(request, "user.password", uid)
    await accounts(request).set_password(uid, body.password)
    return respond(request, {"ok": True})


@r.get("/api-keys")
async def keys(request: Request, format: str | None = None, p: Principal = Depends(admin)):
    return tabular(await accounts(request).list_keys(p), format, "api-keys")


@r.post("/api-keys", status_code=201)
async def mint_key(request: Request, body: m.KeyIn, p: Principal = Depends(admin)):
    note(request, "api_key.create", body.name)
    doc = await accounts(request).mint_key(p, body.name, body.role, body.groups)
    return respond(request, doc, 201, hx_html=_once("API key", doc["key"]))


@r.delete("/api-keys/{kid}")
async def delete_key(request: Request, kid: str, p: Principal = Depends(admin)):
    note(request, "api_key.delete", kid)
    await accounts(request).delete_key(p, kid)
    return respond(request, {"ok": True})


@r.post("/requests", status_code=201)
async def request_permission(request: Request, body: m.RequestIn, p: Principal = Depends(viewer)):
    """Role request {role, group?} or SA permission request {group, zone, permission} (CONTRACTS §9)."""
    tags = [f"group:{body.group}"] if body.group else []
    if body.permission:
        note(
            request,
            "permission.request",
            f"{body.group}/{body.zone}:{body.permission}",
            tags + [f"permission:{body.permission}"],
        )
        await svc(request).check_permission_request(p, body.group, body.zone, body.permission)
    elif body.role:
        note(request, "permission.request", f"{body.role}:{body.group}", tags)
    else:
        raise invalid("request needs role or permission")
    return respond(
        request, await accounts(request).request_permission(p, body.role, body.group, body.zone, body.permission), 201
    )


@r.get("/policy/permissions")
async def policy_permissions(p: Principal = Depends(viewer)):
    return perm.table()


@r.get("/requests")
async def list_requests(request: Request, p: Principal = Depends(super_)):
    return await accounts(request).list_requests()


@r.post("/requests/{rid}/approve")
async def approve(request: Request, rid: str, p: Principal = Depends(super_)):
    note(request, "permission.approve", rid)
    return respond(
        request, await accounts(request).approve_request(rid, p.name, apply=svc(request).apply_sa_permissions)
    )


@r.get("/audit")
async def audit(request: Request, limit: int = 200, format: str | None = None, p: Principal = Depends(viewer)):
    rows = await svc(request).store.list("audit")
    if p.role != "super_admin":
        mine = {f"group:{g}" for g in p.groups}
        rows = [a for a in rows if mine & set(a.get("tags", []))]
    rows = sorted(rows, key=lambda a: a["ts"], reverse=True)[:limit]
    return tabular(rows, format, "audit")


@r.get("/backups")
async def list_backups(request: Request, format: str | None = None, p: Principal = Depends(super_)):
    return tabular(await backups(request).list(), format, "backups")


@r.post("/backups", status_code=201)
async def create_backup(request: Request, body: m.BackupIn, p: Principal = Depends(super_)):
    note(request, "backup.create", body.target)
    return respond(request, await backups(request).create(body.target, p.name, body.path), 201)


@r.get("/backups/{bid}/download")
async def download_backup(request: Request, bid: str, p: Principal = Depends(super_)):
    return await backups(request).read(bid)


@r.post("/backups/{bid}/restore")
async def restore_backup(request: Request, bid: str, p: Principal = Depends(super_)):
    note(request, "backup.restore", bid)
    return respond(request, await backups(request).restore(bid))


def masked_env() -> dict[str, str]:
    return {
        k: ("***" if any(w in k for w in MASK) and k not in UNMASKED else v)
        for k, v in sorted(os.environ.items())
        if k.startswith("RAMEN_")
    }


@r.get("/config")
async def config(request: Request, p: Principal = Depends(super_)):
    return {
        "env": masked_env(),
        "config_file": os.environ.get("RAMEN_CONFIG"),
        "store": os.environ.get("RAMEN_STORE", "memory"),
        "cloud": os.environ.get("RAMEN_CLOUD", "local"),
        "secrets_backend": request.app.state.secrets.kind,
        "mail": request.app.state.mailer.backend,
        "oauth_providers": request.app.state.oauth.providers(),
        "gcp": {
            k: os.environ.get(k)
            for k in ("RAMEN_GCP_PROJECT", "RAMEN_GCP_REGION", "RAMEN_GROUPS_BUCKET", "RAMEN_IMAGE_WORKER")
        },
    }


@r.post("/config/reload")
async def reload_config(request: Request, p: Principal = Depends(super_)):
    note(request, "config.reload", os.environ.get("RAMEN_CONFIG", "-"))
    applied = apply_config()
    st = request.app.state
    st.oauth, st.auth_env, st.mailer = st.oauth.from_env(), st.auth_env.from_env(), st.mailer.from_env()
    return respond(request, {"applied": sorted(applied)})


@r.get("/config/auth")
async def get_auth_config(request: Request, p: Principal = Depends(super_)):
    from .auth_routes import auth_settings

    st = request.app.state
    return {**(await auth_settings(request)).public(), "providers": st.oauth.providers(), "mail": st.mailer.backend}


@r.put("/config/auth")
async def set_auth_config(request: Request, body: m.AuthConfig, p: Principal = Depends(super_)):
    """Super-admin toggles (CONTRACTS §9): password_login (break-glass via RAMEN_ADMIN_FORCE_PASSWORD=1), magic_link."""
    from .auth_routes import auth_settings

    st = request.app.state
    doc = await st.store.get("config", "auth") or {}
    doc.update({k: v for k, v in body.model_dump().items() if v is not None})
    note(request, "config.auth", "auth", [f"{k}:{v}" for k, v in doc.items()])
    if (
        doc.get("password_login") is False
        and not st.oauth.enabled
        and not (await auth_settings(request)).with_doc(doc).magic_link
        and not st.auth_env.force_password
    ):
        raise invalid(
            "refusing to disable password login: no OAuth provider, no magic link "
            "and no RAMEN_ADMIN_FORCE_PASSWORD break-glass"
        )
    await st.store.put("config", "auth", doc)
    return respond(
        request,
        {**(await auth_settings(request)).public(), "providers": st.oauth.providers(), "mail": st.mailer.backend},
    )


@r.get("/config/sa-rules")
async def sa_rules(request: Request, p: Principal = Depends(super_)):
    return {"rules": (await svc(request).store.get("config", "sa_rules") or {}).get("rules", [])}


@r.put("/config/sa-rules")
async def set_sa_rules(request: Request, body: m.Rules, p: Principal = Depends(super_)):
    note(request, "config.sa_rules", "sa_rules")
    doc = await svc(request).set_sa_rules(body.rules)
    return respond(request, {"rules": doc["rules"]})


@r.post("/refresh")
async def refresh(request: Request, p: Principal = Depends(super_)):
    note(request, "refresh", "cloud")
    return respond(request, await svc(request).refresh())
