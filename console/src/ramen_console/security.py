"""Cross-cutting hardening: signing-secret resolution, security headers, login rate limiting, log redaction."""

import logging
import os
import re
import secrets
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

log = logging.getLogger("ramen.security")
_EPHEMERAL: list[str] = []


def resolve_signing_secret() -> str:
    """RAMEN_SESSION_SECRET, else RAMEN_FERNET_KEY, else a random per-process secret (sessions do not survive a
    restart, but nothing is ever signed with a public constant). Set RAMEN_SESSION_SECRET in production."""
    secret = os.environ.get("RAMEN_SESSION_SECRET") or os.environ.get("RAMEN_FERNET_KEY")
    if secret:
        return secret
    if not _EPHEMERAL:
        _EPHEMERAL.append(secrets.token_urlsafe(48))
        log.warning("RAMEN_SESSION_SECRET is not set: using an ephemeral signing secret (sessions reset on restart)")
    return _EPHEMERAL[0]


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    CSP = (
        "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    )

    def __init__(self, app, hsts: bool = False):
        super().__init__(app)
        self.hsts = hsts

    async def dispatch(self, request: Request, call_next):
        resp = await call_next(request)
        h = resp.headers
        h.setdefault("X-Content-Type-Options", "nosniff")
        h.setdefault("X-Frame-Options", "DENY")
        h.setdefault("Referrer-Policy", "same-origin")
        h.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        if not request.url.path.startswith("/api/docs"):
            h.setdefault("Content-Security-Policy", self.CSP)
        if self.hsts:
            h.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-IP limit on *failed* credential attempts (login, reset, magic link). Default 20/minute;
    successful requests are never counted, so automation and test harnesses are unaffected. 0 disables."""

    PATHS = ("/login", "/auth/reset", "/auth/magic")

    def __init__(self, app, limit: int | None = None, window: float = 60.0):
        super().__init__(app)
        self.limit = int(os.environ.get("RAMEN_LOGIN_RATE_LIMIT", "20")) if limit is None else limit
        self.window = window
        self._hits: dict[str, tuple[float, int]] = {}
        self._lock = threading.Lock()

    def _count(self, key: str, add: int) -> int:
        now = time.monotonic()
        with self._lock:
            start, n = self._hits.get(key, (now, 0))
            if now - start > self.window:
                start, n = now, 0
            n += add
            self._hits[key] = (start, n)
            if len(self._hits) > 10000:  # bound memory under a flood
                self._hits = {k: v for k, v in self._hits.items() if now - v[0] <= self.window}
            return n

    async def dispatch(self, request: Request, call_next):
        if self.limit <= 0 or request.method != "POST" or not request.url.path.startswith(self.PATHS):
            return await call_next(request)
        key = f"{request.client.host if request.client else '-'}:{request.url.path.split('/')[1]}"
        if self._count(key, 0) >= self.limit:
            return JSONResponse(
                {"detail": "too many attempts, slow down"}, status_code=429, headers={"Retry-After": "60"}
            )
        resp = await call_next(request)
        if resp.status_code >= 400:  # only failures count toward the limit
            self._count(key, 1)
        return resp


_TOKEN_PATH = re.compile(r"(/auth/(?:reset|magic)/)[^ \"?]+")


class _RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(_TOKEN_PATH.sub(r"\1<redacted>", a) if isinstance(a, str) else a for a in record.args)
        record.msg = _TOKEN_PATH.sub(r"\1<redacted>", record.msg) if isinstance(record.msg, str) else record.msg
        return True


def redact_tokens_in_access_log() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactTokens) for f in lg.filters):
        lg.addFilter(_RedactTokens())


def safe_next(next_: str | None) -> str:
    """Only same-origin absolute paths; blocks protocol-relative and backslash tricks (open redirect)."""
    if not next_ or not next_.startswith("/") or next_.startswith(("//", "/\\")) or any(c in next_ for c in "\r\n"):
        return "/"
    return next_
