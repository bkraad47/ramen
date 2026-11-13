from abc import ABC, abstractmethod
from typing import Any


class GateError(RuntimeError):
    """A deploy gate refused the canary (0.7.0: B3 schema compatibility, C4 golden cases); the adapter tears the
    canary down exactly as on a failed smoke test."""


class BuildError(RuntimeError):
    """An image build failed or timed out (§24.2). `str(e)` ends with the build's last log lines; `.log` is them."""

    def __init__(self, msg: str, log: str = ""):
        super().__init__(msg)
        self.log = log


def no_builder():
    from ..errors import ApiError

    return ApiError(
        422,
        "No image builder in this deployment: worker images cannot be built here. Terraform creates the "
        "ramen-builder identity and the chart (builder.*) sets RAMEN_BUILDER=1 and RAMEN_BUILD_REGISTRY on the console",
    )


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

    # v1.0.0 (§24.1): sleeping workers ------------------------------------
    async def sleep_zone(self, group: str, zone: str) -> dict[str, Any]:
        """Put a zone to sleep: `Deployment/worker-activator` (the zone's own worker image and config, started with
        `RAMEN_ACTIVATOR=1`) Ready behind `Service/worker`, then `worker` and `worker-canary` at 0 replicas.
        Idempotent. Kube adapters answer `{"ok", "namespace", "previous": {"workers", "canary"}, "workers", "canary",
        "activator"}` (`previous` = the replicas asked for before the call; the last three as `zone_runtime`).
        Default: nothing to do — the console only flips the zone's state."""
        return {"ok": True, "note": "no sleeping workers for this adapter"}

    async def wake_zone(self, group: str, zone: str, count: int, canary: int) -> dict[str, Any]:
        """Wake a zone: `worker` to `count` (≥ 1), `worker-canary` to `canary`, wait until they are Ready (a timeout
        names the pods that are not running), then the activator to 0. Idempotent. Default: nothing to do."""
        return {"ok": True, "note": "no sleeping workers for this adapter"}

    async def zone_runtime(self, group: str, zone: str) -> dict[str, int]:
        """Ready replicas of the zone's three Deployments: `{"workers": n, "canary": n, "activator": n}`. All 0 when
        the zone is not deployed (and for adapters that manage no pods)."""
        return {"workers": 0, "canary": 0, "activator": 0}

    # v1.0.0 (§24.2): worker images built on deploy --------------------------
    def builder_available(self) -> bool:
        """True when this deployment can build images: `RAMEN_BUILDER=1` and `RAMEN_BUILD_REGISTRY` (the repository
        prefix that holds `worker`, e.g. `<region>-docker.pkg.dev/<project>/ramen`) are set."""
        return False

    async def build_image(self, group: str, tag: str, dockerfile: str, files: dict[str, str]) -> dict[str, str]:
        """Build `<RAMEN_BUILD_REGISTRY>/worker:<tag>` from `dockerfile` and the context `files` (path → text) and
        push it; answers `{"image": ref, "log": last 40 lines}`. Raises `BuildError` when the build fails or exceeds
        `RAMEN_BUILD_TIMEOUT_SECS` (default 900), a 422 `ApiError` when there is no builder (the default)."""
        raise no_builder()
