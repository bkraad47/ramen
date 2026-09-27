"""Secrets Manager backend (CONTRACTS §8, untested on a real account): values live in AWS Secrets Manager as
ramen/<group>/<env|all>/<zone|all>/<NAME> (tagged group/env/zone); the store keeps name + `asm://` ref only."""
import asyncio
import os

from ..cloud.aws_api import aws_error_code
from ..errors import ApiError
from .base import SecretsBackend

REF = "asm://"  # contains base.REF_PREFIX ("sm://"), so resolve_config() routes it here


def secret_name(group, env, zone, name, kind="secret") -> str:
    n = name if kind == "secret" else f"mcp-{name}"
    return f"ramen/{group}/{env or 'all'}/{zone or 'all'}/{n}"


class AwsSecrets(SecretsBackend):
    kind = "aws"

    def __init__(self, region, client):
        self.region, self.client = region, client

    def _put(self, group, env, zone, name, value, kind):
        sid = secret_name(group, env, zone, name, kind)
        tags = [{"Key": "ramen", "Value": "secret"}, {"Key": "group", "Value": group},
                {"Key": "env", "Value": env or "all"}, {"Key": "zone", "Value": zone or "all"}]
        try:
            self.client.create_secret(Name=sid, SecretString=value, Tags=tags)
        except Exception as e:  # noqa: BLE001
            code = aws_error_code(e)
            if code not in ("ResourceExistsException", "InvalidRequestException"):
                raise
            # re-created within the recovery window: AWS answers InvalidRequest, moto ResourceExists + DeletedDate
            if code == "InvalidRequestException" or self.client.describe_secret(SecretId=sid).get("DeletedDate"):
                self.client.restore_secret(SecretId=sid)
            self.client.put_secret_value(SecretId=sid, SecretString=value)
        return {"value": None, "ref": f"{REF}{sid}"}

    async def put(self, group, env, zone, name, value, kind="secret"):
        try:
            return await asyncio.to_thread(self._put, group, env, zone, name, value, kind)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"secrets manager: {type(e).__name__}: {str(e)[:200]}")

    def _resolve(self, ref):
        return self.client.get_secret_value(SecretId=ref[len(REF):])["SecretString"]

    async def resolve(self, value):
        if not value or not value.startswith(REF):
            return value
        try:
            return await asyncio.to_thread(self._resolve, value)
        except Exception as e:  # noqa: BLE001
            raise ApiError(502, f"secrets manager: cannot read {value}: {type(e).__name__}")

    async def delete(self, doc):
        ref = doc.get("ref")
        if not ref or not ref.startswith(REF):
            return
        try:
            await asyncio.to_thread(self.client.delete_secret, SecretId=ref[len(REF):], ForceDeleteWithoutRecovery=True)
        except Exception as e:  # noqa: BLE001
            if aws_error_code(e) != "ResourceNotFoundException":
                raise ApiError(502, f"secrets manager: {type(e).__name__}: {str(e)[:200]}")


def from_env(cloud=None) -> AwsSecrets:
    region = os.environ.get("RAMEN_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
    client = getattr(getattr(cloud, "c", None), "secretsmanager", None)
    if client is None:
        from ..cloud.aws_api import AwsClients
        client = AwsClients(region).secretsmanager
    return AwsSecrets(region, client)
