from abc import ABC, abstractmethod
from typing import Any


class GateError(RuntimeError):
    """A deploy gate refused the canary (0.7.0: B3 schema compatibility, C4 golden cases); the adapter tears the
    canary down exactly as on a failed smoke test."""


class Cloud(ABC):
    @abstractmethod
    async def sync_repo(self, group: str, repo_url: str, ref: str, token: str | None) -> str: ...

    @abstractmethod
    async def deploy(
        self,
        group: str,
        env: str,
        zone: str,
        canary: bool = True,
        config: dict[str, str] | None = None,
        spec: dict[str, Any] | None = None,
        log=None,
        gate=None,
    ) -> dict[str, Any]:
        """`gate(result, call)` (0.7.0) is awaited after the canary's reload + smoke with the reload result and an
        async `call(jsonrpc) -> response` bound to that pod and the group's first MCP key (None without keys); it
        raises GateError to abort before the stable track rolls. Adapters without a canary call it on each reload."""

    async def read_file(self, group: str, path: str) -> bytes | None:
        """A file of the group's synced repo (`mcp/tests.yaml`), from wherever `sync_repo` put it; None if absent."""
        return None

    @abstractmethod
    async def rebalance(self, group: str, zone: str) -> dict[str, Any]: ...

    @abstractmethod
    async def workers(self, group: str, zone: str) -> list[dict[str, Any]]: ...

    @abstractmethod
    async def logs(self, group: str, zone: str, worker: str | None = None, tail: int = 500) -> str: ...

    @abstractmethod
    async def set_ip_rules(self, group: str, zone: str, cidrs: list[str]) -> dict[str, Any]: ...

    @abstractmethod
    async def create_service_account(self, group: str, zone: str) -> dict[str, Any]: ...

    @abstractmethod
    async def refresh(self) -> dict[str, Any]: ...

    # optional (v0.2.0, additive): zone = namespace lifecycle and worker scaling; no-ops for adapters without them
    async def abort_deploy(self, group: str, zone: str) -> None:
        """Tear down any canary after a deploy that failed before/outside `deploy()`. Default: nothing to do."""
        return None

    async def detach_group(self, group: str) -> dict[str, Any]:
        """Destroy everything the cloud holds for a group. Default: nothing to do."""
        return {"namespaces": [], "service_accounts": []}

    async def attach_zone(self, group: str, zone: str, spec: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"ok": True, "note": "no zone provisioning for this adapter"}

    async def detach_zone(self, group: str, zone: str) -> dict[str, Any]:
        """Destroy one group's deployment in one zone — namespace, workers, the zone identity (0.6.0: a zone that
        is deleted or taken off every environment is really gone, not just forgotten). Default: nothing to do."""
        return {"ok": True, "note": "no zone provisioning for this adapter"}

    async def scale(self, group: str, zone: str, spec: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "note": "no scaling for this adapter"}

    async def apply_sa_permissions(
        self, group: str, zone: str, permissions: list[str], scopes: dict | None = None, previous: dict | None = None
    ) -> dict[str, Any]:
        """Bind the cloud roles mapped from approved permissions to the zone's SA (CONTRACTS §9). `scopes` names the
        resources per permission (`["*"]` = the group's own area), `previous` is what the last apply used so a
        narrowed scope can be unbound. Default: no IAM."""
        return {"ok": True, "applied": [], "permissions": list(permissions), "note": "no IAM for this adapter"}
