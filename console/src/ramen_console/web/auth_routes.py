from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse

from ..audit import note
from ..auth.sessions import COOKIE
from ..errors import not_found

r = APIRouter()


def _login_response(request: Request, user: dict, next_: str = "/"):
    resp = RedirectResponse(next_ if next_.startswith("/") else "/", 303)
    resp.set_cookie(COOKIE, request.app.state.signer.sign({"uid": user["id"]}), httponly=True, samesite="lax",
                    secure=request.app.state.cookie_secure, max_age=12 * 3600)
    return resp


@r.get("/login")
async def login_page(request: Request, next: str = "/"):
    t = request.app.state.templates
    return t.TemplateResponse(request, "login.html", {"next": next, "providers": request.app.state.oauth.providers()})


@r.post("/login")
async def login(request: Request, email: str = Form(), password: str = Form(), next: str = Form("/")):
    note(request, "login", email, user=email)
    user = await request.app.state.accounts.authenticate(email, password)
    if not user:
        t = request.app.state.templates
        return t.TemplateResponse(request, "login.html", {"error": "Invalid email or password", "next": next,
                                                          "providers": request.app.state.oauth.providers()}, status_code=401)
    return _login_response(request, user, next)


@r.get("/logout")
async def logout():
    resp = RedirectResponse("/login", 303)
    resp.delete_cookie(COOKIE)
    return resp


@r.get("/auth/oauth/{name}/login")
async def oauth_login(request: Request, name: str):
    client = request.app.state.oauth.client(name)
    if not client:
        raise not_found("oauth provider")
    return await client.authorize_redirect(request, str(request.url_for("oauth_callback", name=name)))


@r.get("/auth/oauth/{name}/callback", name="oauth_callback")
async def oauth_callback(request: Request, name: str):
    client = request.app.state.oauth.client(name)
    if not client:
        raise not_found("oauth provider")
    token = await client.authorize_access_token(request)
    info = token.get("userinfo") or {}
    if not info.get("email"):
        info = await client.userinfo(token=token)
    user = await request.app.state.accounts.upsert_sso_user(info["email"])
    return _login_response(request, user)
