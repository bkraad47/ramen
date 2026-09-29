"""CSRF (CONTRACTS §9): double-submit token. `ramen_csrf` cookie (readable by JS) must match the
`X-Ramen-CSRF` header on cookie-authenticated `/api/*` mutations, or the hidden `csrf_token` field on HTML forms.
API-key requests are exempt; a form POST without any csrf cookie (non-browser client) is accepted."""

import hmac
import secrets

from fastapi import Form, HTTPException, Request
from starlette.middleware.base import BaseHTTPMiddleware

from .apikeys import HEADER as API_KEY_HEADER
from .sessions import COOKIE as SESSION_COOKIE

COOKIE = "ramen_csrf"
HEADER = "X-Ramen-CSRF"
FIELD = "csrf_token"
SAFE = {"GET", "HEAD", "OPTIONS"}


def token(request: Request) -> str:
    """Token for this session: the cookie's value, else a fresh one the middleware sets on the response."""
    t = request.cookies.get(COOKIE) or getattr(request.state, "csrf_issue", None)
    if not t:
        t = secrets.token_urlsafe(24)
        request.state.csrf_issue = t
    return t


def _ok(expected: str | None, provided: str | None) -> bool:
    return bool(expected and provided) and hmac.compare_digest(expected, provided)


async def csrf_form(request: Request, csrf_token: str = Form("")) -> None:
    """Dependency for HTML form routes."""
    if request.headers.get(API_KEY_HEADER) or COOKIE not in request.cookies:
        return
    if not _ok(request.cookies.get(COOKIE), csrf_token or request.headers.get(HEADER)):
        raise HTTPException(403, "CSRF token missing or invalid")


class CsrfMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if (
            request.method not in SAFE
            and request.url.path.startswith("/api/")
            and SESSION_COOKIE in request.cookies
            and not request.headers.get(API_KEY_HEADER)
            and not _ok(request.cookies.get(COOKIE), request.headers.get(HEADER))
        ):
            from fastapi.responses import JSONResponse

            return JSONResponse({"detail": "CSRF token missing or invalid"}, 403)
        response = await call_next(request)
        issue = getattr(request.state, "csrf_issue", None)
        if issue and COOKIE not in request.cookies:
            response.set_cookie(
                COOKIE, issue, httponly=False, samesite="lax", secure=request.app.state.cookie_secure, max_age=12 * 3600
            )
        return response
