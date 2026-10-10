"""D50 (0.7.5, CONTRACTS §21.3): identity providers managed on the Config page. Store doc `config/oauth_providers =
{"<name>": {issuer, client_id, client_secret, scopes, groups_source}}` (the secret Fernet-encrypted at rest, never read
back); `RAMEN_OAUTH_<NAME>_*` still configures providers and the doc wins per name. `refresh()` rebuilds the app's
registry whenever the doc changed (checked at most every TTL seconds per process, at once after a change here)."""

import re
import time

from . import groups
from .oauth import RESERVED, OAuthRegistry, env_providers

DOC = "oauth_providers"
NAME = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
SOURCES = ("claim", "lookup")
TTL = 5.0
PUBLIC = ("issuer", "client_id", "scopes", "groups_source")


def clean(name: str, body: dict, existing: dict | None) -> dict:
    if not isinstance(name, str) or not NAME.match(name) or name in RESERVED:
        raise ValueError("Provider name: 2–32 lowercase letters, digits, _ or -, starting with a letter, not a route")
    body = body or {}
    issuer = str(body.get("issuer") or "").strip()
    client_id = str(body.get("client_id") or "").strip()
    if not issuer.startswith(("https://", "http://")):
        raise ValueError("issuer must be the provider's https issuer URL")
    if not client_id:
        raise ValueError("client_id is required")
    secret = body.get("client_secret")
    secret = str(secret).strip() if secret is not None else ""
    if not secret:
        secret = (existing or {}).get("client_secret") or ""
    if not secret:
        raise ValueError("client_secret is required the first time")
    source = str(body.get("groups_source") or (existing or {}).get("groups_source") or "claim").strip().lower()
    if source not in SOURCES:
        raise ValueError("groups_source must be claim or lookup")
    if source == "lookup" and not groups.supports(name):
        raise ValueError(f"group lookup is only available for {', '.join(groups.BASE)}, not {name!r}")
    scopes = str(body.get("scopes") or (existing or {}).get("scopes") or "openid email profile").strip()
    return {
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": secret,
        "scopes": scopes,
        "groups_source": source,
    }


async def load(store) -> dict[str, dict]:
    doc = await store.get("config", DOC) or {}
    return {n: dict(p) for n, p in doc.items() if isinstance(p, dict) and n != "id"}


def merged(env: dict[str, dict], doc: dict[str, dict]) -> dict[str, dict]:
    """Every provider the registry is built from: the environment's, each overridden whole by the doc's entry."""
    out = {n: dict(p) for n, p in env.items()}
    for n, p in doc.items():
        out[n] = {**p, "groups": p.get("groups_source", "claim")}
    return out


async def build(store, env=None, transport=None) -> OAuthRegistry:
    return OAuthRegistry(merged(env_providers(env), await load(store)), transport)


async def refresh(st, force: bool = False, rebuild: bool = False) -> OAuthRegistry:
    """Rebuild `st.oauth` from env + doc when the doc changed (or `force`); cheap: no network until a client is used.
    A registry somebody else installed on `st.oauth` (tests wire a fake issuer) is left alone unless `rebuild`."""
    if not rebuild and getattr(st, "_providers_built", st.oauth) is not st.oauth:
        return st.oauth
    now = time.monotonic()
    if not force and now - getattr(st, "_providers_checked", -TTL) < TTL:
        return st.oauth
    st._providers_checked = now
    doc = await load(st.store)
    if not force and doc == getattr(st, "_providers_doc", None):
        return st.oauth
    st.oauth = st._providers_built = OAuthRegistry(merged(env_providers(), doc), getattr(st, "oauth_transport", None))
    st._providers_doc = doc
    return st.oauth


async def describe(store) -> dict[str, dict]:
    """`GET`: every provider, masked — `source` says where it is set, `has_secret` whether a secret is stored."""
    env, doc = env_providers(), await load(store)
    out = {}
    for n in sorted(set(env) | set(doc)):
        p, source = (doc[n], "config") if n in doc else (env[n], "env")
        out[n] = {
            "issuer": p.get("issuer") or p.get("metadata_url") or "",
            "client_id": p.get("client_id") or "",
            "scopes": p.get("scopes") or "openid email profile",
            "groups_source": (p.get("groups_source") or p.get("groups") or "claim").lower(),
            "source": source,
            "has_secret": bool(p.get("client_secret")),
        }
    return out


async def save(st, doc: dict[str, dict]) -> dict[str, dict]:
    await st.store.put("config", DOC, doc)
    await refresh(st, force=True, rebuild=True)
    return await describe(st.store)
