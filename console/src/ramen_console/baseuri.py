"""0.6.1: `config/base_uri` — the console's public address (`scheme://host[:port][/prefix]`, super admin, Config page).
When set, every link the console generates goes through it, so it can sit behind a proxy that forwards `<base>/…` to
its root. Requests still arrive at the root; only generated links change. Unset, links are byte-identical to before."""

import os
import time
from urllib.parse import urlsplit

from jinja2 import pass_context
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request

DOC = "base_uri"
TTL = 5.0  # seconds a replica trusts its cached value; a save on this replica applies at once


def normalize(value: str | None) -> str:
    """'' (unset) or an absolute http(s) URL with an optional path prefix, no query/fragment/credentials, no trailing
    slash. Raises ValueError with a message fit for the Config page."""
    v = (value or "").strip()
    if not v:
        return ""
    if any(c.isspace() or ord(c) < 32 for c in v) or "?" in v or "#" in v:
        raise ValueError("Base URI must be an absolute http(s) URL with no spaces, query or fragment")
    u = urlsplit(v)
    try:
        u.port  # noqa: B018 - raises on a malformed port
    except ValueError as e:
        raise ValueError(f"Base URI has an invalid port: {e}") from e
    if u.scheme not in ("http", "https") or not u.hostname:
        raise ValueError("Base URI must be an absolute http(s) URL, e.g. https://ops.example.com/ramen")
    if u.username or u.password:
        raise ValueError("Base URI must not carry credentials")
    return v.rstrip("/")


async def load(store) -> str:
    return (await store.get("config", DOC) or {}).get("base_uri") or ""


class Cache:
    def __init__(self):
        self.value, self.loaded = "", 0.0

    def set(self, value: str) -> None:
        self.value, self.loaded = value, time.monotonic()

    async def refresh(self, store) -> str:
        if time.monotonic() - self.loaded >= TTL:
            try:
                self.set(await load(store))
            except Exception:  # noqa: BLE001 - a store blip keeps the last known value; links must not 500
                self.loaded = time.monotonic()
        return self.value


class BaseUriMiddleware(BaseHTTPMiddleware):
    """Outermost: puts the current base on `request.state.base_uri` for every helper below."""

    async def dispatch(self, request: Request, call_next):
        st = request.app.state
        request.state.base_uri = await st.base_uri.refresh(st.store)
        return await call_next(request)


def base_of(request: Request) -> str:
    return getattr(request.state, "base_uri", "") or ""


def prefix(request: Request) -> str:
    """The path part of the base ('' when unset or host-only): what every root-relative link is prefixed with."""
    base = base_of(request)
    return urlsplit(base).path.rstrip("/") if base else ""


def link(request: Request, path: str) -> str:
    """A root path (`/login?next=…`) as the browser must request it."""
    return prefix(request) + path


def public_url(request: Request) -> str:
    """Absolute console address: the setting, else RAMEN_PUBLIC_URL, else what the request says."""
    return (base_of(request) or os.environ.get("RAMEN_PUBLIC_URL") or str(request.base_url)).rstrip("/")


@pass_context
def u(ctx, path: str) -> str:
    """Jinja: `{{ u('/groups') }}`; `{{ base }}` is the bare prefix for literal attribute values."""
    return ctx.get("base", "") + path


def context(request: Request) -> dict:
    """Jinja2Templates context processor: every TemplateResponse gets `base`."""
    return {"base": prefix(request)}
