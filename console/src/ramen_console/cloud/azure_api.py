"""Azure plumbing for the AKS adapter (§21.6, D54): a thin Resource Manager REST client (managed identities, federated
credentials, role assignments), the Blob code-bucket sync, pod-log reading, and the lazily built clients.

Resource Manager is called over plain REST with a `DefaultAzureCredential` token (workload identity on AKS): five
calls, stable API versions, nothing versioned to fake. Blob and Key Vault use their data-plane SDKs (the `[azure]`
extra)."""

import hashlib
import json
import tempfile
import threading
import uuid
from pathlib import Path

from ..errors import ApiError
from .gcp_api import _try_json, k8s_name
from .gcp_clients import GcpClients
from .gcp_k8s import log_text
from .local import redact, repo_files, run_git

ARM = "https://management.azure.com"
IDENTITY_API, ROLES_API = "2023-01-31", "2022-04-01"
FIC_AUDIENCE = "api://AzureADTokenExchange"
BASELINE, MANAGED = "ramen-baseline", "ramen-sa-permissions"  # role assignment descriptions Ramen owns

# Built-in role definition GUIDs (the same in every tenant).
ROLE_IDS = {
    "Storage Blob Data Reader": "2a2b9908-6ea1-4ae2-8e65-a410df84e7d1",
    "Storage Blob Data Contributor": "ba92f5b4-2d11-453d-a403-e96b0029c9fe",
    "Storage Table Data Contributor": "0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3",
    "Key Vault Secrets User": "4633458b-17de-408a-b874-0445c86b69e6",
    "Key Vault Crypto User": "12338af0-0e69-4776-bea7-57ae8d297424",
    "Log Analytics Contributor": "92aaf0da-9dab-42b6-94a3-d43ce8d16293",
    "Monitoring Metrics Publisher": "3913510d-42f4-4e42-8a64-420c390055eb",
    "Azure Service Bus Data Sender": "69a216fc-b8fb-44d8-bc22-1f3c2cd27a39",
    "Azure Service Bus Data Receiver": "4f6d3b9b-027b-4f4c-9142-0e5a2a2247e0",
    "Cognitive Services User": "a97b65f3-24c7-4388-baec-2e87135dc908",
    # §38.2: ACR in ABAC mode (`roleAssignmentMode: AbacRepositoryPermissions`) honours these per repository
    "Container Registry Repository Reader": "b93aa761-3e63-49ed-ac28-beffa264f7ac",
    "Container Registry Repository Writer": "2a1e307c-b015-4ebd-883e-5b7698a07328",
}
BUILD_TAG = "ramen-build-group"  # §38.2: tags a group's build identity; never `ramen-group` (zone identities only)
_REPO = "Microsoft.ContainerRegistry/registries/repositories"


def repo_condition(repository: str, actions: tuple[str, ...]) -> str:
    """§38.2: ABAC condition keeping a registry-scoped ACR repository role to one repository."""
    acts = " AND ".join(f"!(ActionMatches{{'{_REPO}/{a}'}})" for a in actions)
    return f"(({acts}) OR (@Request[{_REPO}:name] StringEqualsIgnoreCase '{repository}'))"


READ_ACTIONS = ("content/read", "metadata/read")
WRITE_ACTIONS = (*READ_ACTIONS, "content/write", "metadata/write")
BLOB_ROLES = ("Storage Blob Data Reader", "Storage Blob Data Contributor")
SECRET_ROLE = "Key Vault Secrets User"


class ArmError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(f"{status} {message}")
        self.status = status


class Arm:
    """`request(method, path, body, params)` against management.azure.com; `token` returns a bearer token."""

    def __init__(self, token, http=None, timeout=30.0):
        self.token, self.timeout = token, timeout
        self._http = http

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client()
        return self._http

    def request(self, method, path, body=None, params=None):
        p = dict(params or {})
        p.setdefault("api-version", ROLES_API if "Microsoft.Authorization" in path else IDENTITY_API)
        r = self._client().request(
            method,
            ARM + path,
            json=body,
            params=p,
            headers={"Authorization": f"Bearer {self.token()}"},
            timeout=self.timeout,
        )
        if r.status_code >= 400:
            try:
                err = (r.json() or {}).get("error") or {}
                msg = f"{err.get('code', '')}: {err.get('message', '')}".strip(": ")
            except Exception:  # noqa: BLE001
                msg = r.text[:200]
            raise ArmError(r.status_code, msg[:300])
        if r.status_code == 204 or not r.text:
            return None
        return r.json()


class Identities:
    """User-assigned managed identities in the install's resource group, their federated credentials and the role
    assignments Ramen makes for them."""

    def __init__(self, arm, subscription, resource_group, location):
        self.arm, self.sub, self.rg, self.location = arm, subscription, resource_group, location

    @property
    def rg_path(self) -> str:
        return f"/subscriptions/{self.sub}/resourceGroups/{self.rg}"

    def _path(self, name) -> str:
        return f"{self.rg_path}/providers/Microsoft.ManagedIdentity/userAssignedIdentities/{name}"

    @staticmethod
    def name(group, zone) -> str:
        return f"ramen-{group}-{zone}"[:128]

    def get(self, name) -> dict | None:
        try:
            return self.arm.request("GET", self._path(name))
        except ArmError as e:
            if e.status == 404:
                return None
            raise

    def ensure(self, name, tags) -> tuple[dict, bool]:
        if cur := self.get(name):
            return cur, False
        return self.arm.request("PUT", self._path(name), {"location": self.location, "tags": tags}), True

    def federate(self, name, issuer, subject, credential="worker") -> None:
        body = {"properties": {"issuer": issuer, "subject": subject, "audiences": [FIC_AUDIENCE]}}
        self.arm.request("PUT", f"{self._path(name)}/federatedIdentityCredentials/{credential}", body)

    def delete(self, name) -> None:
        try:
            self.arm.request("DELETE", self._path(name))
        except ArmError as e:
            if e.status != 404:
                raise

    def of_group(self, group) -> list[dict]:
        out = self.arm.request("GET", f"{self.rg_path}/providers/Microsoft.ManagedIdentity/userAssignedIdentities")
        return sorted(
            (i for i in (out or {}).get("value", []) if (i.get("tags") or {}).get("ramen-group") == group),
            key=lambda i: i["name"],
        )

    def all(self) -> list[str]:
        out = self.arm.request("GET", f"{self.rg_path}/providers/Microsoft.ManagedIdentity/userAssignedIdentities")
        return sorted(i["name"] for i in (out or {}).get("value", []) if (i.get("tags") or {}).get("ramen-group"))

    # role assignments -------------------------------------------------------
    def role_definition(self, role) -> str:
        return f"/subscriptions/{self.sub}/providers/Microsoft.Authorization/roleDefinitions/{ROLE_IDS[role]}"

    def assignments(self, principal_id) -> list[dict]:
        out = self.arm.request(
            "GET",
            f"/subscriptions/{self.sub}/providers/Microsoft.Authorization/roleAssignments",
            params={"$filter": f"principalId eq '{principal_id}'"},
        )
        return [a for a in (out or {}).get("value", []) if a["properties"].get("principalId") == principal_id]

    def grant(self, principal_id, role, scope, description=BASELINE, existing=None, condition=None) -> bool:
        """One role assignment; `condition` (§33.4a, Azure ABAC, condition version 2.0) narrows it, e.g. to a blob
        prefix."""
        rid = self.role_definition(role)
        current = existing if existing is not None else self.assignments(principal_id)
        if any(a["properties"]["roleDefinitionId"] == rid and a["properties"]["scope"] == scope for a in current):
            return False
        body = {
            "properties": {
                "roleDefinitionId": rid,
                "principalId": principal_id,
                "principalType": "ServicePrincipal",
                "description": description,
            }
        }
        if condition:
            body["properties"].update(condition=condition, conditionVersion="2.0")
        try:
            self.arm.request("PUT", f"{scope}/providers/Microsoft.Authorization/roleAssignments/{uuid.uuid4()}", body)
        except ArmError as e:
            if e.status != 409:  # RoleAssignmentExists: someone else made the same one
                raise
            return False
        return True

    def revoke(self, assignment_id) -> None:
        try:
            self.arm.request("DELETE", assignment_id)
        except ArmError as e:
            if e.status != 404:
                raise


class Acr:
    """§38.2: ACR data plane — the console removes a deleted group's repository (`DELETE /acr/v1/<repo>`). Its
    Entra token is exchanged for a registry token scoped to that one repository; Terraform's ABAC condition limits
    the console to delete under `ramen/groups/`, never push."""

    def __init__(self, server, token, http=None, timeout=30.0):
        self.server, self.token, self._http, self.timeout = server, token, http, timeout

    def _client(self):
        if self._http is None:
            import httpx

            self._http = httpx.Client()
        return self._http

    def _post(self, path, data) -> dict:
        r = self._client().post(f"https://{self.server}{path}", data=data, timeout=self.timeout)
        if r.status_code >= 400:
            raise ArmError(r.status_code, f"ACR {path}: {r.text[:200]}")
        return r.json()

    def delete_repository(self, repository) -> bool:
        refresh = self._post(
            "/oauth2/exchange", {"grant_type": "access_token", "service": self.server, "access_token": self.token()}
        )["refresh_token"]
        access = self._post(
            "/oauth2/token",
            {
                "grant_type": "refresh_token",
                "service": self.server,
                "scope": f"repository:{repository}:delete",
                "refresh_token": refresh,
            },
        )["access_token"]
        r = self._client().delete(
            f"https://{self.server}/acr/v1/{repository}",
            headers={"Authorization": f"Bearer {access}"},
            timeout=self.timeout,
        )
        if r.status_code == 404:
            return False
        if r.status_code >= 400:
            raise ArmError(r.status_code, f"ACR delete {repository}: {r.text[:200]}")
        return True


class AzureClients(GcpClients):
    """kubernetes (in-cluster, fallback KUBECONFIG; inherited) + Azure clients, each built on first use."""

    def __init__(self, subscription: str, storage_account: str, key_vault: str, container: str = "groups"):
        super().__init__(project="")
        self.subscription, self.account, self.key_vault, self.container_name = (
            subscription,
            storage_account,
            key_vault,
            container,
        )
        self._lock = threading.RLock()  # building a client asks for the shared credential under the same lock

    def _cached(self, key, make):
        with self._lock:
            if key not in self._c:
                self._c[key] = make()
            return self._c[key]

    @property
    def networking(self):
        return self._cached("networking", lambda: self._kube().NetworkingV1Api())

    @property
    def credential(self):
        def make():
            from azure.identity import DefaultAzureCredential

            return DefaultAzureCredential()

        return self._cached("credential", make)

    @property
    def arm(self):
        cred = self.credential
        return self._cached("arm", lambda: Arm(lambda: cred.get_token(f"{ARM}/.default").token))

    @property
    def container(self):
        def make():
            from azure.storage.blob import BlobServiceClient

            svc = BlobServiceClient(f"https://{self.account}.blob.core.windows.net", credential=self.credential)
            return svc.get_container_client(self.container_name)

        return self._cached("container", make)

    @property
    def vault(self):
        def make():
            from azure.keyvault.secrets import SecretClient

            return SecretClient(f"https://{self.key_vault}.vault.azure.net", credential=self.credential)

        return self._cached("vault", make)

    def acr(self, server: str) -> Acr:
        cred = self.credential
        return self._cached(f"acr:{server}", lambda: Acr(server, lambda: cred.get_token(f"{ARM}/.default").token))

    def for_cluster(self, resource_group: str, name: str):
        """§28.2: a satellite AKS cluster's clients — kubernetes bound to it, the data/control planes this bundle's."""
        return AksClients(self, resource_group, name)


AKS_API = "2024-02-01"
AKS_AUDIENCE = "6dae42f8-4368-4678-94ff-3960e28e3630"  # AKS AAD server application: the token every kubelogin presents


class AksClients(AzureClients):
    """A satellite AKS cluster's clients: `server` + CA from `listClusterUserCredential` (the ARM action is singular;
    the kubeconfig's exec block is ignored), a bearer token for the AKS audience from the console's workload identity
    (Entra integration, Azure RBAC off: the console appears as `User <its object id>`). `oidc_issuer` is the
    satellite's, for zone identities.
    The data and control planes (credential, ARM, container, vault) are the home bundle's."""

    def __init__(self, home: AzureClients, resource_group: str, name: str):
        super().__init__(home.subscription, home.account, home.key_vault, home.container_name)
        self.home, self.resource_group, self.name = home, resource_group, name

    credential = property(lambda self: self.home.credential)
    arm = property(lambda self: self.home.arm)
    container = property(lambda self: self.home.container)
    vault = property(lambda self: self.home.vault)

    @property
    def path(self) -> str:
        return (
            f"/subscriptions/{self.subscription}/resourceGroups/{self.resource_group}"
            f"/providers/Microsoft.ContainerService/managedClusters/{self.name}"
        )

    @property
    def oidc_issuer(self) -> str:
        def make():
            info = self.arm.request("GET", self.path, params={"api-version": AKS_API})
            return info["properties"]["oidcIssuerProfile"]["issuerURL"]

        return self._cached("issuer", make)

    def _kube(self):
        if "kube" not in self._c:
            import base64

            import yaml

            from .gcp_clients import remote_kube

            creds = self.arm.request("POST", self.path + "/listClusterUserCredential", params={"api-version": AKS_API})
            cluster = yaml.safe_load(base64.b64decode(creds["kubeconfigs"][0]["value"]))["clusters"][0]["cluster"]
            cred = self.credential
            ca = base64.b64decode(cluster["certificate-authority-data"])
            self._c["kube"] = remote_kube(
                self._kube_modules()[1], cluster["server"], ca, lambda: cred.get_token(f"{AKS_AUDIENCE}/.default").token
            )
        return self._c["kube"]


def sync_repo_to_blob(container, account, container_name, group, repo_url, ref, token) -> str:
    """Mirror the repo at `ref` to `<container>/<group>/`: upload new or changed files (MD5), delete the rest."""
    from azure.storage.blob import ContentSettings

    with tempfile.TemporaryDirectory() as tmp:
        r = run_git(["git", "clone", "-q", "--depth", "1", "--branch", ref, "--", repo_url, tmp], token)
        if r.returncode != 0:
            raise ApiError(502, "Git failed: " + redact(r.stderr, token).strip()[:500])
        root, prefix = Path(tmp), f"{group}/"
        local = {prefix + rel: p for rel, p in repo_files(root).items()}
        remote = {
            b.name: bytes(b.content_settings.content_md5 or b"") for b in container.list_blobs(name_starts_with=prefix)
        }
        for name, path in local.items():
            data = path.read_bytes()
            digest = hashlib.md5(data).digest()
            if remote.get(name) != digest:
                container.upload_blob(
                    name, data, overwrite=True, content_settings=ContentSettings(content_md5=bytearray(digest))
                )
        for name in remote:
            if name not in local:
                container.delete_blob(name)
    return f"az://{account}/{container_name}/{group}"


def fetch_pod_logs(kube, ns, worker, tail) -> str:
    """The workers' own stdout (§21.6: no Log Analytics dependency): JSON lines get `pod` (and keep `ts`), ordered by
    time across pods; other lines are kept verbatim; the newest `tail` lines."""
    ns = k8s_name(ns, "namespace")
    worker = k8s_name(worker, "worker") if worker else None
    if kube.read("Namespace", None, ns) is None:
        return ""
    tail = max(1, min(int(tail), 10000))
    entries: list[tuple[str, str]] = []
    for p in kube.pods(ns, "app=worker"):  # a sleeping zone's activator logs here too (`activator: true`)
        name = p["metadata"]["name"]
        if worker and name != worker:
            continue
        text = log_text(kube.c.core.read_namespaced_pod_log(name, ns, container="worker", tail_lines=tail))
        for line in text.splitlines():
            if not line.strip():
                continue
            doc = _try_json(line)
            if doc is None:
                entries.append(("", line))
                continue
            doc.setdefault("pod", name)
            entries.append((str(doc.get("ts", "")), json.dumps(doc)))
    entries.sort(key=lambda e: e[0])
    lines = [line for _, line in entries[-tail:]]
    return "\n".join(lines) + ("\n" if lines else "")
