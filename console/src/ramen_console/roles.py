"""D48 (0.7.5, CONTRACTS §21.4): custom roles. `config/roles` = `{name: {"base", "label", "oauth_only"}}`; a name is
usable wherever a group role is (memberships, role-map rules, tool-access kinds), ranks as its base in the console, is
the token's `role` claim, and reaches every worker as `RAMEN_ROLES` (`{name: base}`)."""

import json
import re
import time

from . import rbac

DOC = "roles"
NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
RESERVED = set(rbac.ROLES) | {"key"}  # `key` is the tool-access kind of a group MCP key
TTL = 5.0  # seconds a loaded registry is trusted before the store is read again (one console; many are eventual)


def clean(name: str, body: dict) -> dict:
    if not isinstance(name, str) or not NAME.match(name):
        raise ValueError("Role name: 2–32 characters, lowercase letters, digits and _, starting with a letter")
    if name in RESERVED:
        raise ValueError(f"{name!r} is a built-in role")
    body = body or {}
    base = body.get("base")
    if base not in rbac.GROUP_ROLES:
        raise ValueError(f"base must be one of {', '.join(rbac.GROUP_ROLES)}")
    label = str(body.get("label") or "").strip() or name
    return {"base": base, "label": label[:64], "oauth_only": bool(body.get("oauth_only", False))}


async def load(store, force: bool = False) -> dict[str, dict]:
    """Read `config/roles` into the rbac registry (TTL-cached on the store object) and return it."""
    now = time.monotonic()
    cached = getattr(store, "_roles_loaded", None)
    if not force and cached is not None and now - cached < TTL:
        return rbac.custom_roles()
    doc = await store.get("config", DOC) or {}
    roles = {n: r for n, r in (doc.get("roles") or {}).items() if isinstance(r, dict)}
    rbac.set_custom_roles(roles)
    store._roles_loaded = now
    return rbac.custom_roles()


async def save(store, roles: dict[str, dict]) -> dict[str, dict]:
    await store.put("config", DOC, {"roles": roles})
    rbac.set_custom_roles(roles)
    store._roles_loaded = time.monotonic()
    return rbac.custom_roles()


async def in_use(store, name: str) -> list[str]:
    """Who still refers to the role: users (either side of their memberships), role-map rules, tool-access entries."""
    where = []
    for u in await store.list("users"):
        sides = (u.get("idp_memberships") or {}, u.get("manual_memberships") or {}, u.get("memberships") or {})
        if any(name in m.values() for m in sides):
            where.append(f"user {u.get('email')}")
    auth = await store.get("config", "auth") or {}
    for provider, rules in (auth.get("role_map") or {}).items():
        for value, rule in (rules or {}).items():
            if (rule or {}).get("role") == name:
                where.append(f"role-map rule {provider}:{value}")
    for e in await store.list("environments"):
        for tool, entry in (e.get("tool_access") or {}).items():
            if name in (entry.get("list") or []) or name in (entry.get("call") or []):
                where.append(f"tool access {e.get('group')}/{e.get('name')}:{tool}")
    return where


def compact(roles: dict[str, dict] | None) -> str:
    """`RAMEN_ROLES`: canonical JSON `{name: base}`, `{}` when there are none."""
    return json.dumps({n: r["base"] for n, r in (roles or {}).items()}, separators=(",", ":"), sort_keys=True)


def oauth_only_names(roles: dict[str, dict] | None = None) -> set[str]:
    roles = rbac.custom_roles() if roles is None else roles
    return {n for n, r in roles.items() if r.get("oauth_only")}


def requires_oauth(user: dict) -> bool:
    """§21.4: a person holding an `oauth_only` role in any group (either side) may not sign in with a password."""
    names = oauth_only_names()
    if not names:
        return False
    sides = (user.get("idp_memberships") or {}, user.get("manual_memberships") or {}, user.get("memberships") or {})
    return any(r in names for m in sides for r in m.values())
