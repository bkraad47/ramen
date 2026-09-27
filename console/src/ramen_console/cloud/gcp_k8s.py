"""Worker namespace manifests (CONTRACTS §7) and a thin apply/wait layer over the kubernetes client."""
import shutil
import subprocess
import time

import yaml

from ..errors import ApiError
from ..util import now
from .gcp_clients import http_status

SIZES = {"s": {"cpu": "250m", "memory": "512Mi"}, "m": {"cpu": "500m", "memory": "1Gi"}, "l": {"cpu": "1", "memory": "2Gi"}}
ALIASES = {"small": "s", "medium": "m", "large": "l"}
PORT = 8080
CANARY_KEEP = ("replicas",)  # never reset canary replicas on re-apply
GW_GROUP, GW_VERSION, GW_PLURAL = "gateway.networking.k8s.io", "v1", "httproutes"


def normalize_size(size) -> str | None:
    s = ALIASES.get(str(size), str(size))
    return s if s in SIZES else None


def ns_name(group, zone) -> str:
    return f"ramen-{group}-{zone}"


def _deployment(ns, name, track, group, zone, spec, image, bucket_uri):
    labels = {"app": "worker", "ramen.io/track": track, "ramen.io/group": group, "ramen.io/zone": zone}
    res = SIZES[normalize_size(spec.get("size")) or "s"]
    pod = {
        "serviceAccountName": "worker",
        "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "fsGroup": 10001},
        "containers": [{
            "name": "worker", "image": image, "ports": [{"name": "http", "containerPort": PORT}],
            "env": [{"name": k, "value": v} for k, v in {
                "RAMEN_BUCKET_URI": bucket_uri, "RAMEN_BUCKET": "/data/bucket", "RAMEN_GROUP": group,
                "RAMEN_ZONE": zone, "RAMEN_TRACK": track, "RAMEN_NODE_PORT": str(PORT), "RAMEN_TRUST_PROXY": "1"}.items()],
            "envFrom": [{"secretRef": {"name": "ramen-deploy", "optional": True}}],
            "resources": {"requests": res, "limits": res},
            "readinessProbe": {"httpGet": {"path": "/readyz", "port": "http"}, "periodSeconds": 5},
            "livenessProbe": {"httpGet": {"path": "/healthz", "port": "http"}, "periodSeconds": 10},
            "volumeMounts": [{"name": "bucket", "mountPath": "/data/bucket"}]}],
        "volumes": [{"name": "bucket", "emptyDir": {}}]}
    if spec.get("region"):
        pod["nodeSelector"] = {"topology.kubernetes.io/zone": spec["region"]}
    return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns, "labels": labels},
            "spec": {"replicas": int(spec.get("count", 1)) if track == "stable" else 0,
                     "selector": {"matchLabels": {"app": "worker", "ramen.io/track": track}},
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                     "template": {"metadata": {"labels": labels}, "spec": pod}}}


def httproute(group, zone) -> dict:
    """HTTPRoute `worker`: Gateway ramen-system/ramen routes /mcp/<group>/<zone> → Service worker:8080 as /mcp."""
    ns = ns_name(group, zone)
    return {"apiVersion": f"{GW_GROUP}/{GW_VERSION}", "kind": "HTTPRoute",
            "metadata": {"name": "worker", "namespace": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}},
            "spec": {"parentRefs": [{"group": GW_GROUP, "kind": "Gateway", "name": "ramen", "namespace": "ramen-system"}],
                     "rules": [{"matches": [{"path": {"type": "PathPrefix", "value": f"/mcp/{group}/{zone}"}}],
                                "filters": [{"type": "URLRewrite", "urlRewrite": {"path": {"type": "ReplacePrefixMatch", "replacePrefixMatch": "/mcp"}}}],
                                "backendRefs": [{"name": "worker", "port": PORT}]}]}}


def manifests(group, zone, spec, image, bucket_uri, gsa=None) -> list[dict]:
    ns = ns_name(group, zone)
    labels = {"ramen.io/group": group, "ramen.io/zone": zone}
    count = int(spec.get("count", 1))
    ksa = {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "worker", "namespace": ns, "labels": labels}}
    if gsa:
        ksa["metadata"]["annotations"] = {"iam.gke.io/gcp-service-account": gsa}
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": {**labels, "ramen.io/routes": "true"}}},
        ksa,
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "worker", "namespace": ns, "labels": labels, "annotations": {
            "cloud.google.com/neg": f'{{"exposed_ports": {{"{PORT}": {{"name": "{ns}"}}}}}}'}},
         "spec": {"type": "ClusterIP", "selector": {"app": "worker"}, "ports": [{"name": "http", "port": PORT, "targetPort": "http"}]}},
        _deployment(ns, "worker", "stable", group, zone, spec, image, bucket_uri),
        _deployment(ns, "worker-canary", "canary", group, zone, spec, image, bucket_uri),
        {"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler",
         "metadata": {"name": "worker", "namespace": ns, "labels": labels},
         "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "worker"},
                  "minReplicas": count, "maxReplicas": max(int(spec.get("max_count") or 0), count * 2),
                  "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}}}]}},
        httproute(group, zone),
    ]


def helm_available(chart) -> bool:
    return bool(chart) and shutil.which("helm") is not None


def helm_manifests(chart, project, group, zone, spec, image, bucket_uri, gsa=None) -> list[dict]:
    """Render deploy/helm/ramen-worker when RAMEN_WORKER_CHART points at it; values mirror manifests()."""
    ns = ns_name(group, zone)
    count = int(spec.get("count", 1))
    values = {"project": project, "group": group, "zone": zone, "gcpZone": spec.get("region", ""), "image": image,
              "bucketUri": bucket_uri, "size": normalize_size(spec.get("size")) or "s", "replicas": count,
              "serviceAccount.gsa": gsa or "", "secret.create": "false", "hpa.enabled": "true", "hpa.minReplicas": count,
              "hpa.maxReplicas": max(int(spec.get("max_count") or 0), count * 2)}
    cmd = ["helm", "template", "ramen-worker", str(chart), "--namespace", ns]
    for k, v in values.items():
        cmd += ["--set", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ApiError(502, f"helm template failed: {r.stderr.strip()[:500]}")
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    for d in docs:
        if d["kind"] != "Namespace":
            d["metadata"].setdefault("namespace", ns)
        else:
            d["metadata"].setdefault("labels", {})["ramen.io/routes"] = "true"
    if not any(d["kind"] == "HTTPRoute" for d in docs):
        docs.append(httproute(group, zone))
    return docs


class Kube:
    def __init__(self, clients, poll=2.0, wait_secs=300):
        self.c, self.poll, self.wait_secs = clients, poll, wait_secs

    def _fns(self, kind):
        c = self.c
        table = {
            "Namespace": (c.core.create_namespace, c.core.patch_namespace, c.core.read_namespace, False),
            "ServiceAccount": (c.core.create_namespaced_service_account, c.core.patch_namespaced_service_account, c.core.read_namespaced_service_account, True),
            "Secret": (c.core.create_namespaced_secret, c.core.patch_namespaced_secret, c.core.read_namespaced_secret, True),
            "Service": (c.core.create_namespaced_service, c.core.patch_namespaced_service, None, True),
            "Deployment": (c.apps.create_namespaced_deployment, c.apps.patch_namespaced_deployment, c.apps.read_namespaced_deployment, True),
            "HorizontalPodAutoscaler": (c.autoscaling.create_namespaced_horizontal_pod_autoscaler, c.autoscaling.patch_namespaced_horizontal_pod_autoscaler,
                                        c.autoscaling.read_namespaced_horizontal_pod_autoscaler, True),
            "HTTPRoute": (lambda ns, body: c.custom.create_namespaced_custom_object(GW_GROUP, GW_VERSION, ns, GW_PLURAL, body),
                          lambda name, ns, body: c.custom.patch_namespaced_custom_object(GW_GROUP, GW_VERSION, ns, GW_PLURAL, name, body),
                          lambda name, ns: c.custom.get_namespaced_custom_object(GW_GROUP, GW_VERSION, ns, GW_PLURAL, name), True),
        }
        if kind not in table:
            raise ApiError(502, f"unsupported manifest kind {kind}")
        return table[kind]

    def wait_terminated(self, ns: str, selector: str, timeout: float | None = None) -> bool:
        """True once no pod matching `selector` is still terminating (old ReplicaSet drained), False on timeout."""
        deadline = time.monotonic() + (timeout or self.wait_secs)
        while True:
            items = self.c.to_dict(self.c.core.list_namespaced_pod(ns, label_selector=selector)).get("items", [])
            if not any(p["metadata"].get("deletionTimestamp") for p in items):
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(self.poll)

    def wait_gone(self, ns: str, selector: str, timeout: float | None = None) -> bool:
        """True once no pod matches `selector` (terminating pods included), False on timeout."""
        deadline = time.monotonic() + (timeout or self.wait_secs)
        while True:
            if not self.pods(ns, selector):
                return True
            if time.monotonic() > deadline:
                return False
            time.sleep(self.poll)

    def list_namespaces(self, group: str) -> list[str]:
        items = self.c.to_dict(self.c.core.list_namespace(label_selector=f"ramen.io/group={group}")).get("items", [])
        return sorted(n["metadata"]["name"] for n in items if n["metadata"].get("labels", {}).get("ramen.io/group") == group)

    def delete_namespace(self, name: str) -> None:
        try:
            self.c.core.delete_namespace(name)
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 404:
                raise

    def apply(self, doc: dict, keep: tuple[str, ...] = ()) -> None:
        create, patch, _, namespaced = self._fns(doc["kind"])
        m = doc["metadata"]
        try:
            create(m["namespace"], doc) if namespaced else create(doc)
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 409:
                raise
            body = {**doc, "spec": {k: v for k, v in doc.get("spec", {}).items() if k not in keep}} if keep else doc
            patch(m["name"], m["namespace"], body) if namespaced else patch(m["name"], body)

    def read(self, kind, ns, name) -> dict | None:
        _, _, read, namespaced = self._fns(kind)
        try:
            return self.c.to_dict(read(name, ns) if namespaced else read(name))
        except Exception as e:  # noqa: BLE001
            if http_status(e) == 404:
                return None
            raise

    def patch(self, kind, ns, name, body) -> None:
        _, patch, _, namespaced = self._fns(kind)
        patch(name, ns, body) if namespaced else patch(name, body)

    def set_replicas(self, ns, name, n: int) -> None:
        self.patch("Deployment", ns, name, {"spec": {"replicas": n}})

    def restart(self, ns, name) -> None:
        self.patch("Deployment", ns, name, {"spec": {"template": {"metadata": {"annotations": {"ramen.io/restartedAt": now()}}}}})

    def merge_secret(self, ns, name, data: dict[str, str], replace=False) -> None:
        body = {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": name, "namespace": ns}, "type": "Opaque", "stringData": data}
        if replace:
            body["data"] = None
        self.apply(body)

    def secret_values(self, ns, name) -> dict[str, str]:
        import base64
        s = self.read("Secret", ns, name) or {}
        out = {k: base64.b64decode(v).decode() for k, v in (s.get("data") or {}).items()}
        out.update(s.get("stringData") or {})
        return out

    def wait_ready(self, ns, name) -> dict:
        deadline = time.monotonic() + self.wait_secs
        while True:
            d = self.read("Deployment", ns, name)
            if d is None:
                raise ApiError(502, f"deployment {ns}/{name} not found")
            want = int(d["spec"].get("replicas", 1))
            st = d.get("status") or {}
            if want == 0 or (st.get("observedGeneration", 0) >= d["metadata"].get("generation", 0)
                             and st.get("updatedReplicas", 0) >= want and st.get("readyReplicas", 0) >= want
                             and st.get("replicas", 0) == want):
                return d
            if time.monotonic() >= deadline:
                raise TimeoutError(f"deployment {ns}/{name} not ready after {self.wait_secs}s "
                                   f"({st.get('readyReplicas', 0)}/{want} ready)")
            time.sleep(self.poll)

    def pods(self, ns, selector="app=worker") -> list[dict]:
        items = self.c.to_dict(self.c.core.list_namespaced_pod(ns, label_selector=selector)).get("items", [])
        return [p for p in items if not p["metadata"].get("deletionTimestamp")]

    def proxy_get(self, ns, pod, path) -> str:
        r = self.c.core.connect_get_namespaced_pod_proxy_with_path(f"{pod}:{PORT}", ns, path, _preload_content=False)
        return r.data.decode() if hasattr(r, "data") else r
