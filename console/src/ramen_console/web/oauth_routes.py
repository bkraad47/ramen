"""OAuth 2.1 authorization-server routes (CONTRACTS §16.3): discovery, consent, code exchange and refresh."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse

from ..audit import note
from ..auth import csrf
from ..oauth_server import OAuthError, OAuthServer, with_params
from ..rbac import Principal, require
from .auth_routes import base_url
from .pages import render

r = APIRouter()
viewer = require("viewer")


def server(request: Request) -> OAuthServer:
    return request.app.state.oauth_server


@r.get("/.well-known/oauth-authorization-server")
async def metadata(request: Request):
    return await server(request).metadata(base_url(request))


def _authorize_query(request: Request) -> dict:
    return {k: v for k, v in request.query_params.items()}


@r.get("/oauth/authorize")
async def authorize_page(request: Request, p: Principal = Depends(viewer)):
    a = await server(request).validate_authorize(p, _authorize_query(request))
    return render(
        request,
        "oauth_consent.html",
        a=a,
        q=_authorize_query(request),
        csrf_token=csrf.token(request),
        page="oauth",
    )


@r.post("/oauth/authorize")
async def authorize_decide(
    request: Request,
    decision: str = Form(""),
    p: Principal = Depends(viewer),
    _=Depends(csrf.csrf_form),
):
    form = await request.form()
    q = {k: str(v) for k, v in form.items() if k not in ("decision", "csrf_token")}
    a = await server(request).validate_authorize(p, q)  # re-validated: the form is untrusted input like the query
    tags = [f"group:{a['group']}", f"client:{a['client']['client_id']}", f"zone:{a['zone']}"]
    params = {"state": a["state"]} if a["state"] else {}
    if decision != "allow":
        note(request, "oauth.authorize", a["scope"], tags + ["decision:deny"])
        return RedirectResponse(with_params(a["redirect_uri"], {**params, "error": "access_denied"}), 303)
    code = await server(request).issue_code(p, a)
    note(request, "oauth.authorize", a["scope"], tags + ["decision:allow"])
    return RedirectResponse(with_params(a["redirect_uri"], {**params, "code": code}), 303)


@r.post("/oauth/token")
async def token(request: Request):
    """Public client, no client authentication (PKCE is the proof); form-encoded per RFC 6749."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    srv = server(request)
    try:
        out = await srv.exchange(form, base_url(request))
    except OAuthError as e:
        note(request, "oauth.token", form.get("grant_type") or "-", [f"error:{e.error}"])
        return JSONResponse(e.body(), e.status_code, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    note(request, "oauth.token", out["scope"], [f"grant:{form.get('grant_type')}", f"client:{form.get('client_id')}"])
    return JSONResponse(out, 200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
