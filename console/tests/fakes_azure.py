"""Azure test doubles (§21.6): Table Storage, Blob container, Key Vault, Resource Manager (managed identities, federated
credentials, role assignments) and the shared FakeK8s with pod logs and Envoy Gateway SecurityPolicies."""

import base64
import copy
import hashlib
import json
from types import SimpleNamespace as NS

from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError

from tests.fakes_gcp import FakeK8s

SUB, RG, LOCATION = "00000000-0000-0000-0000-000000000001", "ramen-rg", "eastus"
ACCOUNT, VAULT, ISSUER = "ramenacct", "ramen-kv", "https://oidc.example/issuer/"


# Table Storage ---------------------------------------------------------------
class FakeTable:
    def __init__(self, name):
        self.name, self.entities = name, {}

    def get_entity(self, partition_key, row_key):
        try:
            return copy.deepcopy(self.entities[(partition_key, row_key)])
        except KeyError:
            raise ResourceNotFoundError("entity not found") from None

    def upsert_entity(self, entity, mode=None):
        self.entities[(entity["PartitionKey"], entity["RowKey"])] = copy.deepcopy(dict(entity))
        return {}

    def delete_entity(self, partition_key, row_key):
        self.entities.pop((partition_key, row_key), None)

    def query_entities(self, query_filter, parameters=None):
        pk = (parameters or {}).get("pk")
        return [copy.deepcopy(e) for (p, _), e in self.entities.items() if p == pk]


class FakeTableService:
    def __init__(self):
        self.tables = {}

    def create_table_if_not_exists(self, name):
        return self.tables.setdefault(name, FakeTable(name))


# Blob ------------------------------------------------------------------------
class FakeContainer:
    def __init__(self):
        self.blobs: dict[str, bytes] = {}
        self.uploads: list[str] = []

    def list_blobs(self, name_starts_with=""):
        for name, data in sorted(self.blobs.items()):
            if name.startswith(name_starts_with or ""):
                yield NS(name=name, content_settings=NS(content_md5=bytearray(hashlib.md5(data).digest())))

    def upload_blob(self, name, data, overwrite=False, content_settings=None):
        if name in self.blobs and not overwrite:
            raise ResourceExistsError("blob exists")
        self.blobs[name] = data if isinstance(data, bytes) else data.read()
        self.uploads.append(name)

    def delete_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("no blob")
        del self.blobs[name]

    def download_blob(self, name):
        if name not in self.blobs:
            raise ResourceNotFoundError("no blob")
        return NS(readall=lambda: self.blobs[name])


# Key Vault -------------------------------------------------------------------
class FakeVault:
    def __init__(self):
        self.secrets: dict[str, list[str]] = {}
        self.deleted: dict[str, list[str]] = {}
        self.tags: dict[str, dict] = {}

    def set_secret(self, name, value, tags=None):
        if name in self.deleted:
            err = HttpResponseError("Secret is currently in a deleted but recoverable state")
            err.status_code, err.error = 409, NS(code="Conflict")
            raise err
        self.secrets.setdefault(name, []).append(value)
        self.tags[name] = dict(tags or {})
        return NS(name=name, id=f"https://{VAULT}.vault.azure.net/secrets/{name}")

    def get_secret(self, name):
        if name not in self.secrets:
            raise ResourceNotFoundError("no secret")
        return NS(name=name, value=self.secrets[name][-1])

    def begin_delete_secret(self, name):
        if name not in self.secrets:
            raise ResourceNotFoundError("no secret")
        self.deleted[name] = self.secrets.pop(name)
        return NS(wait=lambda: None, result=lambda: None)

    def begin_recover_deleted_secret(self, name):
        self.secrets[name] = self.deleted.pop(name)
        return NS(wait=lambda: None, result=lambda: None)

    def list_properties_of_secrets(self):
        return [NS(name=n, tags=self.tags.get(n, {})) for n in self.secrets]


# Resource Manager --------------------------------------------------------------
class FakeArm:
    """`request(method, path, body)` like azure_api.Arm: identities, federated credentials, role assignments."""

    def __init__(self):
        self.identities: dict[str, dict] = {}
        self.fics: dict[tuple, dict] = {}
        self.assignments: dict[str, dict] = {}
        self.calls: list[tuple] = []
        self.fail: dict[str, int] = {}  # path fragment → status to answer once

    def _ident_path(self, name):
        rg = f"/subscriptions/{SUB}/resourceGroups/{RG}"
        return f"{rg}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"

    def request(self, method, path, body=None, params=None):
        self.calls.append((method, path, body))
        for frag, status in list(self.fail.items()):
            if frag in path:
                del self.fail[frag]
                from ramen_console.cloud.azure_api import ArmError

                raise ArmError(status, f"injected {status}")
        p = path.split("?")[0]
        if "/federatedIdentityCredentials/" in p:
            ident, fic = p.split("/userAssignedIdentities/")[1].split("/federatedIdentityCredentials/")
            if method == "PUT":
                self.fics[(ident, fic)] = body
                return {"name": fic, **body}
            if method == "DELETE":
                self.fics.pop((ident, fic), None)
                return None
        if p.endswith("/userAssignedIdentities"):
            return {"value": [copy.deepcopy(v) for v in self.identities.values()]}
        if "/userAssignedIdentities/" in p:
            name = p.rsplit("/", 1)[1]
            if method == "PUT":
                cur = self.identities.get(name)
                if cur is None:
                    seed = hashlib.sha256(name.encode()).hexdigest()
                    cur = {
                        "id": self._ident_path(name),
                        "name": name,
                        "location": body["location"],
                        "tags": body.get("tags", {}),
                        "properties": {"clientId": f"cid-{seed[:12]}", "principalId": f"pid-{seed[12:24]}"},
                    }
                    self.identities[name] = cur
                return copy.deepcopy(cur)
            if method == "GET":
                if name not in self.identities:
                    from ramen_console.cloud.azure_api import ArmError

                    raise ArmError(404, "not found")
                return copy.deepcopy(self.identities[name])
            if method == "DELETE":
                self.identities.pop(name, None)
                for k in [k for k in self.fics if k[0] == name]:
                    del self.fics[k]
                return None
        if "/providers/Microsoft.Authorization/roleAssignments/" in p:
            if method == "PUT":
                scope = p.split("/providers/Microsoft.Authorization/roleAssignments/")[0]
                for a in self.assignments.values():
                    pr = a["properties"]
                    if (pr["scope"], pr["principalId"], pr["roleDefinitionId"]) == (
                        scope,
                        body["properties"]["principalId"],
                        body["properties"]["roleDefinitionId"],
                    ):
                        from ramen_console.cloud.azure_api import ArmError

                        raise ArmError(409, "RoleAssignmentExists")
                doc = {"id": p, "properties": {**body["properties"], "scope": scope}}
                self.assignments[p] = doc
                return copy.deepcopy(doc)
            if method == "DELETE":
                if self.assignments.pop(p, None) is None:
                    from ramen_console.cloud.azure_api import ArmError

                    raise ArmError(404, "RoleAssignmentNotFound")
                return None
        if p.endswith("/providers/Microsoft.Authorization/roleAssignments") and method == "GET":
            flt = (params or {}).get("$filter", "")
            pid = flt.split("'")[1] if "'" in flt else None
            return {
                "value": [
                    copy.deepcopy(a) for a in self.assignments.values() if pid in (None, a["properties"]["principalId"])
                ]
            }
        raise AssertionError(f"unexpected ARM call {method} {path}")

    def roles_of(self, principal_id) -> list[tuple[str, str, str]]:
        """(role GUID, scope, description) for one principal."""
        out = []
        for a in self.assignments.values():
            pr = a["properties"]
            if pr["principalId"] == principal_id:
                out.append((pr["roleDefinitionId"].rsplit("/", 1)[1], pr["scope"], pr.get("description", "")))
        return sorted(out)


# Kubernetes --------------------------------------------------------------------
class _AzureCore:
    def __init__(self, s, base):
        self.s, self.base = s, base

    def __getattr__(self, name):
        return getattr(self.base, name)

    def read_namespaced_pod_log(self, name, namespace, container=None, tail_lines=None, timestamps=False):
        lines = self.s.pod_logs.get((namespace, name), [])
        return "\n".join(lines[-tail_lines:] if tail_lines else lines)


class FakeAzureK8s(FakeK8s):
    def __init__(self):
        super().__init__()
        self.pod_logs: dict[tuple, list[str]] = {}

    @property
    def core(self):
        return _AzureCore(self, super().core)


class FakeAzureClients:
    """What AzureCloud reads from `self.c`: kubernetes views (FakeK8s) + Azure data/control planes."""

    def __init__(self):
        self.k8s = FakeAzureK8s()
        self.arm = FakeArm()
        self.container = FakeContainer()
        self.vault = FakeVault()
        self.tables = FakeTableService()

    def __getattr__(self, name):  # core, apps, autoscaling, custom, rbac, networking, to_dict
        if name == "to_dict":
            return lambda x: x
        if name == "networking":
            from tests.fakes_aws import _Networking

            return _Networking(self.k8s)
        return getattr(self.k8s, name)


def b64md5(data: bytes) -> str:
    return base64.b64encode(hashlib.md5(data).digest()).decode()


def jsonl(*docs) -> list[str]:
    return [json.dumps(d) for d in docs]
