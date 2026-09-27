from abc import ABC, abstractmethod

REF_PREFIX = "sm://"


class SecretsBackend(ABC):
    kind = "store"

    @abstractmethod
    async def put(self, group, env, zone, name, value, kind="secret") -> dict:
        """Return the fields to store on the secret doc (`value` and/or `ref`)."""

    @abstractmethod
    async def resolve(self, value: str | None) -> str | None: ...

    @abstractmethod
    async def delete(self, doc: dict) -> None: ...

    async def resolve_config(self, cfg: dict[str, str]) -> dict[str, str]:
        out = {}
        for k, v in cfg.items():
            if isinstance(v, str) and REF_PREFIX in v:
                v = ",".join([await self.resolve(part) for part in v.split(",")])
            out[k] = v
        return out


class StoreBackend(SecretsBackend):
    async def put(self, group, env, zone, name, value, kind="secret"):
        return {"value": value, "ref": None}

    async def resolve(self, value):
        return value

    async def delete(self, doc):
        return None
