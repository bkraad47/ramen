"""GCP cloud adapter (CONTRACTS §7): GKE namespaces per zone, GCS group bucket, Cloud Logging, Cloud Armor, IAM."""

import asyncio
import functools
import os
from typing import Any

from .. import __version__, grpcclient
from ..errors import ApiError
from ..grpcclient import GrpcError
from ..policy import permissions as perm
from ..util import now
from . import gcp_api
from .base import Cloud
from .gcp_clients import GcpClients, http_status
from .gcp_k8s import PORT, Kube, helm_available, helm_manifests, manifests, ns_name, rolebinding
from .local import worker_tls_from_env


def _guard(fn):
    @functools.wraps(fn)
    async def wrapper(self, *a, **kw):
        try:
            return await fn(self, *a, **kw)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001 - upstream failure surfaced as 502, never with secret values
            raise ApiError(502, f"gcp {fn.__name__}: {type(e).__name__}: {str(e)[:300]}") from e

    return wrapper


class GcpCloud(Cloud):
    def __init__(
        self,
        project,
        region="us-central1",
        bucket="",
        image="",
        admin_key="",
        clients=None,
        rpc=None,
        timeout=10.0,
        wait_secs=300,
        poll=2.0,
        chart=None,
    ):
        self.project, self.region, self.bucket, self.image, self.admin_key = project, region, bucket, image, admin_key
        self.c = clients or GcpClients(project)
        self.kube = Kube(self.c, poll=poll, wait_secs=wait_secs)
        self.chart = chart
        self.rpc = rpc or grpcclient.Client(tls=worker_tls_from_env(), deadline=timeout)

    @classmethod
    def from_env(cls):
        project = os.environ.get("RAMEN_GCP_PROJECT")
        if not project:
            raise ValueError("RAMEN_CLOUD=gcp needs RAMEN_GCP_PROJECT")
        return cls(
            project,
            os.environ.get("RAMEN_GCP_REGION", "us-central1"),
            os.environ.get("RAMEN_GROUPS_BUCKET", f"ramen-{project}-groups"),
            os.environ.get("RAMEN_IMAGE_WORKER", f"ramen-worker:{__version__}"),
            os.environ.get("RAMEN_ADMIN_KEY", ""),
            timeout=float(os.environ.get("RAMEN_WORKER_DEADLINE", "10")),
            wait_secs=int(os.environ.get("RAMEN_DEPLOY_TIMEOUT_SECS", "300")),
            chart=os.environ.get("RAMEN_WORKER_CHART"),
        )

    def bucket_uri(self, group) -> str:
        return f"gs://{self.bucket}/{group}"

    # zone lifecycle -----------------------------------------------------
    def _render(self, group, zone, spec):
        ns = ns_name(group, zone)
        ksa = self.kube.read("ServiceAccount", ns, "worker") or {}
        gsa = (ksa.get("metadata", {}).get("annotations") or {}).get("iam.gke.io/gcp-service-account") or spec.get(
            "service_account"
        )
        if helm_available(self.chart):
            return "helm", helm_manifests(
                self.chart, self.project, group, zone, spec, self.image, self.bucket_uri(group), gsa
            )
        return "python", manifests(group, zone, spec, self.image, self.bucket_uri(group), gsa)

    def _ensure_namespace(self, group, zone) -> None:
        """Namespace + the RoleBinding that scopes the console's zone permissions to it (SEC-09)."""
        ns = ns_name(group, zone)
        self.kube.apply(
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}},
            }
        )
        self.kube.apply(rolebinding(group, zone))
        self.kube.wait_rbac(ns)  # the binding takes a moment to be honoured (API-server RBAC cache)

    def _ksa_gsa(self, group, zone) -> str | None:
        ksa = self.kube.read("ServiceAccount", ns_name(group, zone), "worker") or {}
        return (ksa.get("metadata", {}).get("annotations") or {}).get("iam.gke.io/gcp-service-account")

    def _attach(self, group, zone, spec) -> dict:
        # Namespace + RoleBinding first: _render reads the zone's KSA, which the console may only do once its
        # RoleBinding exists in that namespace (SEC-09; the API server answers 403, not 404, before that).
        self._ensure_namespace(group, zone)
        spec = dict(spec or {})
        if not spec.get("service_account") and not self._ksa_gsa(group, zone):
            # CONTRACTS §7: the console creates the per-zone GSA; without it workers have no GCS/Secret identity
            spec["service_account"] = self._identity(group, zone)["name"]
        renderer, docs = self._render(group, zone, spec)
        docs.insert(1, rolebinding(group, zone))  # right after the Namespace: everything else needs it
        for d in docs:
            self.kube.apply(d, keep=("replicas",) if d["metadata"]["name"] == "worker-canary" else ())
        return {
            "ok": True,
            "namespace": ns_name(group, zone),
            "renderer": renderer,
            "applied": [d["kind"] for d in docs],
        }

    @_guard
    async def attach_zone(self, group, zone, spec=None) -> dict:
        return await asyncio.to_thread(self._attach, group, zone, spec)

    @_guard
    async def scale(self, group, zone, spec) -> dict:
        return await asyncio.to_thread(self._attach, group, zone, spec)

    # repo sync ----------------------------------------------------------
    @_guard
    async def sync_repo(self, group, repo_url, ref, token):
        return await asyncio.to_thread(
            gcp_api.sync_repo_to_gcs, self.c.storage, self.bucket, group, repo_url, ref, token
        )

    # deploy -------------------------------------------------------------
    def _target(self, pod) -> str:
        return f"{pod['status'].get('podIP')}:{PORT}"

    async def _reload_and_smoke(self, pod, mcp_key, group, zone, log) -> dict:
        """Admin/Reload, then Mcp/Call tools/list with the first MCP key (Health/Check when the group has no keys)."""
        target, name = self._target(pod), pod["metadata"]["name"]
        try:
            result = await self.rpc.reload(target, self.admin_key)
        except GrpcError as e:
            raise RuntimeError(f"reload on {name} returned {e.code.name}: {e.details[:200]}") from None
        log(f"{name}: reload ok ({len(result.get('tools', []))} tools, {len(result.get('errors', []))} errors)")
        if mcp_key:
            try:
                r = await self.rpc.call(
                    target, mcp_key, group, zone, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
                )
            except GrpcError as e:
                raise RuntimeError(f"smoke test on {name} failed: {e.code.name} {e.details[:200]}") from None
            if "result" not in r:
                raise RuntimeError(f"smoke test on {name} failed: {str(r.get('error'))[:200]}")
        elif not await self.rpc.health(target):
            raise RuntimeError(f"smoke test on {name} failed: health not SERVING")
        log(f"{name}: smoke ok ({'tools/list' if mcp_key else 'health SERVING, no MCP keys configured'})")
        return result

    async def deploy(self, group, env, zone, canary=True, config=None, spec=None, log=None) -> dict[str, Any]:
        lines: list[str] = []

        def note(msg):
            lines.append(f"{now()} {msg}")
            if log:
                log(lines[-1])

        ns, config = ns_name(group, zone), dict(config or {})
        workers = []
        try:
            note(f"{ns}: applying zone manifests")
            await asyncio.to_thread(self._attach, group, zone, spec or {})
            existing = await asyncio.to_thread(self.kube.secret_values, ns, "ramen-deploy")
            data = {"RAMEN_GROUP": group, "RAMEN_ENV": env, "RAMEN_ZONE": zone}
            if self.admin_key:
                data["RAMEN_ADMIN_KEY"] = self.admin_key
            if "RAMEN_ALLOWED_CIDRS" in existing and "RAMEN_ALLOWED_CIDRS" not in config:
                data["RAMEN_ALLOWED_CIDRS"] = existing["RAMEN_ALLOWED_CIDRS"]
            data.update(config)
            await asyncio.to_thread(self.kube.merge_secret, ns, "ramen-deploy", data, True)
            note(f"{ns}: secret ramen-deploy written ({len(data)} keys)")
            mcp_key = (config.get("RAMEN_MCP_KEYS") or "").split(",")[0].strip() or None
            if canary:
                await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 1)
                await asyncio.to_thread(self.kube.restart, ns, "worker-canary")
                note("canary: restarted worker-canary, waiting for ready")
                await asyncio.to_thread(self.kube.wait_ready, ns, "worker-canary")
                pods = await asyncio.to_thread(self.kube.pods, ns, "app=worker,ramen.io/track=canary")
                if not pods:
                    raise RuntimeError("canary ready but no canary pod found")
                for p in pods:
                    result = await self._reload_and_smoke(p, mcp_key, group, zone, note)
                    workers.append({"id": p["metadata"]["name"], "ok": True, "track": "canary", "result": result})
            await asyncio.to_thread(self.kube.restart, ns, "worker")
            note("main: restarted worker, waiting for ready")
            d = await asyncio.to_thread(self.kube.wait_ready, ns, "worker")
            # old pods keep serving (with the old key set) until drained; the job is only "ok" once they are gone
            drained = await asyncio.to_thread(self.kube.wait_terminated, ns, "app=worker", 180)
            note("main: old pods drained" if drained else "main: old pods still terminating after 180s")
            for p in await asyncio.to_thread(self.kube.pods, ns, "app=worker,ramen.io/track=stable"):
                workers.append({"id": p["metadata"]["name"], "ok": True, "track": "stable", "status": "OK"})
            note(f"main: {d.get('status', {}).get('readyReplicas', 0)} ready")
            return {"ok": True, "workers": workers, "namespace": ns, "log": lines}
        except Exception as e:  # noqa: BLE001 - reported in the job, canary torn down
            err = f"{type(e).__name__}: {e}"
            note(f"deploy failed: {err}")
            if canary:  # also tears down a canary left from an earlier deploy
                try:
                    await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 0)
                    gone = await asyncio.to_thread(self.kube.wait_gone, ns, "app=worker,ramen.io/track=canary", 120)
                    note(
                        "scaled canary to 0; main deployment untouched"
                        + ("" if gone else " (canary pod still terminating)")
                    )
                except Exception as e2:  # noqa: BLE001
                    note(f"could not scale canary down: {type(e2).__name__}: {e2}")
            workers.append({"id": "worker-canary" if canary else "worker", "ok": False, "error": err})
            return {"ok": False, "error": err, "workers": workers, "namespace": ns, "log": lines}

    # observe ------------------------------------------------------------
    @_guard
    async def workers(self, group, zone):
        ns = ns_name(group, zone)
        if not await asyncio.to_thread(self.kube.read, "Namespace", None, ns):
            return []
        pods = await asyncio.to_thread(self.kube.pods, ns)
        out = []
        for p in pods:
            name, track = p["metadata"]["name"], p["metadata"].get("labels", {}).get("ramen.io/track", "stable")
            w = {"id": name, "track": track, "ip": p["status"].get("podIP"), "phase": p["status"].get("phase")}
            if w["phase"] != "Running":
                out.append({**w, "load": "down", "metrics": {}})
                continue
            try:
                m = await self.rpc.metrics(self._target(p), self.admin_key)
                out.append({**w, "load": m.get("load", "unknown"), "metrics": m})
            except (GrpcError, ValueError) as e:
                out.append({**w, "load": "down", "metrics": {}, "error": f"{type(e).__name__}: {e}"})
        return out

    @_guard
    async def logs(self, group, zone, worker=None, tail=500):
        return await asyncio.to_thread(gcp_api.fetch_logs, self.c.logging, ns_name(group, zone), worker, tail)

    # traffic ------------------------------------------------------------
    @_guard
    async def rebalance(self, group, zone):
        ns, comp = ns_name(group, zone), gcp_api.Compute(self.c.compute, self.project)
        ws = await self.workers(group, zone)
        loads = [w["load"] for w in ws]
        load = "high" if "high" in loads else ("low" if loads and all(x in ("low", "down") for x in loads) else "even")
        scaler = 0.5 if load == "high" else 1.0
        out = {"ok": True, "load": load, "capacity_scaler": scaler, "backend_service": None, "applied": False}
        try:
            bs = await asyncio.to_thread(comp.find_backend_service, ns, f"ramen-{group}")
        except ApiError as e:
            out["note"] = f"capacity not applied: {e.detail}"
        else:
            try:
                await asyncio.to_thread(comp.set_capacity, bs, ns, scaler, 6)  # ~30s; the Gateway may be reconciling
            except Exception as e:  # noqa: BLE001 - keep applying in the background
                if "not ready" not in str(e):
                    raise
                self._background(comp.set_capacity, bs, ns, scaler, 120)
                out.update(
                    backend_service=bs["name"],
                    note="capacity change pending: backend service busy, retrying in background",
                )
            else:
                out.update(backend_service=bs["name"], applied=True)
        hpa = await asyncio.to_thread(self.kube.read, "HorizontalPodAutoscaler", ns, "worker")
        dep = await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker")
        if hpa and dep and dep["spec"].get("replicas") != hpa["spec"].get("minReplicas"):
            await asyncio.to_thread(self.kube.set_replicas, ns, "worker", hpa["spec"]["minReplicas"])
            out["scaled_to"] = hpa["spec"]["minReplicas"]
        return out

    @_guard
    async def set_ip_rules(self, group, zone, cidrs):
        ns, comp = ns_name(group, zone), gcp_api.Compute(self.c.compute, self.project)
        ref = await asyncio.to_thread(comp.set_armor, f"ramen-{group}", list(cidrs))
        await asyncio.to_thread(
            self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"}
        )
        # env comes from the Secret at pod start: roll the workers so the node enforces the new list now
        for dep in ("worker", "worker-canary"):
            d = await asyncio.to_thread(self.kube.read, "Deployment", ns, dep)
            if d and d.get("spec", {}).get("replicas", 0) > 0:
                await asyncio.to_thread(self.kube.restart, ns, dep)
                await asyncio.to_thread(self.kube.wait_ready, ns, dep)
        out = {"ok": True, "policy": f"ramen-{group}", "cidrs": list(cidrs), "backend_service": None, "attached": False}
        try:
            bs = await asyncio.to_thread(comp.find_backend_service, ns, f"ramen-{group}")
        except ApiError as e:
            out["note"] = f"policy written to Secret and Cloud Armor but not attached: {e.detail}"
            return out
        try:
            await asyncio.to_thread(comp.attach_armor, bs["name"], ref, 6)  # ~30s; the LB reconciles after the restart
        except Exception as e:  # noqa: BLE001 - keep attaching in the background, the Secret already protects the node
            if "not ready" not in str(e):
                raise
            self._background(comp.attach_armor, bs["name"], ref, 120)
            return {
                **out,
                "backend_service": bs["name"],
                "note": "Cloud Armor attach pending: backend service busy, retrying in background",
            }
        return {**out, "backend_service": bs["name"], "attached": True}

    def _background(self, fn, *args):
        task = asyncio.get_running_loop().run_in_executor(None, fn, *args)
        self._bg = getattr(self, "_bg", set()) | {task}
        task.add_done_callback(lambda t: self._bg.discard(t))

    @_guard
    async def abort_deploy(self, group, zone):
        ns = ns_name(group, zone)
        # Namespace reads are cluster-scoped; a Deployment read in a namespace without our RoleBinding is 403
        if not await asyncio.to_thread(self.kube.read, "Namespace", None, ns):
            return
        if await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker-canary"):
            await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 0)
            await asyncio.to_thread(self.kube.wait_gone, ns, "app=worker,ramen.io/track=canary", 120)

    @_guard
    async def detach_group(self, group):
        """Destroy the group's infra: every `ramen-<group>-*` namespace and GSA (F4.1)."""
        prefix = f"ramen-{group}-"
        removed = {"namespaces": [], "service_accounts": []}
        for ns in await asyncio.to_thread(self.kube.list_namespaces, group):
            await asyncio.to_thread(self.kube.delete_namespace, ns)
            removed["namespaces"].append(ns)
        iam = self._iam()
        for email in await asyncio.to_thread(iam.list_service_accounts, prefix):
            await asyncio.to_thread(iam.delete_service_account, email)
            removed["service_accounts"].append(email)
        return removed

    # identity (SEC-08: bucket and secret roles are bound on the resources, never on the project) ---------------
    SECRET_ROLE = "roles/secretmanager.secretAccessor"

    def _bucket_condition(self, group) -> dict:
        """IAM condition restricting a storage role to the group's prefix in the groups bucket."""
        return {
            "title": f"ramen {group} bucket prefix",
            "expression": (
                f'resource.name.startsWith("projects/_/buckets/{self.bucket}/objects/{group}/") '
                f'|| resource.name == "projects/_/buckets/{self.bucket}"'
            ),
        }

    def _iam(self):
        return gcp_api.Iam(self.c.iam, self.c.crm, self.project)

    def bind_new_secret(self, name, group) -> bool:
        """Called by the gcp secrets backend after creating `ramen-<group>-…`: grant the group's worker GSAs access."""
        iam = self._iam()
        members = iam.group_members(group)
        return bool(members) and iam.bind_secret(self.c.secretmanager, name, [self.SECRET_ROLE], members)

    def _grant(self, iam, email, group, roles: list[str]) -> tuple[list[str], list[str]]:
        """Bind roles at the narrowest scope: storage.* on the bucket, secretmanager.* on the group's secrets, the rest
        on the project (optional privilege, see grant_project_roles). Returns (applied, skipped)."""
        applied, project = [], []
        for role in roles:
            if role.startswith("roles/storage."):
                applied += iam.grant_bucket_roles(
                    self.c.storage, self.bucket, email, [(role, self._bucket_condition(group))]
                )
            elif role.startswith("roles/secretmanager."):
                applied += iam.grant_secret_roles(self.c.secretmanager, group, email, [role])
            else:
                project.append(role)
        if not project:
            return applied, []
        try:
            applied += iam.grant_project_roles(email, [(r, None) for r in project])
        except Exception as e:  # noqa: BLE001 - no projectIamAdmin: report, keep the resource-level grants
            if http_status(e) != 403:
                raise
            return applied, project
        return applied, []

    def _identity(self, group, zone) -> dict:
        """GSA ramen-<group>-<zone>@ with the baseline grants (bucket prefix viewer, group secrets accessor) and the
        Workload Identity binding for KSA <ns>/worker. Idempotent."""
        ns, iam = ns_name(group, zone), self._iam()
        email, created = iam.ensure_account(iam.account_id(group, zone), f"ramen worker {group}/{zone}")
        roles, _ = self._grant(iam, email, group, ["roles/storage.objectViewer", self.SECRET_ROLE])
        member = iam.bind_workload_identity(email, ns, "worker")
        return {"name": email, "created": created, "roles": roles, "ksa": f"{ns}/worker", "workload_identity": member}

    @_guard
    async def create_service_account(self, group, zone):
        ns = ns_name(group, zone)

        def run():
            out = self._identity(group, zone)
            email = out["name"]
            self._ensure_namespace(group, zone)
            self.kube.apply(
                {
                    "apiVersion": "v1",
                    "kind": "ServiceAccount",
                    "metadata": {
                        "name": "worker",
                        "namespace": ns,
                        "annotations": {"iam.gke.io/gcp-service-account": email},
                    },
                }
            )
            return out

        return await asyncio.to_thread(run)

    @_guard
    async def apply_sa_permissions(self, group, zone, permissions):
        """Grant the mapped IAM roles to the zone GSA (created if missing).

        bucket.* stays scoped to the group prefix, secrets.* to the group's secrets."""
        ns, iam = ns_name(group, zone), self._iam()
        roles = perm.mapped(permissions, "gcp")

        def run():
            email, _ = iam.ensure_account(iam.account_id(group, zone), f"ramen worker {group}/{zone}")
            applied, skipped = self._grant(iam, email, group, roles)
            out = {
                "ok": True,
                "service_account": email,
                "applied": applied,
                "permissions": list(permissions),
                "ksa": f"{ns}/worker",
            }
            if skipped:
                out["skipped"] = skipped
                out["note"] = (
                    "project-wide roles not bound: the console GSA has no projectIamAdmin "
                    "(terraform var console_project_iam=true enables it)"
                )
            return out

        return await asyncio.to_thread(run)

    @_guard
    async def refresh(self):
        def run():
            zones = []
            for n in self.c.to_dict(self.c.core.list_namespace(label_selector="ramen.io/group")).get("items", []):
                labels, ns = n["metadata"].get("labels", {}), n["metadata"]["name"]
                main = self.kube.read("Deployment", ns, "worker") or {}
                can = self.kube.read("Deployment", ns, "worker-canary") or {}
                ksa = self.kube.read("ServiceAccount", ns, "worker") or {}
                zones.append(
                    {
                        "group": labels.get("ramen.io/group"),
                        "zone": labels.get("ramen.io/zone"),
                        "namespace": ns,
                        "replicas": main.get("spec", {}).get("replicas", 0),
                        "ready": (main.get("status") or {}).get("readyReplicas", 0),
                        "canary_replicas": can.get("spec", {}).get("replicas", 0),
                        "canary_ready": (can.get("status") or {}).get("readyReplicas", 0),
                        "service_account": (ksa.get("metadata", {}).get("annotations") or {}).get(
                            "iam.gke.io/gcp-service-account"
                        ),
                    }
                )
            return {
                "groups": sorted({z["group"] for z in zones if z.get("group")}),
                "zones": zones,
                "service_accounts": self._iam().list_accounts(),
                "at": now(),
            }

        return await asyncio.to_thread(run)
