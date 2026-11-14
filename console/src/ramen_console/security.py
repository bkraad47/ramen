"""Cross-cutting hardening: password and key strength, signing-secret resolution, security headers, login rate
limiting, log redaction."""

import contextlib
import json
import logging
import os
import re
import secrets
import threading
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse

from .clientip import client_ip
from .errors import invalid

log = logging.getLogger("ramen.security")
_EPHEMERAL: list[str] = []

# --- password and key strength (U3, CONTRACTS §12.1) ------------------------
MIN_LEN = 12
SPECIALS = "!#$%&()*+-:;<=>?@[]^_{}~"
# Key secrets travel through `rmn_<id>_<secret>` (split on `_`), the comma-separated `RAMEN_MCP_KEYS` and a
# `KEY=value` deploy file, so they use a narrower set: no `_`, no `,`, no `=`, no whitespace, no quotes.
KEY_SPECIALS = "!#$%&()*+-:;<>?@[]^{}~"
PASSWORD_RULE = (
    f"at least {MIN_LEN} characters with an upper-case letter, a lower-case letter, a digit and a special character"
)
_CLASSES = (
    ("an upper-case letter", re.compile(r"[A-Z]")),
    ("a lower-case letter", re.compile(r"[a-z]")),
    ("a digit", re.compile(r"[0-9]")),
    ("a special character", re.compile(r"[^A-Za-z0-9]")),
)


def min_length() -> int:
    """`RAMEN_MIN_PASSWORD_LEN` may raise the floor, never lower it (and never break on a bad value)."""
    try:
        return max(MIN_LEN, int(os.environ.get("RAMEN_MIN_PASSWORD_LEN", MIN_LEN)))
    except ValueError:
        return MIN_LEN


def password_problem(value: str) -> str | None:
    """The first unmet requirement, phrased for a person, or None when the value is strong enough."""
    least = min_length()
    if len(value or "") < least:
        return f"it is shorter than {least} characters"
    missing = [label for label, rx in _CLASSES if not rx.search(value)]
    return f"it is missing {', '.join(missing)}" if missing else None


def check_password(value: str) -> None:
    """Raise a 422 naming the rule when `value` is too weak. Used wherever a password or key is set or changed."""
    problem = password_problem(value)
    if problem:
        raise invalid(f"Password must be {PASSWORD_RULE}; {problem}.")


_LOWER, _UPPER, _DIGIT = "abcdefghijkmnopqrstuvwxyz", "ABCDEFGHJKLMNPQRSTUVWXYZ", "23456789"


def generate_password(length: int | None = None, specials: str = SPECIALS) -> str:
    """A secret that always satisfies the rule: one character of each class, the rest random, then shuffled."""
    length = max(length or min_length(), min_length())
    pools = (_LOWER, _UPPER, _DIGIT, specials)
    alphabet = "".join(pools)
    chars = [secrets.choice(p) for p in pools]
    chars += [secrets.choice(alphabet) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


def generate_key_secret(length: int = 32) -> str:
    """The secret half of an API or MCP key: same strength rule, transport-safe punctuation only."""
    return generate_password(max(length, min_length()), KEY_SPECIALS)


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


class HttpsOnlyMiddleware(BaseHTTPMiddleware):
    """0.6.0: when the console is public over HTTPS (RAMEN_COOKIE_SECURE=1), plain http never reaches a page or the
    API — a GET/HEAD is redirected to https, anything else is refused. The load balancer terminates TLS and says so
    in X-Forwarded-Proto; the kubelet's probes reach the pod directly, so the probe paths stay plain."""

    EXEMPT = ("/healthz", "/readyz")
    INTERNAL = "/api/v1/internal/"

    async def dispatch(self, request: Request, call_next):
        proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip().lower()
        if proto == "https" or request.url.path in self.EXEMPT:
            return await call_next(request)
        # §24.1: the activator's wake call comes from inside the cluster over plain http; the internal routes refuse
        # whatever an edge forwarded, so only a request no edge touched is let through here
        if request.url.path.startswith(self.INTERNAL) and "x-forwarded-for" not in request.headers:
            return await call_next(request)
        if request.method in ("GET", "HEAD"):
            from .baseuri import base_of

            if base := base_of(request):  # 0.6.1: behind a prefixing proxy, back to the public address
                query = f"?{request.url.query}" if request.url.query else ""
                return RedirectResponse(f"https{base[base.index(':') :]}{request.url.path}{query}", 301)
            return RedirectResponse(str(request.url.replace(scheme="https")), 301)
        return JSONResponse({"detail": "HTTPS required"}, 403)


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Fixed-window per-IP limit on *failed* credential attempts (login, reset, magic link). Default 20/minute;
    successful requests are never counted, so automation and test harnesses are unaffected. 0 disables."""

    PATHS = ("/login", "/auth/reset", "/auth/magic", "/oauth/token")  # token: code and refresh guessing (L5)

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
        key = f"{client_ip(request)}:{request.url.path.split('/')[1]}"  # the trusted hop, never the raw peer (SC6)
        if self._count(key, 0) >= self.limit:
            return JSONResponse(
                {"detail": "Too many attempts, slow down"}, status_code=429, headers={"Retry-After": "60"}
            )
        resp = await call_next(request)
        if resp.status_code >= 400:  # only failures count toward the limit
            self._count(key, 1)
        return resp


_TOKEN_PATH = re.compile(r"(/auth/(?:reset|magic)/)[^ \"?]+|(/oauth/authorize)\?[^ \"]+")


def _redact(text: str) -> str:
    # the authorize query carries the PKCE challenge and state; the reset/magic path carries the token itself (I1)
    return _TOKEN_PATH.sub(lambda m: f"{m.group(1)}<redacted>" if m.group(1) else f"{m.group(2)}?<redacted>", text)


class _RedactTokens(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.args and isinstance(record.args, tuple):
            record.args = tuple(_redact(a) if isinstance(a, str) else a for a in record.args)
        record.msg = _redact(record.msg) if isinstance(record.msg, str) else record.msg
        return True


_USERINFO = re.compile(r"(://)[^/@\s:]*:[^/@\s]+@")
_KNOWN: set[str] = set()


def remember_secret(value) -> None:
    """1.0.0: every secret the console reads or writes is scrubbed from any log line and deploy log afterwards."""
    if not isinstance(value, str):
        return
    if value.startswith("{"):  # telemetry headers: a JSON object whose values are the secrets
        with contextlib.suppress(ValueError):
            for v in json.loads(value).values():
                remember_secret(v)
    if len(value) >= 8:
        _KNOWN.add(value)


def scrub(text: str) -> str:
    """Credentials in URLs (`scheme://user:pass@`) and every remembered secret become `***`."""
    text = _USERINFO.sub(r"\1***@", _redact(text))
    for v in sorted((v for v in _KNOWN if v in text), key=len, reverse=True):
        text = text.replace(v, "***")
    return text


class ScrubbedLog(list):
    """A deploy job's log: each line is scrubbed as it is written."""

    def append(self, line) -> None:
        super().append(scrub(line) if isinstance(line, str) else line)


class _ScrubSecrets(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        # `args` stay in place: uvicorn's access formatter unpacks them (dropping them printed a TypeError stack
        # on every access line); each string argument and the template are scrubbed on their own.
        if isinstance(record.args, tuple) and record.args:
            record.args = tuple(scrub(a) if isinstance(a, str) else a for a in record.args)
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
        elif isinstance(record.args, dict):
            record.args = {k: scrub(v) if isinstance(v, str) else v for k, v in record.args.items()}
            if isinstance(record.msg, str):
                record.msg = scrub(record.msg)
        else:
            record.msg, record.args = scrub(record.getMessage()), None
        if record.exc_info and not record.exc_text:
            record.exc_text = scrub(logging.Formatter().formatException(record.exc_info))
        return True


def scrub_all_logs() -> None:
    """On every handler the process logs through, so no logger can bypass it."""
    for name in (None, "uvicorn", "uvicorn.error", "uvicorn.access"):
        for h in logging.getLogger(name).handlers:
            if not any(isinstance(f, _ScrubSecrets) for f in h.filters):
                h.addFilter(_ScrubSecrets())


def redact_tokens_in_access_log() -> None:
    lg = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, _RedactTokens) for f in lg.filters):
        lg.addFilter(_RedactTokens())


def safe_next(next_: str | None) -> str:
    """Only same-origin absolute paths; blocks protocol-relative and backslash tricks (open redirect)."""
    if not next_ or not next_.startswith("/") or next_.startswith(("//", "/\\")) or any(c in next_ for c in "\r\n"):
        return "/"
    return next_
