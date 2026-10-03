import os

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from starlette.responses import Response

from ..audit import note
from ..auth import csrf
from ..auth.sessions import COOKIE
from ..errors import ApiError, not_found
from ..mail import magic_mail, reset_mail
from ..security import safe_next

r = APIRouter()
ALIASES = ("/auth/{name}/login", "/auth/oauth/{name}/login")
CALLBACKS = ("/auth/{name}/callback", "/auth/oauth/{name}/callback")


async def auth_settings(request: Request):
    st = request.app.state
    return st.auth_env.with_doc(await st.store.get("config", "auth"))


def base_url(request: Request) -> str:
    import os

    return (os.environ.get("RAMEN_PUBLIC_URL") or str(request.base_url)).rstrip("/")


def _login_response(request: Request, user: dict, next_: str = "/"):
    resp = RedirectResponse(safe_next(next_), 303)
    kw = {"samesite": "lax", "secure": request.app.state.cookie_secure, "max_age": 12 * 3600}
    session = {"uid": user["id"], "ep": request.app.state.accounts.epoch_of(user)}
    resp.set_cookie(COOKIE, request.app.state.signer.sign(session), httponly=True, **kw)
    resp.set_cookie(csrf.COOKIE, csrf.token(request), httponly=False, **kw)
    return resp


async def _login_page(request: Request, status=200, **ctx) -> Response:
    st, auth = request.app.state, await auth_settings(request)
    ctx.setdefault("next", "/")
    return st.templates.TemplateResponse(
        request,
        "login.html",
        {**ctx, "providers": st.oauth.providers(), "auth": auth, "mail": st.mailer.enabled},
        status_code=status,
    )


@r.get("/login")
async def login_page(request: Request, next: str = "/", msg: str | None = None):
    return await _login_page(request, next=next, msg=msg)


@r.post("/login")
async def login(
    request: Request, email: str = Form(), password: str = Form(), next: str = Form("/"), _=Depends(csrf.csrf_form)
):
    note(request, "login", email, user=email)
    auth = await auth_settings(request)
    if not auth.can_password(email):
        note(request, "login", email, ["password_login:disabled"], user=email)
        return await _login_page(
            request, 403, error="Password login is disabled; use a configured provider or a sign-in link", next=next
        )
    user = await request.app.state.accounts.authenticate(email, password)
    if not user:
        return await _login_page(request, 401, error="Invalid email or password", next=next)
    return _login_response(request, user, next)


@r.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE)
    resp.delete_cookie(csrf.COOKIE)
    return resp


# password reset / invite ------------------------------------------------
@r.get("/auth/reset")
async def reset_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request, "reset.html", {"stage": "request", "mail": request.app.state.mailer.enabled}
    )


@r.post("/auth/reset")
async def reset_request(request: Request, email: str = Form(), _=Depends(csrf.csrf_form)):
    """Always 200: never reveals whether the account exists."""
    st = request.app.state
    note(request, "password.reset.request", email, user=email)
    started = await st.accounts.start_token(email, "reset")
    if started and st.mailer.enabled:
        user, nonce = started
        link = f"{base_url(request)}/auth/reset/{st.tokens.issue('reset', user['id'], nonce)}"
        await st.mailer.send(email, *reset_mail(base_url(request), link))
    return st.templates.TemplateResponse(request, "reset.html", {"stage": "sent", "mail": st.mailer.enabled})


@r.get("/auth/reset/{token}")
async def reset_form(request: Request, token: str):
    st = request.app.state
    if not st.tokens.load("reset", token):
        return st.templates.TemplateResponse(request, "reset.html", {"stage": "invalid"}, status_code=400)
    return st.templates.TemplateResponse(request, "reset.html", {"stage": "set", "token": token})


@r.post("/auth/reset/{token}")
async def reset_finish(request: Request, token: str, password: str = Form(), _=Depends(csrf.csrf_form)):
    st = request.app.state
    parsed = st.tokens.load("reset", token)
    user = parsed and await st.accounts.redeem_token(parsed[0], "reset", parsed[1], password=password)
    note(request, "password.reset", user["email"] if user else "-", user=user["email"] if user else None)
    if not user:
        raise ApiError(400, "This reset link is invalid, expired or already used")
    return RedirectResponse("/login?msg=Password+updated%2C+sign+in", 303)


# magic link ----------------------------------------------------------------
@r.post("/auth/magic")
async def magic_request(request: Request, email: str = Form(), _=Depends(csrf.csrf_form)):
    st = request.app.state
    note(request, "login.magic.request", email, user=email)
    if not (await auth_settings(request)).magic_link:
        raise ApiError(403, "Magic-link login is disabled")
    started = await st.accounts.start_token(email, "magic")
    if started and st.mailer.enabled:
        user, nonce = started
        link = f"{base_url(request)}/auth/magic/{st.tokens.issue('magic', user['id'], nonce)}"
        await st.mailer.send(email, *magic_mail(base_url(request), link))
    return await _login_page(request, msg="If that account exists, a sign-in link has been emailed")


@r.get("/auth/magic/{token}")
async def magic_login(request: Request, token: str):
    st = request.app.state
    parsed = st.tokens.load("magic", token)
    user = (
        parsed
        and (await auth_settings(request)).magic_link
        and await st.accounts.redeem_token(parsed[0], "magic", parsed[1])
    )
    note(request, "login.magic", user["email"] if user else "-", user=user["email"] if user else None)
    if not user:
        raise ApiError(400, "This sign-in link is invalid, expired or already used")
    return _login_response(request, user)


# OAuth / OIDC ---------------------------------------------------------------
def _client(request: Request, name: str):
    client = request.app.state.oauth.client(name)
    if not client:
        raise not_found("OAuth provider")
    return client


async def oauth_login(request: Request, name: str):
    client = _client(request, name)
    try:
        return await client.authorize_redirect(request, str(request.url_for("oauth_callback", name=name)))
    except Exception as e:  # noqa: BLE001 - issuer metadata unreachable/malformed: a clear 502, not a bare 500
        note(request, "login.oauth", name, [f"provider:{name}", "error:issuer"], user="-")
        raise ApiError(502, f"OAuth provider {name!r} is not reachable: {type(e).__name__}") from e


async def oauth_callback(request: Request, name: str):
    client = _client(request, name)
    from authlib.integrations.base_client.errors import OAuthError

    try:
        token = await client.authorize_access_token(request)
    except OAuthError as e:
        note(request, "login.oauth", name, [f"provider:{name}"], user="-")
        raise ApiError(401, f"OAuth error: {e.error}") from e
    info = dict(token.get("userinfo") or {})
    if not info.get("email"):
        info = dict(await client.userinfo(token=token))
    email = (info.get("email") or "").strip().lower()
    note(request, "login.oauth", email or "-", [f"provider:{name}"], user=email or "-")
    trusted = os.environ.get(f"RAMEN_OAUTH_{name.upper()}_ALLOW_UNVERIFIED", "0") == "1"
    if not email or (info.get("email_verified") is not True and not trusted):
        raise ApiError(403, "The provider did not return a verified email")
    auth = await auth_settings(request)
    role, groups = auth.map_role(name, info)
    # the bootstrap super admin is the break-glass account: a provider's rules never demote it
    authoritative = auth.has_mapping(name) and email != auth.admin_email
    user = await request.app.state.accounts.upsert_sso_user(
        email, role, groups, provider=name, authoritative=authoritative
    )
    return _login_response(request, user)


for path in ALIASES:
    r.get(path)(oauth_login)
for path in CALLBACKS:
    r.get(path, name="oauth_callback")(oauth_callback)
