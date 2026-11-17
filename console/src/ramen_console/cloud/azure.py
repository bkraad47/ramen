"""Azure cloud adapter (§21.6, D54): AKS namespaces per zone, the group's code in a Blob container (`az://`), Envoy
Gateway routes and `SecurityPolicy` IP rules, user-assigned managed identities with workload identity, Key Vault
secrets, worker logs from the Kubernetes API. The Kubernetes-side flow (canary deploy, workers, abort) is the GCP one.

Environment: `RAMEN_AZURE_SUBSCRIPTION_ID`, `RAMEN_AZURE_RESOURCE_GROUP`, `RAMEN_AZURE_LOCATION` (default eastus),
`RAMEN_AZURE_STORAGE_ACCOUNT`, `RAMEN_AZURE_CONTAINER` (default groups), `RAMEN_AZURE_KEY_VAULT`,
`RAMEN_AZURE_OIDC_ISSUER` (the AKS OIDC issuer the zone identities trust), `RAMEN_IMAGE_WORKER`, `RAMEN_ADMIN_KEY`.
`RAMEN_AZURE_TLS` belongs to the Helm chart and Terraform (edge certificate), not to this adapter."""

import asyncio
import functools
import os
from typing import Any

from .. import __version__, imagebuild
from ..errors import ApiError
from ..policy import permissions as perm
from . import azure_api
from .azure_api import (
    BASELINE,
    BLOB_ROLES,
    BUILD_TAG,
    MANAGED,
    READ_ACTIONS,
    SECRET_ROLE,
    WRITE_ACTIONS,
    AzureClients,
    Identities,
    repo_condition,
)
from .azure_k8s import WI_CLIENT_ID, WI_USE, AzureKube, azure_manifests, securitypolicy
from .gcp import GcpCloud, detail_of
from .gcp_k8s import ACTIVATOR, BUILD_NS, ns_name


def _guard(fn):
    @functools.wraps(fn)
    async def wrapper(self, *a, **kw):
        try:
            return await fn(self, *a, **kw)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001 - upstream failure surfaced as 502, never with secret values
            raise ApiError(502, f"Azure {fn.__name__}: {type(e).__name__}: {str(e)[:300]}") from e

    return wrapper


WRITER, READER = "Container Registry Repository Writer", "Container Registry Repository Reader"
_BLOB = "Microsoft.Storage/storageAccounts/blobServices/containers/blobs"


def blob_condition(group: str) -> str:
    """§33.4a: the ABAC condition on a worker's `Storage Blob Data Reader` — reads only under `<group>/`, lists only
    with that prefix."""
    read, lst = f"ActionMatches{{'{_BLOB}/read'}}", "SubOperationMatches{'Blob.List'}"
    return (
        f"(!({read} AND NOT {lst}) OR @Resource[{_BLOB}:path] StringStartsWith '{group}/') AND "
        f"(!({read} AND {lst}) OR @Request[{_BLOB}:prefix] StringStartsWith '{group}/')"
    )


def _open(cidrs: list[str]) -> bool:
    return not cidrs or any(c.strip() in ("0.0.0.0/0", "::/0") for c in cidrs)


class AzureCloud(GcpCloud):
    PROVIDER = "Azure"
    JOB_POD_LABELS = {WI_USE: "true"}  # §33.4a: the webhook injects the `ramen-evals` identity's token into the Jobs

    def __init__(
        self,
        subscription,
        resource_group,
        location="eastus",
        storage_account="",
        key_vault="",
        oidc_issuer="",
        image="",
        admin_key="",
        container="groups",
        clients=None,
        rpc=None,
        timeout=10.0,
        wait_secs=300,
        poll=2.0,
        node_wait_secs=None,
    ):
        clients = clients or AzureClients(subscription, storage_account, key_vault, container)
        super().__init__("", location, container, image, admin_key, clients, rpc, timeout, wait_secs, poll, None)
        self.kube = AzureKube(clients, poll=poll, wait_secs=wait_secs, node_wait_secs=node_wait_secs)
        self.subscription, self.resource_group, self.location = subscription, resource_group, location
        self.account, self.key_vault, self.oidc_issuer, self.container = (
            storage_account,
            key_vault,
            oidc_issuer,
            container,
        )

    @classmethod
    def from_env(cls):
        need = {
            k: os.environ.get(k)
            for k in (
                "RAMEN_AZURE_SUBSCRIPTION_ID",
                "RAMEN_AZURE_RESOURCE_GROUP",
                "RAMEN_AZURE_STORAGE_ACCOUNT",
                "RAMEN_AZURE_KEY_VAULT",
                "RAMEN_AZURE_OIDC_ISSUER",
            )
        }
        if missing := [k for k, v in need.items() if not v]:
            raise ValueError(f"RAMEN_CLOUD=azure needs {', '.join(missing)}")
        return cls(
            need["RAMEN_AZURE_SUBSCRIPTION_ID"],
            need["RAMEN_AZURE_RESOURCE_GROUP"],
            os.environ.get("RAMEN_AZURE_LOCATION", "eastus"),
            need["RAMEN_AZURE_STORAGE_ACCOUNT"],
            need["RAMEN_AZURE_KEY_VAULT"],
            need["RAMEN_AZURE_OIDC_ISSUER"],
            os.environ.get("RAMEN_IMAGE_WORKER", f"ramen-worker:{__version__}"),
            os.environ.get("RAMEN_ADMIN_KEY", ""),
            os.environ.get("RAMEN_AZURE_CONTAINER", "groups"),
            timeout=float(os.environ.get("RAMEN_WORKER_DEADLINE", "10")),
            wait_secs=int(os.environ.get("RAMEN_DEPLOY_TIMEOUT_SECS", "300")),
            node_wait_secs=int(os.environ.get("RAMEN_NODE_WAIT_SECS", "1200")),
        )

    # the k8s-side flow is the GCP one; re-guard so errors say "Azure"
    workers = _guard(GcpCloud.workers.__wrapped__)
    abort_deploy = _guard(GcpCloud.abort_deploy.__wrapped__)
    attach_zone = _guard(GcpCloud.attach_zone.__wrapped__)
    scale = _guard(GcpCloud.scale.__wrapped__)
    zone_runtime = _guard(GcpCloud.zone_runtime.__wrapped__)
    sleep_zone = _guard(GcpCloud.sleep_zone.__wrapped__)
    wake_zone = _guard(GcpCloud.wake_zone.__wrapped__)

    # §38.2 (D85): per-group build identities ------------------------------------------------------------------
    def _acr_scope(self) -> str:
        """The registry's resource id: `RAMEN_AZURE_ACR_ID`, else the registry of the build registry's host in the
        install's resource group (Terraform puts it there)."""
        if acr_id := os.environ.get("RAMEN_AZURE_ACR_ID", "").strip():
            return acr_id
        name = self._build_registry().split("/")[0].split(".")[0]
        return f"{self._ids().rg_path}/providers/Microsoft.ContainerRegistry/registries/{name}"

    def _build_identity(self, group, repository) -> dict[str, str]:
        """Managed identity `ramen-build-<group>` (tag `ramen-build-group`, federated with KSA
        `ramen-system/ramen-build-<group>`) holding `Container Registry Repository Writer` on the registry under an
        ABAC condition naming the group's repository `ramen/groups/<group>/worker`, and `… Reader` on the base image's
        repository when it lives in the same registry. ACR creates the repository on the first push."""
        ids, name = self._ids(), f"ramen-build-{group}"[:128]
        ident, _ = ids.ensure(name, {BUILD_TAG: group})
        ids.federate(name, self.oidc_issuer, f"system:serviceaccount:{BUILD_NS}:{self.build_ksa(group)}", "build")
        pid, scope, host = ident["properties"]["principalId"], self._acr_scope(), self._build_registry().split("/")[0]
        existing = ids.assignments(pid)
        repo = repository.split("/", 1)[1]
        ids.grant(pid, WRITER, scope, BASELINE, existing, repo_condition(repo, WRITE_ACTIONS))
        base = imagebuild.repo_of(imagebuild.base_image(self))
        if base.startswith(host + "/"):
            ids.grant(pid, READER, scope, BASELINE, existing, repo_condition(base.split("/", 1)[1], READ_ACTIONS))
        return {WI_CLIENT_ID: ident["properties"]["clientId"]}

    def _drop_build_identity(self, group) -> dict[str, list[str]]:
        ids, out = self._ids(), {"repositories": [], "service_accounts": []}
        if ident := ids.get(f"ramen-build-{group}"[:128]):
            self._drop_identity(ids, ident)
            out["service_accounts"].append(ident["name"])
        repository = self.build_repository(group)
        if repository and self.c.acr(repository.split("/")[0]).delete_repository(repository.split("/", 1)[1]):
            out["repositories"].append(repository)
        return out

    def _build_extras(self, registry) -> dict:
        """kaniko's bundled `acr-env` helper exchanges the federated token the workload-identity webhook injects (the
        pod label asks for it; `ramen-build-<group>`'s KSA names the identity) for an ACR token."""
        host = registry.split("/")[0]
        return {"docker_config": {"credHelpers": {host: "acr-env"}}, "pod_labels": {WI_USE: "true"}}

    def bucket_uri(self, group) -> str:
        return f"az://{self.account}/{self.container}/{group}"

    def _ids(self) -> Identities:
        return Identities(self.c.arm, self.subscription, self.resource_group, self.location)

    # clusters (§28.2): a satellite is an AKS cluster in another location; `project` names its resource group
    def _satellite_clients(self, entry: dict):
        rg = entry.get("project") or entry.get("resource_group") or self.resource_group
        return self.c.for_cluster(rg, entry["name"])

    def _issuer(self) -> str:
        """The OIDC issuer the zone identity's federated credential trusts: the satellite's for a satellite zone."""
        return self.oidc_issuer if self.current_cluster == "home" else self.cc.oidc_issuer

    # scopes -------------------------------------------------------------
    def _container_scope(self, name=None) -> str:
        return (
            f"{self._ids().rg_path}/providers/Microsoft.Storage/storageAccounts/{self.account}"
            f"/blobServices/default/containers/{name or self.container}"
        )

    def _secret_scope(self, name) -> str:
        return f"{self._ids().rg_path}/providers/Microsoft.KeyVault/vaults/{self.key_vault}/secrets/{name}"

    def _group_secrets(self, group) -> list[str]:
        from ..secrets.azure import secret_name

        prefix = secret_name(group, "x", "x", "x").rsplit("--", 3)[0] + "--"
        return sorted(p.name for p in self.c.vault.list_properties_of_secrets() if p.name.startswith(prefix))

    # zone lifecycle -----------------------------------------------------
    def _ksa_gsa(self, group, zone) -> str | None:
        ksa = self.kube.read("ServiceAccount", ns_name(group, zone), "worker") or {}
        return (ksa.get("metadata", {}).get("annotations") or {}).get(WI_CLIENT_ID)

    def _render(self, group, zone, spec):
        image = spec.get("image") or self.image  # §13.3: the group's pinned image, else the release image
        client_id = self._ksa_gsa(group, zone) or spec.get("service_account")
        return "python", azure_manifests(group, zone, spec, image, self.bucket_uri(group), client_id)

    def _identity(self, group, zone) -> dict:
        """Managed identity `ramen-<group>-<zone>` federated with KSA `<ns>/worker`, with the baseline grants: read on
        the code container under an ABAC condition that keeps it to the group's own prefix (§33.4a; a SKU without
        ABAC rejects the condition and the error names it) and Key Vault Secrets User on each of the group's secrets.
        Idempotent."""
        ns, ids = ns_name(group, zone), self._ids()
        name = ids.name(group, zone)
        ident, created = ids.ensure(name, {"ramen-group": group, "ramen-zone": zone})
        subject, issuer = f"system:serviceaccount:{ns}:worker", self._issuer()
        ids.federate(name, issuer, subject)
        pid = ident["properties"]["principalId"]
        existing = ids.assignments(pid)
        roles = []
        if ids.grant(
            pid, "Storage Blob Data Reader", self._container_scope(), BASELINE, existing, blob_condition(group)
        ):
            roles.append("Storage Blob Data Reader")
        for sid in self._group_secrets(group):
            if ids.grant(pid, SECRET_ROLE, self._secret_scope(sid), BASELINE, existing):
                roles.append(f"{SECRET_ROLE} on {sid}")
        return {
            "name": ident["properties"]["clientId"],
            "identity": name,
            "principal_id": pid,
            "created": created,
            "roles": roles,
            "ksa": f"{ns}/worker",
            "workload_identity": f"{issuer}#{subject}",
        }

    def home_region(self) -> str | None:
        return self.location

    @_guard
    async def create_service_account(self, group, zone):
        ns = ns_name(group, zone)
        await self._select(zone)

        def run():
            out = self._identity(group, zone)
            self._ensure_namespace(group, zone)
            self.kube.apply(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {"name": "worker", "namespace": ns, "annotations": {WI_CLIENT_ID: out["name"]}},
                }
            )
            return out

        return await asyncio.to_thread(run)

    def bind_new_secret(self, scope_suffix, group) -> bool:
        """Secrets backend hook: a new group secret is readable by every zone identity of the group (§21.6)."""
        ids, name = self._ids(), scope_suffix.rsplit("/", 1)[-1]
        idents = ids.of_group(group)
        for i in idents:
            ids.grant(i["properties"]["principalId"], SECRET_ROLE, self._secret_scope(name), BASELINE)
        return bool(idents)

    def _wanted(self, group, permissions, scopes) -> set[tuple[str, str]]:
        out = set()
        for role, names in perm.role_scopes(permissions, scopes, "azure").items():
            if role in BLOB_ROLES:
                out |= {(role, self._container_scope(None if n == "*" else n)) for n in names}
            elif role == SECRET_ROLE:
                secrets = self._group_secrets(group) if "*" in names else names
                out |= {(role, self._secret_scope(s)) for s in secrets}
            else:
                out.add((role, self._ids().rg_path))
        return out

    @_guard
    async def apply_sa_permissions(self, group, zone, permissions, scopes=None, previous=None):
        """Role assignments for the mapped Azure roles, described `ramen-sa-permissions` and replaced whole: what is no
        longer wanted is deleted, the baseline (`ramen-baseline`) is never touched. Blob roles bind the group container
        or the named containers, Key Vault Secrets User the group's secrets or the named ones; the rest bind the
        resource group and cannot be scoped further."""
        ns = ns_name(group, zone)
        sa = await self.create_service_account(group, zone)

        def run():
            ids, pid = self._ids(), sa["principal_id"]
            wanted = self._wanted(group, permissions, scopes)
            rid = {ids.role_definition(r): r for r in azure_api.ROLE_IDS}
            current = ids.assignments(pid)
            revoked = []
            for a in current:
                pr = a["properties"]
                key = (rid.get(pr["roleDefinitionId"]), pr["scope"])
                if pr.get("description") == MANAGED and key not in wanted:
                    ids.revoke(a["id"])
                    revoked.append(key[0])
            mine = {
                (rid.get(a["properties"]["roleDefinitionId"]), a["properties"]["scope"])
                for a in current
                if a["properties"].get("description") == MANAGED
            }
            applied = []
            for role, scope in sorted(wanted - mine):
                ids.grant(pid, role, scope, MANAGED, existing=[])
                applied.append(role)
            out = {
                "ok": True,
                "service_account": sa["name"],
                "applied": sorted(set(applied) | {r for r, _ in wanted}),
                "permissions": list(permissions),
                "scopes": dict(scopes or {}),
                "ksa": f"{ns}/worker",
            }
            if revoked:
                out["revoked"] = sorted(set(revoked))
            if unscoped := perm.unscoped(permissions, scopes):
                out["unscoped"] = unscoped
            return out

        return await asyncio.to_thread(run)

    # repo sync ----------------------------------------------------------
    @_guard
    async def sync_repo(self, group, repo_url, ref, token):
        return await asyncio.to_thread(
            azure_api.sync_repo_to_blob, self.c.container, self.account, self.container, group, repo_url, ref, token
        )

    @_guard
    async def read_file(self, group, path):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return await asyncio.to_thread(lambda: self.c.container.download_blob(f"{group}/{path}").readall())
        except ResourceNotFoundError:
            return None

    @_guard
    async def list_files(self, group, prefix):
        def run():
            blobs = self.c.container.list_blobs(name_starts_with=f"{group}/{prefix}")
            return sorted(b.name[len(group) + 1 :] for b in blobs)

        return await asyncio.to_thread(run)

    @_guard
    async def delete_files(self, group):
        from azure.core.exceptions import ResourceNotFoundError

        def run():
            names = [b.name for b in self.c.container.list_blobs(name_starts_with=f"{group}/")]
            for n in names:
                try:
                    self.c.container.delete_blob(n)
                except ResourceNotFoundError:
                    pass
            return len(names)

        return await asyncio.to_thread(run)

    # observe ------------------------------------------------------------
    @_guard
    async def logs(self, group, zone, worker=None, tail=500):
        kube = await self._select(zone)
        return await asyncio.to_thread(azure_api.fetch_pod_logs, kube, ns_name(group, zone), worker, tail)

    # traffic ------------------------------------------------------------
    @_guard
    async def rebalance(self, group, zone):
        """The zone Service selects both tracks and Envoy balances across its endpoints, so there is no load-balancer
        weight to move; replicas are the console's autoscaler's (§21.7), so this reports the zone's load only."""
        ws = await self.workers(group, zone)
        loads = [w["load"] for w in ws]
        load = "high" if "high" in loads else ("low" if loads and all(x in ("low", "down") for x in loads) else "even")
        out: dict[str, Any] = {"ok": True, "load": load, "applied": False, "note": "Envoy balances by endpoint"}
        return out

    @_guard
    async def set_ip_rules(self, group, zone, cidrs):
        """The node enforces the list (authoritative, needs no cloud API); the zone route's Envoy `SecurityPolicy`
        mirrors it at the edge, best effort. An open list removes the edge policy."""
        ns, cidrs = ns_name(group, zone), list(cidrs)
        await self._select(zone)
        await asyncio.to_thread(
            self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"}
        )
        # env comes from the Secret at pod start: roll the workers (and a sleeping zone's activator, which enforces too)
        for dep in ("worker", "worker-canary", ACTIVATOR):
            d = await asyncio.to_thread(self.kube.read, "Deployment", ns, dep)
            if d and d.get("spec", {}).get("replicas", 0) > 0:
                await asyncio.to_thread(self.kube.restart, ns, dep)
                await asyncio.to_thread(self.kube.wait_ready, ns, dep)
        out = {"ok": True, "policy": f"{ns}/worker", "cidrs": cidrs, "attached": False}
        try:
            if _open(cidrs):
                await asyncio.to_thread(self.kube.delete_security_policy, ns)
            else:
                await asyncio.to_thread(self.kube.apply, securitypolicy(group, zone, cidrs))
                out["attached"] = True
        except Exception as e:  # noqa: BLE001 - the node already enforces; the edge rule is best effort
            out["note"] = f"enforced at the node; edge policy not updated: {detail_of(e)}"
        return out

    # teardown -----------------------------------------------------------
    def _drop_identity(self, ids, ident) -> None:
        for a in ids.assignments(ident["properties"]["principalId"]):
            ids.revoke(a["id"])
        ids.delete(ident["name"])

    @_guard
    async def detach_zone(self, group, zone):
        """Destroy one group's deployment in one zone: its namespace (workers, routes and policies go with it), the
        zone identity and every role assignment it holds."""
        ns, ids = ns_name(group, zone), self._ids()
        await self._select(zone)
        await asyncio.to_thread(self.kube.delete_namespace, ns)
        removed = {"namespaces": [ns], "service_accounts": []}
        if ident := await asyncio.to_thread(ids.get, ids.name(group, zone)):
            await asyncio.to_thread(self._drop_identity, ids, ident)
            removed["service_accounts"].append(ident["name"])
        return removed

    @_guard
    async def detach_group(self, group):
        removed = {"namespaces": [], "service_accounts": []}
        for cluster in await self._all_clusters():  # §28.2: every cluster's namespaces of the group
            kube = self._use(cluster)
            for ns in await asyncio.to_thread(kube.list_namespaces, group):
                await asyncio.to_thread(kube.delete_namespace, ns)
                removed["namespaces"].append(ns)
        self._use("home")
        ids = self._ids()
        for ident in await asyncio.to_thread(ids.of_group, group):
            await asyncio.to_thread(self._drop_identity, ids, ident)
            removed["service_accounts"].append(ident["name"])
        await self._drop_builder(group, removed)
        return removed

    SA_ANNOTATION = WI_CLIENT_ID

    def _accounts(self) -> list:
        return self._ids().all()

    refresh = _guard(GcpCloud.refresh.__wrapped__)
