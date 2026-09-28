"""OAuth/OIDC providers (CONTRACTS §9).

Configured from `RAMEN_OAUTH_<NAME>_{ISSUER|METADATA_URL,CLIENT_ID,CLIENT_SECRET,SCOPES}`."""

import os

from authlib.integrations.starlette_client import OAuth

REQUIRED = ("client_id", "client_secret")
RESERVED = {"reset", "magic", "oauth", "login", "logout"}


def metadata_url(p: dict) -> str | None:
    if p.get("metadata_url"):
        return p["metadata_url"]
    if p.get("issuer"):
        return p["issuer"].rstrip("/") + "/.well-known/openid-configuration"
    return None


class OAuthRegistry:
    def __init__(self, providers: dict[str, dict], transport=None):
        self._cfg = {
            n: p
            for n, p in providers.items()
            if n not in RESERVED and all(p.get(k) for k in REQUIRED) and metadata_url(p)
        }
        self._oauth = OAuth()
        for name, p in self._cfg.items():
            scope = p.get("scopes", "openid email profile").split()
            if "openid" not in scope:  # always OIDC: authlib then sends and verifies `nonce`
                scope.insert(0, "openid")
            kw = {"scope": " ".join(scope), "code_challenge_method": "S256"}  # PKCE (SEC-06)
            if transport is not None:
                kw["transport"] = transport
            self._oauth.register(
                name,
                client_id=p["client_id"],
                client_secret=p["client_secret"],
                server_metadata_url=metadata_url(p),
                client_kwargs=kw,
            )

    @classmethod
    def from_env(cls, env=None, transport=None):
        env = env or os.environ
        found: dict[str, dict] = {}
        for k, v in env.items():
            if k.startswith("RAMEN_OAUTH_") and k.count("_") >= 3:
                _, _, name, field = k.split("_", 3)
                found.setdefault(name.lower(), {})[field.lower()] = v
        return cls(found, transport)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg)

    def providers(self) -> list[str]:
        return sorted(self._cfg)

    def client(self, name: str):
        return self._oauth.create_client(name) if name in self._cfg else None
