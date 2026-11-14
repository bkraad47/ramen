"""GCP cloud adapter (CONTRACTS §7): GKE namespaces per zone, GCS group bucket, Cloud Logging, Cloud Armor, IAM."""

import asyncio
import functools
import logging
import os
import shlex
import time
from typing import Any

from .. import __version__, grpcclient
from ..errors import ApiError
from ..grpcclient import GrpcError
from ..policy import permissions as perm
from ..util import now
from . import gcp_api
from .base import BuildError, Cloud, no_builder
from .gcp_clients import GcpClients, http_status
from .gcp_k8s import (
    ACTIVATOR,
    BUILD_NS,
    BUILDER_IMAGE,
    PORT,
    ROLE,
    SAME_ZONE_PEER,
    WORKERS,
    Kube,
    activator,
    build_objects,
    direct_service,
    helm_available,
    helm_manifests,
    job_state,
    manifests,
    ns_name,
    rolebinding,
    same_zone_rule,
)
from .local import worker_tls_from_env


def _guard(fn):
    @functools.wraps(fn)
    async def wrapper(self, *a, **kw):
        try:
            return await fn(self, *a, **kw)
        except ApiError:
            raise
        except Exception as e:  # noqa: BLE001 - upstream failure surfaced as 502, never with secret values
            raise ApiError(502, f"GCP {fn.__name__}: {type(e).__name__}: {str(e)[:300]}") from e

    return wrapper


def detail_of(e: Exception) -> str:
    """Short, safe description of a cloud failure for a degraded-path note."""
    return e.detail if isinstance(e, ApiError) else f"{type(e).__name__}: {str(e)[:200]}"


class GcpCloud(Cloud):
    PROVIDER = "GCP"

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
        image = spec.get("image") or self.image  # §13.3: the group's pinned image, else the release image
        if helm_available(self.chart):
            return "helm", helm_manifests(
                self.chart, self.project, group, zone, spec, image, self.bucket_uri(group), gsa
            )
        return "python", manifests(group, zone, spec, image, self.bucket_uri(group), gsa)

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

    def _attach(self, group, zone, spec, held: list | None = None) -> dict:
        """`held` (a canary deploy): an existing stable Deployment keeps its pod template — a new image or size reaches
        it only once the canary passed (`deploy` applies the appended doc then)."""
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
            name = d["metadata"]["name"]
            if held is not None and d["kind"] == "Deployment" and name == "worker":
                held.append(d)
                if self.kube.read("Deployment", d["metadata"]["namespace"], name):
                    self.kube.apply(d, keep=("template",))
                    continue
            self.kube.apply(d, keep=("replicas",) if name == "worker-canary" else ())
        self.kube.delete("HorizontalPodAutoscaler", ns_name(group, zone), "worker")  # §21.7: pre-0.7.5 HPA, if any
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
    async def _write_deploy_secret(self, ns, group, env, zone, config) -> dict:
        """The `ramen-deploy` Secret the worker pods read as environment: identity, admin key, the kept IP
        allow-list, then the deploy config."""
        existing = await asyncio.to_thread(self.kube.secret_values, ns, "ramen-deploy")
        data = {"RAMEN_GROUP": group, "RAMEN_ENV": env, "RAMEN_ZONE": zone}
        if self.admin_key:
            data["RAMEN_ADMIN_KEY"] = self.admin_key
        if "RAMEN_ALLOWED_CIDRS" in existing and "RAMEN_ALLOWED_CIDRS" not in config:
            data["RAMEN_ALLOWED_CIDRS"] = existing["RAMEN_ALLOWED_CIDRS"]
        data.update(config)
        await asyncio.to_thread(self.kube.merge_secret, ns, "ramen-deploy", data, True)
        return data

    @_guard
    async def push_config(self, group, env, zone, config) -> dict[str, Any]:
        """§26.3: the Secret rewritten, `Admin/Reload` on every worker pod, then a rolling restart of the stable
        track — the pods read the Secret as environment, so a reload alone cannot see the new values."""
        ns = ns_name(group, zone)
        await self._write_deploy_secret(ns, group, env, zone, dict(config))
        n, failed = 0, []
        for p in await asyncio.to_thread(self.kube.pods, ns, WORKERS):
            try:
                await self.rpc.reload(self._target(p), self.admin_key)
                n += 1
            except GrpcError as e:
                failed.append(f"{p['metadata']['name']}: {e.code.name}")
        if await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker"):
            await asyncio.to_thread(self.kube.restart, ns, "worker")
        return {"ok": not failed, "reloaded": n, "restarted": "worker", "error": "; ".join(failed) or None}

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

    async def _canary_out(self, ns, note) -> None:
        """Before the canary's pods change: GCP's Service selects whatever pods exist, nothing to do. AWS overrides it
        (the ALB keeps a weighted share for the canary target group even when it has no targets)."""

    def _caller(self, target, mcp_key, group, zone):
        async def call(msg):
            return await self.rpc.call(target, mcp_key, group, zone, msg)

        return call

    @_guard
    async def read_file(self, group, path):
        blob = await asyncio.to_thread(self.c.storage.bucket(self.bucket).get_blob, f"{group}/{path}")
        return await asyncio.to_thread(blob.download_as_bytes) if blob else None

    async def deploy(
        self, group, env, zone, canary=True, config=None, spec=None, log=None, gate=None
    ) -> dict[str, Any]:
        lines: list[str] = []

        def note(msg):
            lines.append(f"{now()} {msg}")
            if log:
                log(lines[-1])

        ns, config = ns_name(group, zone), dict(config or {})
        workers = []
        try:
            note(f"{ns}: applying zone manifests")
            held: list[dict] = []
            await asyncio.to_thread(self._attach, group, zone, spec or {}, held if canary else None)
            data = await self._write_deploy_secret(ns, group, env, zone, config)
            note(f"{ns}: secret ramen-deploy written ({len(data)} keys)")
            mcp_key = (config.get("RAMEN_MCP_KEYS") or "").split(",")[0].strip() or None
            if not canary:
                # A canary left running by an earlier deploy still holds the OLD deploy config, and the
                # zone Service selects both tracks — so a blocked tool, a revoked key or a tightened
                # allowlist would still be served by it. Remove it before the stable roll.
                if await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker-canary"):
                    await self._canary_out(ns, note)
                    await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 0)
                    gone = await asyncio.to_thread(self.kube.wait_gone, ns, "app=worker,ramen.io/track=canary", 120)
                    note("canary: scaled to 0" + ("" if gone else " (a canary pod is still terminating)"))
            if canary:
                await self._canary_out(ns, note)  # it rolls in place (maxSurge 0): no pod serves for a moment
                await asyncio.to_thread(self.kube.set_replicas, ns, "worker-canary", 1)
                await asyncio.to_thread(self.kube.restart, ns, "worker-canary")
                note("canary: restarted worker-canary, waiting for ready")
                await asyncio.to_thread(self.kube.wait_ready, ns, "worker-canary")
                pods = await asyncio.to_thread(self.kube.pods, ns, "app=worker,ramen.io/track=canary")
                if not pods:
                    raise RuntimeError("canary ready but no canary pod found")
                for p in pods:
                    result = await self._reload_and_smoke(p, mcp_key, group, zone, note)
                    if gate:  # 0.7.0: schema gate + golden cases decide whether stable may roll (GateError aborts)
                        await gate(result, self._caller(self._target(p), mcp_key, group, zone) if mcp_key else None)
                    workers.append({"id": p["metadata"]["name"], "ok": True, "track": "canary", "result": result})
            for d in held:  # the canary passed: now the stable track may take the new template
                ann = d["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})
                ann["ramen.io/restartedAt"] = now()
                await asyncio.to_thread(self.kube.apply, d)
            if not held:
                await asyncio.to_thread(self.kube.restart, ns, "worker")
            note("main: restarted worker, waiting for ready")
            d = await asyncio.to_thread(self.kube.wait_ready, ns, "worker")
            # old pods keep serving (with the old key set) until drained; the job is only "ok" once they are gone
            drained = await asyncio.to_thread(self.kube.wait_terminated, ns, "app=worker", 180)
            note("main: old pods drained" if drained else "main: old pods still terminating after 180s")
            for p in await asyncio.to_thread(self.kube.pods, ns, f"{WORKERS},ramen.io/track=stable"):
                w = {"id": p["metadata"]["name"], "ok": True, "track": "stable", "status": "OK"}
                # Readiness+drain above already proves the rollout; a reload/smoke failure here must not flip
                # it to failed. Still attempted so the group page's per-zone package list (F5.6) has data for
                # a stable-only deploy (canary=False), not just a canary one.
                try:
                    w["result"] = await self._reload_and_smoke(p, mcp_key, group, zone, note)
                except Exception as e:  # noqa: BLE001 - logged, not fatal; see comment above
                    note(f"{w['id']}: package list not refreshed: {type(e).__name__}: {e}")
                workers.append(w)
            note(f"main: {d.get('status', {}).get('readyReplicas', 0)} ready")
            return {"ok": True, "workers": workers, "namespace": ns, "log": lines}
        except Exception as e:  # noqa: BLE001 - reported in the job, canary torn down
            err = f"{type(e).__name__}: {e}"
            note(f"deploy failed: {err}")
            if canary:  # also tears down a canary left from an earlier deploy
                try:
                    await self._canary_out(ns, note)
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

    # sleeping workers (§24.1) -------------------------------------------
    def _replicas(self, ns, name) -> tuple[int, int]:
        """(wanted, ready) replicas of a Deployment; (0, 0) when it does not exist."""
        d = self.kube.read("Deployment", ns, name)
        if d is None:
            return 0, 0
        return int(d["spec"].get("replicas", 1)), int((d.get("status") or {}).get("readyReplicas") or 0)

    def _runtime(self, ns) -> dict:
        if not self.kube.read("Namespace", None, ns):
            return {"workers": 0, "canary": 0, "activator": 0}
        return {
            "workers": self._replicas(ns, "worker")[1],
            "canary": self._replicas(ns, "worker-canary")[1],
            "activator": self._replicas(ns, ACTIVATOR)[1],
        }

    @_guard
    async def zone_runtime(self, group, zone) -> dict:
        return await asyncio.to_thread(self._runtime, ns_name(group, zone))

    def _scale_workers(self, ns, name, replicas: int) -> None:
        """Replicas plus the role label on the pod template in one patch: a zone attached before 1.0 gets the label
        that `worker-direct` selects without an extra rollout (the selector is untouched)."""
        self.kube.patch(
            "Deployment",
            ns,
            name,
            {"spec": {"replicas": int(replicas), "template": {"metadata": {"labels": {ROLE: "worker"}}}}},
        )

    def _ensure_direct(self, group, zone) -> None:
        """What a zone attached before 1.0 lacks for sleeping: `worker-direct` and the NetworkPolicy peer that lets the
        activator reach the workers. Idempotent."""
        ns = ns_name(group, zone)
        self.kube.apply(direct_service(group, zone))
        np = self.kube.read("NetworkPolicy", ns, "worker")
        rules = ((np or {}).get("spec") or {}).get("ingress") or []
        if rules and not any(SAME_ZONE_PEER in (r.get("from") or []) for r in rules):
            self.kube.patch("NetworkPolicy", ns, "worker", {"spec": {"ingress": [*rules, same_zone_rule()]}})

    def _sleep_prepare(self, group, zone) -> dict:
        ns = ns_name(group, zone)
        worker = self.kube.read("Namespace", None, ns) and self.kube.read("Deployment", ns, "worker")
        if not worker:
            raise ApiError(409, f"Zone {zone} of group {group} is not deployed: nothing to put to sleep")
        self._ensure_direct(group, zone)
        selector = ((self.kube.read("Service", ns, "worker") or {}).get("spec") or {}).get("selector") or {}
        self.kube.apply(activator(worker, selector))  # replicas 1; an activator left at 0 by a wake is patched back
        self.kube.wait_ready(ns, ACTIVATOR)
        # P1 (1.0.0a): Ready is not enough — until the activator is listed behind Service/worker, kube-proxy routes
        # to the terminating-but-serving workers, and a draining worker never asks for the wake.
        try:
            listed = self.kube.wait_endpoint(ns, "worker", f"app=worker,{ROLE}=activator")
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 403:
                raise
            logging.getLogger("ramen.cloud").warning("%s: cannot read Endpoints (zone role predates 1.0.0a)", ns)
            listed = True
        if not listed:
            raise ApiError(502, f"Deployment {ACTIVATOR} is Ready but not in the Endpoints of Service/worker in {ns}")
        return {"workers": self._replicas(ns, "worker")[0], "canary": self._replicas(ns, "worker-canary")[0]}

    def _sleep_finish(self, ns) -> None:
        for name in ("worker", "worker-canary"):
            if self.kube.read("Deployment", ns, name):
                self._scale_workers(ns, name, 0)

    @_guard
    async def sleep_zone(self, group, zone) -> dict:
        """§24.1 sleep: the activator first (Ready behind `Service/worker`), then the workers to 0 — their SIGTERM
        drain serves what is in flight. Idempotent. `previous` is what the Deployments asked for before this call."""
        ns = ns_name(group, zone)
        previous = await asyncio.to_thread(self._sleep_prepare, group, zone)
        await self._canary_out(ns, lambda _msg: None)  # AWS: an empty canary target group must hold no weight
        await asyncio.to_thread(self._sleep_finish, ns)
        return {"ok": True, "namespace": ns, "previous": previous, **await asyncio.to_thread(self._runtime, ns)}

    def _after_wake(self, ns, count: int, canary: int) -> None:
        """Edge bookkeeping once the workers are Ready. GCP/Azure: none. AWS restores the ALB split."""

    def _wake(self, group, zone, count: int, canary: int) -> dict:
        ns = ns_name(group, zone)
        if not (self.kube.read("Namespace", None, ns) and self.kube.read("Deployment", ns, "worker")):
            raise ApiError(409, f"Zone {zone} of group {group} is not deployed: nothing to wake")
        count, canary = max(1, int(count)), max(0, int(canary))
        self._ensure_direct(group, zone)
        self._scale_workers(ns, "worker", count)
        has_canary = self.kube.read("Deployment", ns, "worker-canary") is not None
        if has_canary:
            self._scale_workers(ns, "worker-canary", canary)
        self.kube.wait_ready(ns, "worker")  # a timeout names the stuck pods; the activator keeps answering 503
        if has_canary:
            self.kube.wait_ready(ns, "worker-canary")
        self._after_wake(ns, count, canary if has_canary else 0)
        if self.kube.read("Deployment", ns, ACTIVATOR):
            self.kube.set_replicas(ns, ACTIVATOR, 0)  # last: while both exist the activator forwards (§24.1)
        return {"ok": True, "namespace": ns, **self._runtime(ns)}

    @_guard
    async def wake_zone(self, group, zone, count, canary) -> dict:
        """§24.1 wake: `worker` to `count` (≥ 1) and `worker-canary` to `canary`, wait Ready, then the activator to 0.
        Idempotent; a zone that never slept is simply scaled."""
        return await asyncio.to_thread(self._wake, group, zone, count, canary)

    # images built on deploy (§24.2) ---------------------------------------
    def builder_available(self) -> bool:
        return os.environ.get("RAMEN_BUILDER") == "1" and bool(os.environ.get("RAMEN_BUILD_REGISTRY", "").strip())

    def _build_extras(self, registry: str) -> dict:
        """What the provider's registry needs on the build pod beyond `ramen-builder`'s workload identity (GKE:
        nothing, kaniko's Google keychain reads the metadata server)."""
        return {}

    def _build_log(self, name, tail=40) -> str:
        pods = self.kube.pods(BUILD_NS, f"job-name={name}")
        if not pods:
            return ""
        pod = max(pods, key=lambda p: p["metadata"].get("creationTimestamp") or "")
        out = []
        for container in ("context", "build"):
            try:
                out += self.kube.pod_log(BUILD_NS, pod["metadata"]["name"], container, tail).splitlines()
            except Exception:  # noqa: BLE001, S112, PERF203 - a container that never started has no log
                continue
        if not out:  # nothing ran: say why (image pull, scheduling) instead of nothing
            out = [f"build pod {pod['metadata']['name']}{self.kube._stuck_pods_of([pod])}"]
        return "\n".join(out[-tail:])

    def _build(self, group, tag, dockerfile, files) -> dict:
        if not self.builder_available():
            raise no_builder()
        registry = os.environ["RAMEN_BUILD_REGISTRY"].strip().rstrip("/")
        image = f"{registry}/worker:{tag}"
        timeout = float(os.environ.get("RAMEN_BUILD_TIMEOUT_SECS", "900"))
        extras = self._build_extras(registry)
        cm, job = build_objects(
            group,
            tag,
            image,
            dockerfile,
            dict(files or {}),
            builder_image=os.environ.get("RAMEN_BUILDER_IMAGE") or BUILDER_IMAGE,
            init_image=os.environ.get("RAMEN_BUILD_INIT_IMAGE") or None,
            args=(*extras.get("args", ()), *shlex.split(os.environ.get("RAMEN_BUILD_ARGS", ""))),
            env=extras.get("env"),
            pod_labels=extras.get("pod_labels"),
            docker_config=extras.get("docker_config"),
            cpu=os.environ.get("RAMEN_BUILD_CPU", "250m"),
            memory=os.environ.get("RAMEN_BUILD_MEMORY", "768Mi"),
            memory_limit=os.environ.get("RAMEN_BUILD_MEMORY_LIMIT", "2Gi"),
        )
        name = job["metadata"]["name"]
        live = self.kube.read("Job", BUILD_NS, name)
        if live is not None and job_state(live) == "failed":  # a failed earlier run of this tag: start over
            self.kube.delete("Job", BUILD_NS, name)
            live = None
        try:
            if live is None:  # else: the same build is already running (or just finished) — wait for that one
                self.kube.apply(cm)
                try:
                    self.c.batch.create_namespaced_job(BUILD_NS, job)
                except Exception as e:  # noqa: BLE001 - created by a concurrent caller between read and create
                    if http_status(e) != 409:
                        raise
            deadline = time.monotonic() + timeout
            while True:
                current = self.kube.read("Job", BUILD_NS, name)
                # gone: another caller waiting on the same build cleaned it up, its outcome is not ours to guess
                state = job_state(current) if current is not None else "gone"
                if state != "running":
                    break
                if time.monotonic() >= deadline:
                    state = "timeout"
                    break
                time.sleep(self.kube.poll)
            log = self._build_log(name)
            self.kube.delete("Job", BUILD_NS, name)  # a timed-out build stops here; TTL 600 s is only the safety net
        finally:
            self.kube.delete("ConfigMap", BUILD_NS, name)
        if state == "succeeded":
            return {"image": image, "log": log}
        why = {"timeout": f"timed out after {int(timeout)}s", "gone": "was removed before it finished"}.get(
            state, "failed"
        )
        raise BuildError(f"image build {name} for {image} {why}" + (f":\n{log}" if log else ""), log)

    async def build_image(self, group, tag, dockerfile, files) -> dict:
        """§24.2: `Job/ramen-build-<hash>` in ramen-system (kaniko as `ramen-builder`), context from a ConfigMap.
        Raises `BuildError` (its text ends with the last 40 log lines) on a failed or timed-out build."""
        try:
            return await asyncio.to_thread(self._build, group, tag, dockerfile, files)
        except (ApiError, BuildError):
            raise
        except Exception as e:  # noqa: BLE001 - upstream failure surfaced as 502, never with secret values
            raise ApiError(502, f"{self.PROVIDER} build_image: {type(e).__name__}: {str(e)[:300]}") from e

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
        ns = ns_name(group, zone)
        ws = await self.workers(group, zone)
        loads = [w["load"] for w in ws]
        load = "high" if "high" in loads else ("low" if loads and all(x in ("low", "down") for x in loads) else "even")
        scaler = 0.5 if load == "high" else 1.0
        out = {"ok": True, "load": load, "capacity_scaler": scaler, "backend_service": None, "applied": False}
        try:
            # Built here, not above: `self.c.compute` is a lazy discovery client, so absent credentials
            # raise on first use and must land inside the degrading path, not outside it.
            comp = gcp_api.Compute(self.c.compute, self.project)
            bs = await asyncio.to_thread(comp.find_backend_service, ns, f"ramen-{group}")
        except Exception as e:  # noqa: BLE001 - §7: the load-balancer leg degrades, it never fails the call.
            # Not just ApiError: absent credentials, 401/403 and transient 5xx all reach here, and the HPA
            # re-scale below needs no cloud API, so losing it to a compute-API problem would be wrong.
            out["note"] = f"capacity not applied: {detail_of(e)}"
        else:
            try:
                await asyncio.to_thread(comp.set_capacity, bs, ns, scaler, 6)  # ~30s; the Gateway may be reconciling
            except Exception as e:  # noqa: BLE001 - retry in the background, or degrade
                if "not ready" not in str(e):
                    out.update(backend_service=bs["name"], note=f"capacity not applied: {detail_of(e)}")
                    bs = None
                else:
                    self._background(comp.set_capacity, bs, ns, scaler, 120)
                    out.update(
                        backend_service=bs["name"],
                        note="capacity change pending: backend service busy, retrying in background",
                    )
            else:
                out.update(backend_service=bs["name"], applied=True)
        return out

    @_guard
    async def set_ip_rules(self, group, zone, cidrs):
        ns = ns_name(group, zone)
        # Node enforcement first: it is the control that actually gates a call and it needs no cloud API.
        # Cloud Armor at the edge is defence in depth and is attempted afterwards, best effort.
        await asyncio.to_thread(
            self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"}
        )
        # env comes from the Secret at pod start: roll the workers so the node enforces the new list now
        for dep in ("worker", "worker-canary", ACTIVATOR):  # a sleeping zone's activator enforces the list too
            d = await asyncio.to_thread(self.kube.read, "Deployment", ns, dep)
            if d and d.get("spec", {}).get("replicas", 0) > 0:
                await asyncio.to_thread(self.kube.restart, ns, dep)
                await asyncio.to_thread(self.kube.wait_ready, ns, dep)
        out = {"ok": True, "policy": f"ramen-{group}", "cidrs": list(cidrs), "backend_service": None, "attached": False}
        try:
            # Built here, not above: `self.c.compute` is a lazy discovery client, so absent credentials
            # raise on first use and must land inside the degrading path, not outside it.
            comp = gcp_api.Compute(self.c.compute, self.project)
            ref = await asyncio.to_thread(comp.set_armor, f"ramen-{group}", list(cidrs))
        except Exception as e:  # noqa: BLE001 - §7: the node already enforces; the edge policy is best effort.
            out["note"] = f"enforced at the node; Cloud Armor not updated: {detail_of(e)}"
            return out
        try:
            bs = await asyncio.to_thread(comp.find_backend_service, ns, f"ramen-{group}")
        except Exception as e:  # noqa: BLE001 - the policy exists, only the attachment is missing.
            out["note"] = f"enforced at the node and Cloud Armor updated, but not attached: {detail_of(e)}"
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
    async def detach_zone(self, group, zone):
        """Destroy one group's deployment in one zone: its namespace (workers, services, routes go with it) and the
        zone GSA (0.6.0). Nothing else of the group is touched."""
        ns, iam = ns_name(group, zone), self._iam()
        await asyncio.to_thread(self.kube.delete_namespace, ns)
        email = f"{iam.account_id(group, zone)}@{self.project}.iam.gserviceaccount.com"
        removed = {"namespaces": [ns], "service_accounts": []}
        if email in await asyncio.to_thread(iam.list_service_accounts, f"ramen-{group}-{zone}@"):
            # unbind before deleting: a deleted identity left in the bucket policy blocks later policy writes
            await asyncio.to_thread(iam.purge_bucket_member, self.c.storage, self.bucket, email)
            await asyncio.to_thread(iam.delete_service_account, email)
            removed["service_accounts"].append(email)
        return removed

    async def detach_group(self, group):
        """Destroy the group's infra: every `ramen-<group>-*` namespace and GSA, its Cloud Armor policy (F4.1)."""
        prefix = f"ramen-{group}-"
        removed = {"namespaces": [], "service_accounts": [], "security_policies": []}
        for ns in await asyncio.to_thread(self.kube.list_namespaces, group):
            await asyncio.to_thread(self.kube.delete_namespace, ns)
            removed["namespaces"].append(ns)
        iam = self._iam()
        for email in await asyncio.to_thread(iam.list_service_accounts, prefix):
            await asyncio.to_thread(iam.purge_bucket_member, self.c.storage, self.bucket, email)
            await asyncio.to_thread(iam.delete_service_account, email)
            removed["service_accounts"].append(email)
        comp = gcp_api.Compute(self.c.compute, self.project)
        if await asyncio.to_thread(comp.delete_armor, f"ramen-{group}"):
            removed["security_policies"].append(f"ramen-{group}")
        return removed

    def _revoke_stale(self, iam, email, group, keep: list[str]) -> tuple[list[str], list[str]]:
        """§13.2: `apply_sa_permissions` is a set operation. Every catalogue role the remaining permissions no longer
        need is unbound at the scope it was bound on. Returns (revoked, retained-because-baseline)."""
        needed = set(keep)
        stale = [r for r in perm.all_roles("gcp") if r not in needed]
        retained = [r for r in stale if r in self.BASELINE_ROLES]
        stale = [r for r in stale if r not in self.BASELINE_ROLES]
        bucket = [r for r in stale if r.startswith("roles/storage.")]
        secret = [r for r in stale if r.startswith("roles/secretmanager.")]
        project = [r for r in stale if r not in bucket and r not in secret]
        revoked: list[str] = []
        if bucket:
            revoked += iam.revoke_bucket_roles(self.c.storage, self.bucket, email, bucket)
        if secret:
            revoked += iam.revoke_secret_roles(self.c.secretmanager, group, email, secret)
        if project:
            try:
                revoked += iam.revoke_project_roles(email, project)
            except Exception as e:  # noqa: BLE001 - no projectIamAdmin: the resource-level unbinds still happened
                if http_status(e) != 403:
                    raise
        return revoked, retained

    # identity (SEC-08: bucket and secret roles are bound on the resources, never on the project) ---------------
    SECRET_ROLE = "roles/secretmanager.secretAccessor"
    # §13.2: what the zone identity holds because it is a Ramen worker, not because a permission was granted. These
    # are never unbound by a revoke — `bucket.read` and `secrets.read` map onto them, so revoking either takes the
    # grant off the record while the identity keeps that baseline read access. `retained` says so in the response.
    BASELINE_ROLES = ("roles/storage.objectViewer", SECRET_ROLE)

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

    def _named_secrets(self, iam, group, names: list[str]) -> list[str]:
        """The group's secrets whose Ramen name is one of `names` (ids are ramen-<group>-<env>-<zone>-<name>)."""
        return [
            full
            for full in iam.group_secrets(self.c.secretmanager, group)
            if any(full.rsplit("/", 1)[1].endswith(f"-{n}") for n in names)
        ]

    def _unbind_narrowed(self, iam, email, group, before: dict, wanted: dict) -> None:
        """0.5.93: resources a role was bound on last time and is not wanted on now — named buckets or secrets that
        left the scope, or the group-wide binding once a scope names resources. Baseline roles keep their group-wide
        binding (§13.2); roles no longer needed at all are left to `_revoke_stale`."""
        member = f"serviceAccount:{email}"
        for role, names in before.items():
            now = wanted.get(role)
            if names == ["*"]:
                if now is None or now == ["*"] or role in self.BASELINE_ROLES:
                    continue
                if role.startswith("roles/storage."):
                    iam.revoke_bucket_roles(self.c.storage, self.bucket, email, [role])
                elif role.startswith("roles/secretmanager."):
                    iam.revoke_secret_roles(self.c.secretmanager, group, email, [role])
                continue
            gone = [n for n in names if now is None or now == ["*"] or n not in now]
            if role.startswith("roles/storage."):
                for bucket in gone:
                    if bucket == self.bucket and role in self.BASELINE_ROLES:
                        continue  # the groups bucket's baseline binding is the worker's own; never shed here
                    iam.revoke_bucket_roles(self.c.storage, bucket, email, [role])
            elif role.startswith("roles/secretmanager.") and gone:
                for full in self._named_secrets(iam, group, gone):
                    iam.unbind_secret(self.c.secretmanager, full, [role], [member])

    def _grant(self, iam, email, group, roles: list[str], scopes: dict | None = None) -> tuple[list[str], list[str]]:
        """Bind roles at the narrowest scope: storage.* on the groups bucket (group prefix) or on the named buckets,
        secretmanager.* on the group's secrets or the named ones, the rest on the project (optional privilege, see
        grant_project_roles). Returns (applied, skipped)."""
        applied, project = [], []
        for role in roles:
            names = (scopes or {}).get(role) or ["*"]
            if role.startswith("roles/storage."):
                if names == ["*"]:
                    applied += iam.grant_bucket_roles(
                        self.c.storage, self.bucket, email, [(role, self._bucket_condition(group))]
                    )
                else:
                    for bucket in names:  # naming the groups bucket itself still means the group's prefix of it
                        cond = self._bucket_condition(group) if bucket == self.bucket else None
                        try:
                            iam.grant_bucket_roles(self.c.storage, bucket, email, [(role, cond)])
                        except Exception as e:  # noqa: BLE001 - say what to grant, not just "Forbidden"
                            if http_status(e) not in (403, 404):
                                raise
                            raise RuntimeError(
                                f"bucket {bucket!r}: {'not found' if http_status(e) == 404 else 'no IAM access'} — "
                                "the console's service account manages IAM only on the groups bucket (SEC-08); "
                                "to scope a permission to another bucket grant it roles/storage.admin there: "
                                f"gcloud storage buckets add-iam-policy-binding gs://{bucket} "
                                "--member=serviceAccount:<console gsa> --role=roles/storage.admin"
                            ) from e
                    applied.append(role)
            elif role.startswith("roles/secretmanager."):
                if names == ["*"]:
                    applied += iam.grant_secret_roles(self.c.secretmanager, group, email, [role])
                else:
                    for full in self._named_secrets(iam, group, names):
                        iam.bind_secret(self.c.secretmanager, full, [role], [f"serviceAccount:{email}"])
                    applied.append(role)
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
    async def apply_sa_permissions(self, group, zone, permissions, scopes=None, previous=None):
        """Grant the mapped IAM roles to the zone GSA (created if missing).

        Unscoped (`*`), bucket.* stays on the group's prefix of the groups bucket and secrets.* on the group's
        secrets; a scope names the buckets / secrets instead (0.5.93). Project-wide roles cannot be scoped."""
        ns, iam = ns_name(group, zone), self._iam()
        roles = perm.mapped(permissions, "gcp")
        wanted = perm.role_scopes(permissions, scopes, "gcp")
        before = perm.role_scopes(list(previous or {}), previous, "gcp")

        def run():
            email, _ = iam.ensure_account(iam.account_id(group, zone), f"ramen worker {group}/{zone}")
            self._unbind_narrowed(iam, email, group, before, wanted)
            applied, skipped = self._grant(iam, email, group, roles, wanted)
            revoked, retained = self._revoke_stale(iam, email, group, roles)
            out = {
                "ok": True,
                "service_account": email,
                "applied": applied,
                "permissions": list(permissions),
                "scopes": dict(scopes or {}),
                "ksa": f"{ns}/worker",
            }
            if unscoped := perm.unscoped(permissions, scopes):
                out["unscoped"] = unscoped
            if revoked:
                out["revoked"] = revoked
            if retained:
                out["retained"] = retained
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
            skipped = []
            for n in self.c.to_dict(self.c.core.list_namespace(label_selector="ramen.io/group")).get("items", []):
                labels, ns = n["metadata"].get("labels", {}), n["metadata"]["name"]
                # A namespace deleted with its group sits in Terminating for minutes, and its RoleBinding
                # is collected first, so reads inside it answer 403. Reconcile is a survey of what exists:
                # one unreadable namespace must not fail the whole call (D4, found on GKE).
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
                        "service_account": (ksa.get("metadata", {}).get("annotations") or {}).get(
                            "iam.gke.io/gcp-service-account"
                        ),
                    }
                )
            return {
                "groups": sorted({z["group"] for z in zones if z.get("group")}),
                "zones": zones,
                "skipped": skipped,
                "service_accounts": self._iam().list_accounts(),
                "at": now(),
            }

        return await asyncio.to_thread(run)
