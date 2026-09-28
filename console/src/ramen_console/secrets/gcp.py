"""Secret Manager backend: values live in SM as ramen-<group>-<env>-<zone>-<NAME>; the store keeps name + ref only."""

import asyncio

from ..cloud.gcp_clients import http_status
from ..errors import ApiError
from .base import REF_PREFIX, SecretsBackend


def secret_id(group, env, zone, name, kind="secret") -> str:
    n = name if kind == "secret" else f"mcp-{name}"
    return f"ramen-{group}-{env or 'all'}-{zone or 'all'}-{n}"


class GcpSecrets(SecretsBackend):
    kind = "gcp"

    def __init__(self, project, client):
        self.project, self.client = project, client

    def _put(self, group, env, zone, name, value, kind):
        sid = secret_id(group, env, zone, name, kind)
        parent = f"projects/{self.project}"
        labels = {"ramen": "secret", "group": group, "env": env or "all", "zone": zone or "all"}
        try:
            self.client.create_secret(
                request={
                    "parent": parent,
                    "secret_id": sid,
                    "secret": {"replication": {"automatic": {}}, "labels": labels},
                }
            )
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 409:
                raise
        self.client.add_secret_version(
            request={"parent": f"{parent}/secrets/{sid}", "payload": {"data": value.encode()}}
        )
        return {"value": None, "ref": f"{REF_PREFIX}{parent}/secrets/{sid}"}

    async def put(self, group, env, zone, name, value, kind="secret"):
        try:
            return await asyncio.to_thread(self._put, group, env, zone, name, value, kind)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"secret manager: {type(e).__name__}: {str(e)[:200]}") from e

    def _resolve(self, ref):
        r = self.client.access_secret_version(request={"name": f"{ref[len(REF_PREFIX) :]}/versions/latest"})
        return r.payload.data.decode()

    async def resolve(self, value):
        if not value or not value.startswith(REF_PREFIX):
            return value
        try:
            return await asyncio.to_thread(self._resolve, value)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"secret manager: cannot read {value}: {type(e).__name__}") from e

    async def delete(self, doc):
        ref = doc.get("ref")
        if not ref:
            return
        try:
            await asyncio.to_thread(self.client.delete_secret, request={"name": ref[len(REF_PREFIX) :]})
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 404:
                raise ApiError(502, f"secret manager: {type(e).__name__}: {str(e)[:200]}") from e
