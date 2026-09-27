"""GCP cloud adapter (CONTRACTS §7): GKE namespaces per zone, GCS group bucket, Cloud Logging, Cloud Armor, IAM."""
import asyncio
import functools
import json
import os
from typing import Any

import httpx

from ..errors import ApiError
from ..util import now
from . import gcp_api
from .base import Cloud
from .gcp_clients import GcpClients
from .gcp_k8s import PORT, Kube, helm_available, helm_manifests, manifests, ns_name


def _guard(fn):
    @functools.wraps(fn)
    async def wrapper(self, *a, **kw):
        try:
            return await fn(self, *a, **kw)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001 - upstream failure surfaced as 502, never with secret values
            raise ApiError(502, f"gcp {fn.__name__}: {type(e).__name__}: {str(e)[:300]}")
    return wrapper


class GcpCloud(Cloud):
    def __init__(self, project, region="us-central1", bucket="", image="", admin_key="", clients=None, transport=None,
                 timeout=10.0, wait_secs=300, poll=2.0, pod_proxy=False, chart=None):
        self.project, self.region, self.bucket, self.image, self.admin_key = project, region, bucket, image, admin_key
        self.c = clients or GcpClients(project)
        self.kube = Kube(self.c, poll=poll, wait_secs=wait_secs)
        self.pod_proxy, self.chart = pod_proxy, chart
        self._client_kw = {"transport": transport, "timeout": timeout}

    @classmethod
    def from_env(cls):
        project = os.environ.get("RAMEN_GCP_PROJECT")
        if not project:
            raise ValueError("RAMEN_CLOUD=gcp needs RAMEN_GCP_PROJECT")
        return cls(project, os.environ.get("RAMEN_GCP_REGION", "us-central1"),
                   os.environ.get("RAMEN_GROUPS_BUCKET", f"ramen-{project}-groups"),
                   os.environ.get("RAMEN_IMAGE_WORKER", "ramen-worker:0.2.0"), os.environ.get("RAMEN_ADMIN_KEY", ""),
                   wait_secs=int(os.environ.get("RAMEN_DEPLOY_TIMEOUT_SECS", "300")),
                   pod_proxy=os.environ.get("RAMEN_GCP_POD_PROXY", "0") == "1", chart=os.environ.get("RAMEN_WORKER_CHART"))

    def bucket_uri(self, group) -> str:
        return f"gs://{self.bucket}/{group}"

    # zone lifecycle -----------------------------------------------------
    def _render(self, group, zone, spec):
        ns = ns_name(group, zone)
        ksa = self.kube.read("ServiceAccount", ns, "worker") or {}
        gsa = (ksa.get("metadata", {}).get("annotations") or {}).get("iam.gke.io/gcp-service-account") or spec.get("service_account")
        if helm_available(self.chart):
            return "helm", helm_manifests(self.chart, self.project, group, zone, spec, self.image, self.bucket_uri(group), gsa)
        return "python", manifests(group, zone, spec, self.image, self.bucket_uri(group), gsa)

    def _attach(self, group, zone, spec) -> dict:
        renderer, docs = self._render(group, zone, spec or {})
        for d in docs:
            self.kube.apply(d, keep=("replicas",) if d["metadata"]["name"] == "worker-canary" else ())
        return {"ok": True, "namespace": ns_name(group, zone), "renderer": renderer, "applied": [d["kind"] for d in docs]}

    @_guard
    async def attach_zone(self, group, zone, spec=None) -> dict:
        return await asyncio.to_thread(self._attach, group, zone, spec)

    @_guard
    async def scale(self, group, zone, spec) -> dict:
        return await asyncio.to_thread(self._attach, group, zone, spec)

    # repo sync ----------------------------------------------------------
    @_guard
    async def sync_repo(self, group, repo_url, ref, token):
        return await asyncio.to_thread(gcp_api.sync_repo_to_gcs, self.c.storage, self.bucket, group, repo_url, ref, token)

    # deploy -------------------------------------------------------------
    def _pod_url(self, pod) -> str:
        return f"http://{pod['status'].get('podIP')}:{PORT}"

    async def _pod_get(self, client, ns, pod, path) -> dict:
        if self.pod_proxy:
            return json.loads(await asyncio.to_thread(self.kube.proxy_get, ns, pod["metadata"]["name"], path))
        return (await client.get(f"{self._pod_url(pod)}/{path}")).json()

    async def _reload_and_smoke(self, client, pod, mcp_key, log) -> dict:
        url, name = self._pod_url(pod), pod["metadata"]["name"]
        r = await client.post(f"{url}/admin/reload", headers={"X-Ramen-Admin-Key": self.admin_key})
        if r.status_code != 200:
            raise RuntimeError(f"reload on {name} returned {r.status_code}: {r.text[:200]}")
        result = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        log(f"{name}: reload ok ({len(result.get('tools', []))} tools, {len(result.get('errors', []))} errors)")
        if mcp_key:
            s = await client.post(f"{url}/mcp", headers={"Authorization": f"Bearer {mcp_key}"},
                                  json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
            ok = s.status_code == 200 and "result" in s.json()
        else:
            s = await client.get(f"{url}/readyz")
            ok = s.status_code == 200
        if not ok:
            raise RuntimeError(f"smoke test on {name} failed: {s.status_code} {s.text[:200]}")
        log(f"{name}: smoke ok ({'tools/list' if mcp_key else 'readyz, no MCP keys configured'})")
        return result

    async def deploy(self, group, env, zone, canary=True, config=None, spec=None, log=None) -> dict[str, Any]:
        lines: list[str] = []

        def note(msg):
            lines.append(f"{now()} {msg}")
            if log:
                log(lines[-1])
        ns, config = ns_name(group, zone), dict(config or {})
        workers, canary_up = [], False  # noqa: F841 - kept for log clarity
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
            async with httpx.AsyncClient(**self._client_kw) as client:
                if canary:
                    canary_up = True
                    await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 1)
                    await asyncio.to_thread(self.kube.restart, ns, "worker-canary")
                    note("canary: restarted worker-canary, waiting for ready")
                    await asyncio.to_thread(self.kube.wait_ready, ns, "worker-canary")
                    pods = await asyncio.to_thread(self.kube.pods, ns, "app=worker,ramen.io/track=canary")
                    if not pods:
                        raise RuntimeError("canary ready but no canary pod found")
                    for p in pods:
                        result = await self._reload_and_smoke(client, p, mcp_key, note)
                        workers.append({"id": p["metadata"]["name"], "ok": True, "track": "canary", "result": result})
                await asyncio.to_thread(self.kube.restart, ns, "worker")
                note("main: restarted worker, waiting for ready")
                d = await asyncio.to_thread(self.kube.wait_ready, ns, "worker")
                # old pods keep serving (with the old key set) until drained; the job is only "ok" once they are gone
                drained = await asyncio.to_thread(self.kube.wait_terminated, ns, "app=worker", 180)
                note("main: old pods drained" if drained else "main: old pods still terminating after 180s")
                for p in await asyncio.to_thread(self.kube.pods, ns, "app=worker,ramen.io/track=stable"):
                    workers.append({"id": p["metadata"]["name"], "ok": True, "track": "stable", "status": 200})
                note(f"main: {d.get('status', {}).get('readyReplicas', 0)} ready")
            return {"ok": True, "workers": workers, "namespace": ns, "log": lines}
        except Exception as e:  # noqa: BLE001 - reported in the job, canary torn down
            err = f"{type(e).__name__}: {e}"
            note(f"deploy failed: {err}")
            if canary:  # also tears down a canary left from an earlier deploy (canary_up tracks this attempt only)
                try:
                    await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 0)
                    gone = await asyncio.to_thread(self.kube.wait_gone, ns, "app=worker,ramen.io/track=canary", 120)
                    note("scaled canary to 0; main deployment untouched" + ("" if gone else " (canary pod still terminating)"))
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
        async with httpx.AsyncClient(**self._client_kw) as client:
            for p in pods:
                name, track = p["metadata"]["name"], p["metadata"].get("labels", {}).get("ramen.io/track", "stable")
                w = {"id": name, "track": track, "ip": p["status"].get("podIP"), "phase": p["status"].get("phase")}
                if w["phase"] != "Running":
                    out.append({**w, "load": "down", "metrics": {}})
                    continue
                try:
                    m = await self._pod_get(client, ns, p, "metrics")
                    out.append({**w, "load": m.get("load", "unknown"), "metrics": m})
                except (httpx.HTTPError, ValueError) as e:
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
        load = "high" if "high" in loads else ("low" if loads and all(l in ("low", "down") for l in loads) else "even")
        scaler = 0.5 if load == "high" else 1.0
        out = {"ok": True, "load": load, "capacity_scaler": scaler, "backend_service": None, "applied": False}
        try:
            bs = await asyncio.to_thread(comp.find_backend_service, ns, f"ramen-{group}")
        except ApiError as e:
            out["note"] = f"capacity not applied: {e.detail}"
        else:
            await asyncio.to_thread(comp.set_capacity, bs, ns, scaler)
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
        await asyncio.to_thread(self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"})
        out = {"ok": True, "policy": f"ramen-{group}", "cidrs": list(cidrs), "backend_service": None, "attached": False}
        try:
            bs = await asyncio.to_thread(comp.find_backend_service, ns, f"ramen-{group}")
        except ApiError as e:
            out["note"] = f"policy written to Secret and Cloud Armor but not attached: {e.detail}"
            return out
        await asyncio.to_thread(comp.attach_armor, bs["name"], ref)
        return {**out, "backend_service": bs["name"], "attached": True}

    @_guard
    async def detach_group(self, group):
        """Destroy the group's infra: every `ramen-<group>-*` namespace and GSA (F4.1)."""
        prefix = f"ramen-{group}-"
        removed = {"namespaces": [], "service_accounts": []}
        for ns in await asyncio.to_thread(self.kube.list_namespaces, group):
            await asyncio.to_thread(self.kube.delete_namespace, ns)
            removed["namespaces"].append(ns)
        iam = gcp_api.Iam(self.c.iam, self.c.crm, self.project)
        for email in await asyncio.to_thread(iam.list_service_accounts, prefix):
            await asyncio.to_thread(iam.delete_service_account, email)
            removed["service_accounts"].append(email)
        return removed

    # identity -----------------------------------------------------------
    @_guard
    async def create_service_account(self, group, zone):
        ns, iam = ns_name(group, zone), gcp_api.Iam(self.c.iam, self.c.crm, self.project)

        def run():
            email, created = iam.ensure_account(iam.account_id(group, zone), f"ramen worker {group}/{zone}")
            number = iam.project_number()
            roles = iam.grant_project_roles(email, [
                ("roles/storage.objectViewer", {"title": f"ramen {group} bucket prefix", "expression":
                    f'resource.name.startsWith("projects/_/buckets/{self.bucket}/objects/{group}/") || '
                    f'resource.name == "projects/_/buckets/{self.bucket}"'}),
                ("roles/secretmanager.secretAccessor", {"title": f"ramen {group} secrets", "expression":
                    f'resource.name.startsWith("projects/{number}/secrets/ramen-{group}-")'})])
            member = iam.bind_workload_identity(email, ns, "worker")
            self.kube.apply({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}}})
            self.kube.apply({"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {
                "name": "worker", "namespace": ns, "annotations": {"iam.gke.io/gcp-service-account": email}}})
            return {"name": email, "created": created, "roles": roles, "ksa": f"{ns}/worker", "workload_identity": member}
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
                zones.append({"group": labels.get("ramen.io/group"), "zone": labels.get("ramen.io/zone"), "namespace": ns,
                              "replicas": main.get("spec", {}).get("replicas", 0), "ready": (main.get("status") or {}).get("readyReplicas", 0),
                              "canary_replicas": can.get("spec", {}).get("replicas", 0), "canary_ready": (can.get("status") or {}).get("readyReplicas", 0),
                              "service_account": (ksa.get("metadata", {}).get("annotations") or {}).get("iam.gke.io/gcp-service-account")})
            return {"groups": sorted({z["group"] for z in zones if z.get("group")}), "zones": zones,
                    "service_accounts": gcp_api.Iam(self.c.iam, self.c.crm, self.project).list_accounts(), "at": now()}
        return await asyncio.to_thread(run)
