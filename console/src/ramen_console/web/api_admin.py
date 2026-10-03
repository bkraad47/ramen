import os

from fastapi import APIRouter, Depends, Request

from .. import alerts, github_app, scheduler
from .. import mail as mail_mod
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
MASK = ("SECRET", "PASSWORD", "KEY", "TOKEN", "DSN")  # N9: a DSN commonly embeds a password (RAMEN_POSTGRES_DSN)
UNMASKED = {"RAMEN_SECRETS_BACKEND"}


@r.get("/users")
async def users(request: Request, format: str | None = None, p: Principal = Depends(viewer)):
    return tabular(await accounts(request).list_users(p), format, "users")


@r.post("/users", status_code=201)
async def create_user(request: Request, body: m.UserIn, p: Principal = Depends(admin)):
    note(request, "user.create", body.email, [f"group:{g}" for g in body.groups])
    st = request.app.state
    doc = await st.accounts.create_user(
        p, body.email, body.password, body.role, body.groups, memberships=body.memberships
    )
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
    tags = ([f"role:{body.role}"] if body.role else []) + [f"group:{g}" for g in body.groups or []]
    tags += [f"{g}:{r}" for g, r in (body.memberships or {}).items()]
    note(request, "user.update", uid, tags)
    return respond(request, await accounts(request).update_user(uid, body.role, body.groups, body.memberships))


@r.delete("/users/{uid}")
async def delete_user(request: Request, uid: str, p: Principal = Depends(admin)):
    note(request, "user.delete", uid, [f"user:{uid}"])
    await accounts(request).delete_user(p, uid)
    return respond(request, {"ok": True})


@r.post("/users/{uid}/password")
async def set_password(request: Request, uid: str, body: m.Password, p: Principal = Depends(viewer)):
    if uid == "me":
        uid = p.id
    if uid != p.id:
        target = await accounts(request).store.get("users", uid)
        if not target or not await accounts(request).can_manage(p, target):
            raise forbidden("You may reset passwords of members of groups you administer (never a super admin's)")
    note(request, "user.password", uid, [f"user:{uid}"])
    await accounts(request).set_password(uid, body.password)
    resp = respond(request, {"ok": True})
    return await _reissue_session(request, resp, p) if uid == p.id else resp


@r.get("/api-keys")
async def keys(request: Request, format: str | None = None, p: Principal = Depends(super_)):
    return tabular(await accounts(request).list_keys(p), format, "api-keys")


@r.post("/api-keys", status_code=201)
async def mint_key(request: Request, body: m.KeyIn, p: Principal = Depends(super_)):
    """`devops` keys drive this API; `agent` keys are mirrored as the named groups' MCP keys so the next deploy
    hands them to the workers, exactly as the group page's key form does (D21)."""
    note(request, "api_key.create", body.name, [f"client_type:{body.client_type}"])
    a = accounts(request)
    doc = await a.mint_key(p, body.name, body.role, body.groups, body.client_type)
    if doc["client_type"] == "agent":
        try:
            await svc(request).mirror_agent_key(doc["groups"], doc["name"], doc["key"], doc["id"], p.name)
        except Exception:
            await a.store.delete("api_keys", doc["id"])  # never leave a key that can never reach a worker
            raise
    return respond(request, doc, 201, hx_html=_once("API key", doc["key"]))


@r.delete("/api-keys/{kid}")
async def delete_key(request: Request, kid: str, p: Principal = Depends(super_)):
    note(request, "api_key.delete", kid, [f"key:{kid}"])
    await accounts(request).delete_key(p, kid)
    await svc(request).revoke_agent_key(kid)  # an agent key must stop reaching workers on the next deploy
    return respond(request, {"ok": True})


@r.post("/requests", status_code=201)
async def request_permission(request: Request, body: m.RequestIn, p: Principal = Depends(require("mcp_user"))):
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
        raise invalid("A request needs a role or a permission")
    scope = perm.parse_scope(body.scope)
    return respond(
        request,
        await accounts(request).request_permission(p, body.role, body.group, body.zone, body.permission, scope),
        201,
    )


@r.get("/policy/permissions")
async def policy_permissions(p: Principal = Depends(viewer)):
    return perm.table()


@r.get("/requests")
async def list_requests(request: Request, p: Principal = Depends(admin)):
    return await accounts(request).list_requests(p)


@r.post("/requests/{rid}/approve")
async def approve(request: Request, rid: str, p: Principal = Depends(admin)):
    """A super admin, or a group admin of the request's group who is not the requester (R4)."""
    note(request, "permission.approve", rid, [f"request:{rid}"])
    return respond(
        request, await accounts(request).approve_request(rid, p.name, apply=svc(request).apply_sa_permissions, p=p)
    )


@r.post("/requests/{rid}/deny")
async def deny(request: Request, rid: str, p: Principal = Depends(admin)):
    note(request, "permission.deny", rid, [f"request:{rid}"])
    return respond(request, await accounts(request).deny_request(rid, p.name, p=p))


@r.post("/requests/{rid}/revoke")
async def revoke(request: Request, rid: str, p: Principal = Depends(admin)):
    """Takes an approved request back: the cloud roles for a permission, or the role and group for a role grant."""
    note(request, "permission.revoke", rid, [f"request:{rid}"])
    return respond(
        request, await accounts(request).revoke_request(rid, p.name, revoke=svc(request).revoke_sa_permission, p=p)
    )


# --- OAuth clients (§16.3): pre-registered by a super admin; no dynamic registration in this release ---------------
@r.get("/oauth/clients")
async def oauth_clients(request: Request, p: Principal = Depends(super_)):
    return await request.app.state.oauth_server.clients()


@r.post("/oauth/clients", status_code=201)
async def oauth_client_create(request: Request, body: m.OAuthClientIn, p: Principal = Depends(super_)):
    note(request, "oauth.client.create", body.name, [f"client:{body.name}"])
    return respond(
        request, await request.app.state.oauth_server.register_client(body.name, body.redirect_uris, p.name), 201
    )


@r.delete("/oauth/clients/{client_id}")
async def oauth_client_delete(request: Request, client_id: str, p: Principal = Depends(super_)):
    note(request, "oauth.client.delete", client_id, [f"client:{client_id}"])
    await request.app.state.oauth_server.delete_client(client_id)
    return respond(request, {"ok": True})


@r.get("/audit")
async def audit(request: Request, limit: int = 200, format: str | None = None, p: Principal = Depends(super_)):
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
    note(request, "backup.create", body.target, [f"target:{body.target}"])
    return respond(request, await backups(request).create(body.target, p.name, body.path), 201)


@r.get("/backups/{bid}/download")
async def download_backup(request: Request, bid: str, p: Principal = Depends(super_)):
    return await backups(request).read(bid)


@r.post("/backups/{bid}/restore")
async def restore_backup(request: Request, bid: str, body: m.RestoreIn | None = None, p: Principal = Depends(super_)):
    """§13.1: `dry_run` reports the plan, `prune` removes what the backup does not have, `reconcile` re-applies the
    restored zones, `force` overrides the release gate."""
    b = body or m.RestoreIn()
    note(request, "backup.restore", bid, [f"{k}:{str(v).lower()}" for k, v in b.model_dump().items() if v])
    result = await backups(request).restore(
        bid, p.name, p.id, dry_run=b.dry_run, prune=b.prune, reconcile=b.reconcile, force=b.force
    )
    resp = respond(request, result, hx_html=_restore_html(result))
    # A restore bumps every restored user's epoch, which would sign out the super admin mid-request; they keep a
    # session on the new epoch and every *other* session dies (same rule as a change to config/auth).
    return await _reissue_session(request, resp, p)


def _restore_html(r: dict) -> str:
    """A restore is destructive, so the page shows what it did (or would do) instead of only refreshing."""
    from markupsafe import escape

    head = "Restore preview" if r["dry_run"] else "Restore done"
    rows = "".join(
        f"<tr><td>{c}</td><td>{v['created']}</td><td>{v['updated']}</td><td>{v['unchanged']}</td>"
        f"<td>{len(r['extra'].get(c, []))}</td></tr>"
        for c, v in r["restored"].items()
    )
    parts = [
        f'<div class="once"><strong>{head}</strong> (release {escape(r["release_version"])})',
        "<table><tr><th>Collection</th><th>Created</th><th>Updated</th><th>Unchanged</th>"
        f"<th>Not in the backup</th></tr>{rows}</table>",
    ]
    for label, value in (
        ("Pruned", ", ".join(f"{c}: {', '.join(v)}" for c, v in r["pruned"].items())),
        ("Reconciled", ", ".join(r["reconciled"])),
        ("Left running but not in the backup", ", ".join(r["orphans"])),
    ):
        if value:
            parts.append(f"<p>{label}: <code>{escape(value)}</code></p>")
    for w in r["warnings"]:
        parts.append(f'<p class="err">{escape(w)}</p>')
    return "".join(parts) + "</div>"


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
    note(request, "config.reload", os.environ.get("RAMEN_CONFIG", "-"), ["scope:global"])
    applied = apply_config()
    st = request.app.state
    st.oauth, st.auth_env = st.oauth.from_env(), st.auth_env.from_env()
    st.mailer = await mail_mod.build_mailer(st.store)  # layers config/smtp back over the fresh env read (N2)
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
            "Refusing to disable password login: no OAuth provider, no magic link "
            "and no RAMEN_ADMIN_FORCE_PASSWORD break-glass"
        )
    await st.store.put("config", "auth", doc)
    await st.accounts.bump_all_epochs()  # V1.4: nobody keeps a session minted under the old auth rules
    resp = respond(
        request,
        {**(await auth_settings(request)).public(), "providers": st.oauth.providers(), "mail": st.mailer.backend},
    )
    return await _reissue_session(request, resp, p)


@r.put("/config/auth/role-map/{provider}")
async def set_role_map(request: Request, provider: str, body: m.RoleMapChange, p: Principal = Depends(super_)):
    """D38: a provider's IdP role → Ramen role + MCP groups rules, re-applied on every login. One change per call:
    `claim` names the token claim to read, `value`+`role`(+`groups`) adds or replaces a rule, `remove` drops one.
    Nobody is signed out: the rules take effect at each person's next login."""
    from ..errors import not_found
    from .auth_routes import auth_settings

    st = request.app.state
    if provider not in st.oauth.providers():
        raise not_found(f"Unknown OAuth provider {provider!r}")
    doc = await st.store.get("config", "auth") or {}
    claims, rules = doc.setdefault("role_claim", {}), doc.setdefault("role_map", {}).setdefault(provider, {})
    tags = []
    if body.claim is not None:
        claims[provider] = body.claim.strip()
        tags.append(f"claim:{claims[provider]}")
    if body.value:
        if not body.role:
            raise invalid("A rule needs a role")
        rules[body.value.strip().lower()] = {"role": body.role, "groups": body.groups or []}
        tags.append(f"rule:{body.value.strip().lower()}={body.role}:{','.join(body.groups or [])}")
    if body.remove:
        rules.pop(body.remove.strip().lower(), None)
        tags.append(f"remove:{body.remove.strip().lower()}")
    if not tags:
        raise invalid("Nothing to change: give claim, value+role or remove")
    note(request, "config.auth", f"role-map:{provider}", tags)
    await st.store.put("config", "auth", doc)
    return respond(
        request,
        {**(await auth_settings(request)).public(), "providers": st.oauth.providers(), "mail": st.mailer.backend},
    )


async def _reissue_session(request: Request, resp, p: Principal):
    """Bumping every epoch would sign the acting super admin out of the request they just authenticated; hand
    them a session on the new epoch instead, so only *other* sessions die."""
    from ..auth.sessions import COOKIE

    st = request.app.state
    if p.kind != "user" or COOKIE not in request.cookies:
        return resp
    user = await st.store.get("users", p.id)
    if user:
        resp.set_cookie(
            COOKIE,
            st.signer.sign({"uid": p.id, "ep": st.accounts.epoch_of(user)}),
            httponly=True,
            samesite="lax",
            secure=st.cookie_secure,
            max_age=12 * 3600,
        )
    return resp


@r.get("/config/sa-rules")
async def sa_rules(request: Request, p: Principal = Depends(super_)):
    return {"rules": (await svc(request).store.get("config", "sa_rules") or {}).get("rules", [])}


@r.put("/config/sa-rules")
async def set_sa_rules(request: Request, body: m.Rules, p: Principal = Depends(super_)):
    note(request, "config.sa_rules", "sa_rules", [f"rules:{len(body.rules)}"])
    doc = await svc(request).set_sa_rules(body.rules)
    return respond(request, {"rules": doc["rules"]})


@r.get("/config/scheduler")
async def get_scheduler_config(request: Request, p: Principal = Depends(super_)):
    return await scheduler.get_config(svc(request).store)


@r.put("/config/scheduler")
async def set_scheduler_config(request: Request, body: m.SchedulerConfig, p: Principal = Depends(super_)):
    """N1: super-admin on/off + check-interval toggle for the auto-rebalance scheduler."""
    doc = await scheduler.set_config(svc(request).store, **body.model_dump())
    note(request, "config.scheduler", "scheduler", [f"{k}:{v}" for k, v in doc.items()])
    return respond(request, doc)


@r.get("/config/smtp")
async def get_smtp_config(request: Request, p: Principal = Depends(super_)):
    return mail_mod.public_smtp_config(await mail_mod.get_smtp_config(svc(request).store))


@r.put("/config/smtp")
async def set_smtp_config(request: Request, body: m.SmtpConfig, p: Principal = Depends(super_)):
    """N2: super-admin SMTP credentials (Users page); overrides RAMEN_SMTP_* field by field, takes effect now."""
    st = request.app.state
    doc = await mail_mod.set_smtp_config(st.store, **body.model_dump())
    note(request, "config.smtp", "smtp", [f"host:{doc['host']}", f"tls:{doc['tls']}"])  # never the password
    st.mailer = mail_mod.Mailer.from_config(doc)
    return respond(request, mail_mod.public_smtp_config(doc))


@r.get("/config/notify")
async def get_notify_config(request: Request, p: Principal = Depends(super_)):
    return await alerts.get_notify_config(svc(request).store)


@r.put("/config/notify")
async def set_notify_config(request: Request, body: m.NotifyConfig, p: Principal = Depends(super_)):
    """N2: which users get emailed a digest of server warnings/errors."""
    doc = await alerts.set_notify_config(svc(request).store, body.user_ids)
    note(request, "config.notify", "notify", [f"user_ids:{len(doc['user_ids'])}"])
    return respond(request, doc)


@r.get("/config/github-app")
async def get_github_app_config(request: Request, p: Principal = Depends(super_)):
    return github_app.public_app_config(await github_app.get_app_config(svc(request).store))


@r.put("/config/github-app")
async def set_github_app_config(request: Request, body: m.GitHubAppConfig, p: Principal = Depends(super_)):
    """N5: console-wide GitHub App credentials; a group then only needs `github_app_installation_id`, no
    stored per-group token, to sync a private repo."""
    doc = await github_app.set_app_config(svc(request).store, **body.model_dump())
    note(request, "config.github_app", "github_app", [f"app_id:{doc['app_id']}"])  # never the private key
    return respond(request, github_app.public_app_config(doc))


@r.post("/refresh")
async def refresh(request: Request, p: Principal = Depends(super_)):
    note(request, "refresh", "cloud", ["scope:cloud"])
    return respond(request, await svc(request).refresh())
