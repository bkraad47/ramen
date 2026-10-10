"""OAuth 2.1 authorization-server routes (CONTRACTS §16.3): discovery, consent, code exchange and refresh."""

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..audit import note
from ..auth import csrf
from ..oauth_server import OAuthError, OAuthServer, with_params
from ..rbac import Principal, require
from .auth_routes import base_url
from .pages import render

r = APIRouter()
# 0.5.92: any signed-in person, MCP users included, may authorize a client for their groups
viewer = require("mcp_user")


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


def _back_to_client(request: Request, target: str):
    """Hand the browser to the client's redirect URI. A `303` is right for a web client and for anything that follows
    redirects itself, but browsers in 2026 drop a redirect from a public https page to a loopback `http://` address
    (Chromium: the form POST ends `ERR_ABORTED`, the page never moves), which is exactly where Claude Code, the bridge
    and every RFC 8252 native client listen. A script-driven navigation from the page is still allowed, so a browser
    form submission (`Sec-Fetch-Mode: navigate`) to a loopback target gets a page that navigates itself; programmatic
    callers keep the `303` (found by the 0.7.5 live Entra run on GKE)."""
    from urllib.parse import urlsplit

    browser = request.headers.get("sec-fetch-mode") == "navigate"
    if not browser or urlsplit(target).hostname not in ("127.0.0.1", "localhost", "::1"):
        return RedirectResponse(target, 303)
    safe = target.replace("\\", "\\\\").replace("'", "\\'").replace("<", "%3C").replace(">", "%3E")
    body = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>Returning to your application</title>"
        f"</head><body style='font-family:sans-serif'><p>Returning to your application… "
        f"<a href='{safe}'>continue</a> if nothing happens.</p>"
        f"<script>location.replace('{safe}')</script></body></html>"
    )
    return HTMLResponse(body, headers={"Cache-Control": "no-store"})


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
        return _back_to_client(request, with_params(a["redirect_uri"], {**params, "error": "access_denied"}))
    code = await server(request).issue_code(p, a)
    note(request, "oauth.authorize", a["scope"], tags + ["decision:allow"])
    return _back_to_client(request, with_params(a["redirect_uri"], {**params, "code": code}))


@r.post("/oauth/token")
async def token(request: Request):
    """Public client, no client authentication (PKCE is the proof); form-encoded per RFC 6749."""
    form = {k: str(v) for k, v in (await request.form()).items()}
    grant = form.get("grant_type") or "-"
    tags = [f"grant:{grant}", f"client:{form.get('client_id') or '-'}"]
    try:
        out = await server(request).exchange(form, base_url(request))
    except OAuthError as e:
        # D3: every denial is audited with its reason; a replayed refresh token is its own event (M3)
        action = "oauth.refresh_reuse" if e.reason == "refresh_reuse" else "oauth.denied"
        note(request, action, grant, tags + [f"reason:{e.reason}", f"error:{e.error}"])
        return JSONResponse(e.body(), e.status_code, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
    _, group, zone = out["scope"].split(":")
    action = "oauth.refresh" if grant == "refresh_token" else "oauth.token"
    note(request, action, out["scope"], tags + [f"group:{group}", f"zone:{zone}"])
    return JSONResponse(out, 200, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})
