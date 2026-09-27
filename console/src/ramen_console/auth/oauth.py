import os

from authlib.integrations.starlette_client import OAuth

REQUIRED = ("client_id", "client_secret", "metadata_url")


class OAuthRegistry:
    def __init__(self, providers: dict[str, dict]):
        self._cfg = {n: p for n, p in providers.items() if all(p.get(k) for k in REQUIRED)}
        self._oauth = OAuth()
        for name, p in self._cfg.items():
            self._oauth.register(name, client_id=p["client_id"], client_secret=p["client_secret"],
                                 server_metadata_url=p["metadata_url"],
                                 client_kwargs={"scope": p.get("scopes", "openid email profile")})

    @classmethod
    def from_env(cls, env=None):
        env = env or os.environ
        found: dict[str, dict] = {}
        for k, v in env.items():
            if k.startswith("RAMEN_OAUTH_") and k.count("_") >= 3:
                _, _, name, field = k.split("_", 3)
                found.setdefault(name.lower(), {})[field.lower()] = v
        return cls(found)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg)

    def providers(self) -> list[str]:
        return sorted(self._cfg)

    def client(self, name: str):
        return self._oauth.create_client(name) if name in self._cfg else None
