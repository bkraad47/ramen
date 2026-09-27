from abc import ABC, abstractmethod
from typing import Any


class Cloud(ABC):
    @abstractmethod
    async def sync_repo(self, group: str, repo_url: str, ref: str, token: str | None) -> str: ...

    @abstractmethod
    async def deploy(self, group: str, env: str, zone: str, canary: bool = True,
                     config: dict[str, str] | None = None) -> dict[str, Any]: ...

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
