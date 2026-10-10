"""OAuth/OIDC providers (CONTRACTS §9).

Configured from `RAMEN_OAUTH_<NAME>_{ISSUER|METADATA_URL,CLIENT_ID,CLIENT_SECRET,SCOPES}`."""

import os

from authlib.integrations.starlette_client import OAuth

REQUIRED = ("client_id", "client_secret")
RESERVED = {"reset", "magic", "oauth", "login", "logout"}
NOT_PROVIDERS = {"RAMEN_OAUTH_ACCESS_TTL", "RAMEN_OAUTH_REFRESH_TTL"}


def metadata_url(p: dict) -> str | None:
    if p.get("metadata_url"):
        return p["metadata_url"]
    if p.get("issuer"):
        return p["issuer"].rstrip("/") + "/.well-known/openid-configuration"
    return None


def env_providers(env=None) -> dict[str, dict]:
    """`RAMEN_OAUTH_<NAME>_<FIELD>` → `{name: {field: value}}` (fields lower-cased; TTL variables are not providers)."""
    env = env or os.environ
    found: dict[str, dict] = {}
    for k, v in env.items():
        if k in NOT_PROVIDERS:  # C8 token lifetimes share the prefix but configure no provider
            continue
        if k.startswith("RAMEN_OAUTH_") and k.count("_") >= 3:
            _, _, name, field = k.split("_", 3)
            found.setdefault(name.lower(), {})[field.lower()] = v
    return found


class OAuthRegistry:
    def __init__(self, providers: dict[str, dict], transport=None):
        self._cfg = {
            n: p
            for n, p in providers.items()
            if n not in RESERVED and all(p.get(k) for k in REQUIRED) and metadata_url(p)
        }
        self._transport = transport
        self._oauth = OAuth()
        self._scopes: dict[str, str] = {}
        for name, p in self._cfg.items():
            source = (p.get("groups") or "claim").strip().lower()
            if source not in ("claim", "lookup"):
                raise ValueError(f"RAMEN_OAUTH_{name.upper()}_GROUPS must be claim or lookup, not {source!r}")
            p["groups"] = source
            self._register(name)

    def _register(self, name: str) -> None:
        """(Re)register one client; the scope follows the provider's group source (§21.3: `lookup` needs the API's)."""
        from . import groups

        p = self._cfg[name]
        scope = p.get("scopes", "openid email profile").split()
        if "openid" not in scope:  # always OIDC: authlib then sends and verifies `nonce`
            scope.insert(0, "openid")
        if p.get("groups") == "lookup":
            if not groups.supports(name):
                raise ValueError(f"group lookup is only available for {', '.join(groups.BASE)}, not {name!r}")
            if groups.SCOPES[name] not in scope:
                scope.append(groups.SCOPES[name])
        kw = {"scope": " ".join(scope), "code_challenge_method": "S256"}  # PKCE (SEC-06)
        if self._transport is not None:
            kw["transport"] = self._transport
        self._scopes[name] = kw["scope"]
        self._oauth._clients.pop(name, None)
        self._oauth._registry.pop(name, None)
        self._oauth.register(
            name,
            client_id=p["client_id"],
            client_secret=p["client_secret"],
            server_metadata_url=metadata_url(p),
            client_kwargs=kw,
        )

    @classmethod
    def from_env(cls, env=None, transport=None):
        return cls(env_providers(env), transport)

    def config(self, name: str) -> dict | None:
        """What the provider was registered from (secret included: callers mask)."""
        return dict(self._cfg[name]) if name in self._cfg else None

    @property
    def enabled(self) -> bool:
        return bool(self._cfg)

    def providers(self) -> list[str]:
        return sorted(self._cfg)

    def client(self, name: str):
        return self._oauth.create_client(name) if name in self._cfg else None

    def scope(self, name: str) -> str:
        return self._scopes.get(name, "")

    def groups_source(self, name: str) -> str:
        """`claim` (default) or `lookup` as the environment configured it; the store doc may override (settings)."""
        return (self._cfg.get(name) or {}).get("groups") or "claim"

    def set_groups_source(self, name: str, source: str) -> None:
        """The Config page flipped a provider's group source: the client is re-registered with the right scope."""
        if name not in self._cfg:
            raise KeyError(name)
        if source not in ("claim", "lookup"):
            raise ValueError("source must be claim or lookup")
        before = self._cfg[name].get("groups")
        self._cfg[name]["groups"] = source
        try:
            self._register(name)
        except ValueError:
            self._cfg[name]["groups"] = before
            raise

    def groups_url(self, name: str) -> str | None:
        """`RAMEN_OAUTH_<NAME>_GROUPS_URL`: another base for the lookup API (tests, sovereign clouds)."""
        return (self._cfg.get(name) or {}).get("groups_url") or None
