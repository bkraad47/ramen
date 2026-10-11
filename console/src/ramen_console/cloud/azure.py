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

from .. import __version__
from ..errors import ApiError
from ..policy import permissions as perm
from ..util import now
from . import azure_api
from .azure_api import BASELINE, BLOB_ROLES, MANAGED, SECRET_ROLE, AzureClients, Identities
from .azure_k8s import WI_CLIENT_ID, AzureKube, azure_manifests, securitypolicy
from .gcp import GcpCloud, detail_of
from .gcp_k8s import ns_name


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


def _open(cidrs: list[str]) -> bool:
    return not cidrs or any(c.strip() in ("0.0.0.0/0", "::/0") for c in cidrs)


class AzureCloud(GcpCloud):
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
    ):
        clients = clients or AzureClients(subscription, storage_account, key_vault, container)
        super().__init__("", location, container, image, admin_key, clients, rpc, timeout, wait_secs, poll, None)
        self.kube = AzureKube(clients, poll=poll, wait_secs=wait_secs)
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
        )

    # the k8s-side flow is the GCP one; re-guard so errors say "Azure"
    workers = _guard(GcpCloud.workers.__wrapped__)
    abort_deploy = _guard(GcpCloud.abort_deploy.__wrapped__)
    attach_zone = _guard(GcpCloud.attach_zone.__wrapped__)
    scale = _guard(GcpCloud.scale.__wrapped__)

    def bucket_uri(self, group) -> str:
        return f"az://{self.account}/{self.container}/{group}"

    def _ids(self) -> Identities:
        return Identities(self.c.arm, self.subscription, self.resource_group, self.location)

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
        the code container (Azure cannot scope a role to a blob prefix; the console's sync keeps groups apart) and
        Key Vault Secrets User on each of the group's secrets. Idempotent."""
        ns, ids = ns_name(group, zone), self._ids()
        name = ids.name(group, zone)
        ident, created = ids.ensure(name, {"ramen-group": group, "ramen-zone": zone})
        subject = f"system:serviceaccount:{ns}:worker"
        ids.federate(name, self.oidc_issuer, subject)
        pid = ident["properties"]["principalId"]
        existing = ids.assignments(pid)
        roles = []
        if ids.grant(pid, "Storage Blob Data Reader", self._container_scope(), BASELINE, existing):
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
            "workload_identity": f"{self.oidc_issuer}#{subject}",
        }

    @_guard
    async def create_service_account(self, group, zone):
        ns = ns_name(group, zone)

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

    # observe ------------------------------------------------------------
    @_guard
    async def logs(self, group, zone, worker=None, tail=500):
        return await asyncio.to_thread(azure_api.fetch_pod_logs, self.kube, ns_name(group, zone), worker, tail)

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
        await asyncio.to_thread(
            self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"}
        )
        for dep in ("worker", "worker-canary"):  # env comes from the Secret at pod start: roll the workers
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
        await asyncio.to_thread(self.kube.delete_namespace, ns)
        removed = {"namespaces": [ns], "service_accounts": []}
        if ident := await asyncio.to_thread(ids.get, ids.name(group, zone)):
            await asyncio.to_thread(self._drop_identity, ids, ident)
            removed["service_accounts"].append(ident["name"])
        return removed

    @_guard
    async def detach_group(self, group):
        removed = {"namespaces": [], "service_accounts": []}
        for ns in await asyncio.to_thread(self.kube.list_namespaces, group):
            await asyncio.to_thread(self.kube.delete_namespace, ns)
            removed["namespaces"].append(ns)
        ids = self._ids()
        for ident in await asyncio.to_thread(ids.of_group, group):
            await asyncio.to_thread(self._drop_identity, ids, ident)
            removed["service_accounts"].append(ident["name"])
        return removed

    @_guard
    async def refresh(self):
        def run():
            zones, skipped = [], []
            for n in self.c.to_dict(self.c.core.list_namespace(label_selector="ramen.io/group")).get("items", []):
                labels, ns = n["metadata"].get("labels", {}), n["metadata"]["name"]
                if (n.get("status") or {}).get("phase") == "Terminating":
                    skipped.append({"namespace": ns, "reason": "terminating"})
                    continue
                try:
                    main = self.kube.read("Deployment", ns, "worker") or {}
                    can = self.kube.read("Deployment", ns, "worker-canary") or {}
                    ksa = self.kube.read("ServiceAccount", ns, "worker") or {}
                except Exception as e:  # noqa: BLE001 - report the namespace as unreadable, keep surveying
                    skipped.append({"namespace": ns, "reason": detail_of(e)})
                    continue
                zones.append(
                    {
                        "group": labels.get("ramen.io/group"),
                        "zone": labels.get("ramen.io/zone"),
                        "namespace": ns,
                        "replicas": main.get("spec", {}).get("replicas", 0),
                        "ready": (main.get("status") or {}).get("readyReplicas", 0),
                        "canary_replicas": can.get("spec", {}).get("replicas", 0),
                        "canary_ready": (can.get("status") or {}).get("readyReplicas", 0),
                        "service_account": (ksa.get("metadata", {}).get("annotations") or {}).get(WI_CLIENT_ID),
                    }
                )
            return {
                "groups": sorted({z["group"] for z in zones if z.get("group")}),
                "zones": zones,
                "skipped": skipped,
                "service_accounts": self._ids().all(),
                "at": now(),
            }

        return await asyncio.to_thread(run)
