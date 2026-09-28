"""Worker namespace manifests (CONTRACTS §7, §11: gRPC h2c, header routing) and a thin apply/wait layer over the
kubernetes client."""

import shutil
import subprocess
import time

import yaml

from ..errors import ApiError
from ..util import now
from .gcp_clients import http_status

SIZES = {
    "s": {"cpu": "250m", "memory": "512Mi"},
    "m": {"cpu": "500m", "memory": "1Gi"},
    "l": {"cpu": "1", "memory": "2Gi"},
}
ALIASES = {"small": "s", "medium": "m", "large": "l"}
PORT = 8080
PORT_NAME = "grpc"
MCP_PATH = "/ramen.v1.Mcp"
# Services reachable through the LB (CONTRACTS §11): Mcp (bearer key), Health and reflection (unauthenticated by
# contract; clients/harness probe readiness and discover services through the LB). Admin stays cluster-internal.
LB_PATHS = (
    MCP_PATH,
    "/grpc.health.v1.Health",
    "/grpc.reflection.v1.ServerReflection",
    "/grpc.reflection.v1alpha.ServerReflection",
)
CANARY_KEEP = ("replicas",)  # never reset canary replicas on re-apply
GW_GROUP, GW_VERSION, GW_PLURAL = "gateway.networking.k8s.io", "v1", "httproutes"
GKE_GROUP, GKE_VERSION, HCP_PLURAL = "networking.gke.io", "v1", "healthcheckpolicies"
ZONE_CLUSTERROLE = "ramen-console-zone"  # unbound ClusterRole from deploy/helm/ramen; bound per zone namespace


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
        "containers": [
            {
                "name": "worker",
                "image": image,
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "capabilities": {"drop": ["ALL"]},
                    "seccompProfile": {"type": "RuntimeDefault"},
                },
                "ports": [{"name": PORT_NAME, "containerPort": PORT}],
                "env": [
                    {"name": k, "value": v}
                    for k, v in {
                        "RAMEN_BUCKET_URI": bucket_uri,
                        "RAMEN_BUCKET": "/data/bucket",
                        "RAMEN_GROUP": group,
                        "RAMEN_ZONE": zone,
                        "RAMEN_TRACK": track,
                        "RAMEN_NODE_PORT": str(PORT),
                        "RAMEN_TRUST_PROXY": "1",
                    }.items()
                ],
                "envFrom": [{"secretRef": {"name": "ramen-deploy", "optional": True}}],
                "resources": {"requests": res, "limits": res},
                # grpc.health.v1: "" / ramen.v1.Mcp = SERVING once runtime.load succeeded (readiness),
                # ramen.v1.Admin = process alive (liveness) so a slow load never gets the pod killed
                "readinessProbe": {"grpc": {"port": PORT}, "periodSeconds": 5, "failureThreshold": 60},
                "livenessProbe": {"grpc": {"port": PORT, "service": "ramen.v1.Admin"}, "periodSeconds": 10},
                "volumeMounts": [{"name": "bucket", "mountPath": "/data/bucket"}],
            }
        ],
        "volumes": [{"name": "bucket", "emptyDir": {}}],
    }
    if spec.get("region"):
        pod["nodeSelector"] = {"topology.kubernetes.io/zone": spec["region"]}
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "replicas": int(spec.get("count", 1)) if track == "stable" else 0,
            "selector": {"matchLabels": {"app": "worker", "ramen.io/track": track}},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
            "template": {"metadata": {"labels": labels}, "spec": pod},
        },
    }


def route_headers(group, zone) -> list[dict]:
    return [{"name": "ramen-group", "value": group}, {"name": "ramen-zone", "value": zone}]


def httproute(group, zone) -> dict:
    """HTTPRoute `worker`: Gateway ramen-system/ramen routes gRPC calls carrying metadata ramen-group/ramen-zone
    (paths LB_PATHS: Mcp, Health, reflection; no rewrite) → Service worker:8080 (h2c). Admin is not routed."""
    ns = ns_name(group, zone)
    return {
        "apiVersion": f"{GW_GROUP}/{GW_VERSION}",
        "kind": "HTTPRoute",
        "metadata": {"name": "worker", "namespace": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}},
        "spec": {
            "parentRefs": [{"group": GW_GROUP, "kind": "Gateway", "name": "ramen", "namespace": "ramen-system"}],
            "rules": [
                {
                    "matches": [
                        {"path": {"type": "PathPrefix", "value": path}, "headers": route_headers(group, zone)}
                        for path in LB_PATHS
                    ],
                    "backendRefs": [{"name": "worker", "port": PORT}],
                }
            ],
        },
    }


def healthcheckpolicy(group, zone) -> dict:
    """GKE health check of type GRPC (grpc.health.v1: SERVING once runtime.load succeeded)."""
    ns = ns_name(group, zone)
    return {
        "apiVersion": f"{GKE_GROUP}/{GKE_VERSION}",
        "kind": "HealthCheckPolicy",
        "metadata": {"name": "worker", "namespace": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}},
        "spec": {
            "default": {"checkIntervalSec": 15, "config": {"type": "GRPC", "grpcHealthCheck": {"port": PORT}}},
            "targetRef": {"group": "", "kind": "Service", "name": "worker"},
        },
    }


def rolebinding(group, zone, ksa_namespace="ramen-system", ksa="console") -> dict:
    """RoleBinding of the console KSA to the unbound ClusterRole `ramen-console-zone` inside this zone namespace only
    (SEC-09: secrets/serviceaccounts/deployments verbs are never granted cluster-wide)."""
    ns = ns_name(group, zone)
    return {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {
            "name": "ramen-console",
            "namespace": ns,
            "labels": {"ramen.io/group": group, "ramen.io/zone": zone},
        },
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": ZONE_CLUSTERROLE},
        "subjects": [{"kind": "ServiceAccount", "name": ksa, "namespace": ksa_namespace}],
    }


def manifests(group, zone, spec, image, bucket_uri, gsa=None) -> list[dict]:
    ns = ns_name(group, zone)
    labels = {"ramen.io/group": group, "ramen.io/zone": zone}
    count = int(spec.get("count", 1))
    ksa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "worker", "namespace": ns, "labels": labels},
    }
    if gsa:
        ksa["metadata"]["annotations"] = {"iam.gke.io/gcp-service-account": gsa}
    return [
        {
            "apiVersion": "v1",
            "kind": "Namespace",
            "metadata": {"name": ns, "labels": {**labels, "ramen.io/routes": "true"}},
        },
        networkpolicy(ns, group, zone),
        ksa,
        {
            "apiVersion": "v1",
            "kind": "Service",
            "metadata": {
                "name": "worker",
                "namespace": ns,
                "labels": labels,
                "annotations": {"cloud.google.com/neg": f'{{"exposed_ports": {{"{PORT}": {{"name": "{ns}"}}}}}}'},
            },
            "spec": {
                "type": "ClusterIP",
                "selector": {"app": "worker"},
                "ports": [
                    {"name": PORT_NAME, "port": PORT, "targetPort": PORT_NAME, "appProtocol": "kubernetes.io/h2c"}
                ],
            },
        },
        _deployment(ns, "worker", "stable", group, zone, spec, image, bucket_uri),
        _deployment(ns, "worker-canary", "canary", group, zone, spec, image, bucket_uri),
        {
            "apiVersion": "autoscaling/v2",
            "kind": "HorizontalPodAutoscaler",
            "metadata": {"name": "worker", "namespace": ns, "labels": labels},
            "spec": {
                "scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": "worker"},
                "minReplicas": count,
                "maxReplicas": max(int(spec.get("max_count") or 0), count * 2),
                "metrics": [
                    {
                        "type": "Resource",
                        "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 70}},
                    }
                ],
            },
        },
        httproute(group, zone),
        healthcheckpolicy(group, zone),
    ]


def helm_available(chart) -> bool:
    return bool(chart) and shutil.which("helm") is not None


def helm_manifests(chart, project, group, zone, spec, image, bucket_uri, gsa=None) -> list[dict]:
    """Render deploy/helm/ramen-worker when RAMEN_WORKER_CHART points at it; values mirror manifests()."""
    ns = ns_name(group, zone)
    count = int(spec.get("count", 1))
    values = {
        "project": project,
        "group": group,
        "zone": zone,
        "gcpZone": spec.get("region", ""),
        "image": image,
        "bucketUri": bucket_uri,
        "size": normalize_size(spec.get("size")) or "s",
        "replicas": count,
        "serviceAccount.gsa": gsa or "",
        "secret.create": "false",
        "hpa.enabled": "true",
        "hpa.minReplicas": count,
        "hpa.maxReplicas": max(int(spec.get("max_count") or 0), count * 2),
    }
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
    if not any(d["kind"] == "HealthCheckPolicy" for d in docs):
        docs.append(healthcheckpolicy(group, zone))
    return docs


GCP_LB_CIDRS = ["35.191.0.0/16", "130.211.0.0/22"]  # Google front ends / health checkers


def networkpolicy(ns: str, group: str, zone: str, lb_cidrs: list[str] | None = None) -> dict:
    """Workers accept ingress only from the console namespace and the load balancer (SEC-04)."""
    peers = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ramen-system"}}}]
    peers += [{"ipBlock": {"cidr": c}} for c in (lb_cidrs if lb_cidrs is not None else GCP_LB_CIDRS)]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "worker", "namespace": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}},
        "spec": {
            "podSelector": {"matchLabels": {"app": "worker"}},
            "policyTypes": ["Ingress"],
            "ingress": [{"from": peers, "ports": [{"port": PORT, "protocol": "TCP"}]}],
        },
    }


class Kube:
    def __init__(self, clients, poll=2.0, wait_secs=300, rbac_wait=30.0):
        self.c, self.poll, self.wait_secs, self.rbac_wait = clients, poll, wait_secs, rbac_wait

    def _fns(self, kind):
        c = self.c
        table = {
            "Namespace": (c.core.create_namespace, c.core.patch_namespace, c.core.read_namespace, False),
            "ServiceAccount": (
                c.core.create_namespaced_service_account,
                c.core.patch_namespaced_service_account,
                c.core.read_namespaced_service_account,
                True,
            ),
            "NetworkPolicy": (
                c.networking.create_namespaced_network_policy,
                c.networking.patch_namespaced_network_policy,
                c.networking.read_namespaced_network_policy,
                True,
            ),
            "Secret": (
                c.core.create_namespaced_secret,
                c.core.patch_namespaced_secret,
                c.core.read_namespaced_secret,
                True,
            ),
            "Service": (c.core.create_namespaced_service, c.core.patch_namespaced_service, None, True),
            "Deployment": (
                c.apps.create_namespaced_deployment,
                c.apps.patch_namespaced_deployment,
                c.apps.read_namespaced_deployment,
                True,
            ),
            "HorizontalPodAutoscaler": (
                c.autoscaling.create_namespaced_horizontal_pod_autoscaler,
                c.autoscaling.patch_namespaced_horizontal_pod_autoscaler,
                c.autoscaling.read_namespaced_horizontal_pod_autoscaler,
                True,
            ),
            "HTTPRoute": self._custom(GW_GROUP, GW_VERSION, GW_PLURAL),
            "HealthCheckPolicy": self._custom(GKE_GROUP, GKE_VERSION, HCP_PLURAL),
            "RoleBinding": (
                c.rbac.create_namespaced_role_binding,
                c.rbac.patch_namespaced_role_binding,
                c.rbac.read_namespaced_role_binding,
                True,
            ),
        }
        if kind not in table:
            raise ApiError(502, f"unsupported manifest kind {kind}")
        return table[kind]

    def _custom(self, group, version, plural):
        c = self.c.custom
        return (
            lambda ns, body: c.create_namespaced_custom_object(group, version, ns, plural, body),
            lambda name, ns, body: c.patch_namespaced_custom_object(group, version, ns, plural, name, body),
            lambda name, ns: c.get_namespaced_custom_object(group, version, ns, plural, name),
            True,
        )

    def wait_rbac(self, ns: str, kind: str = "ServiceAccount", name: str = "worker") -> None:
        """Block until a zone-scoped read in `ns` is authorised. A RoleBinding is not honoured the instant it is
        created (the API server's RBAC cache lags by up to a few seconds): reads answer 403, not 404, until then."""
        deadline = time.monotonic() + self.rbac_wait
        while True:
            try:
                self.read(kind, ns, name)
                return
            except Exception as e:  # noqa: BLE001
                if http_status(e) != 403 or time.monotonic() > deadline:
                    raise
                time.sleep(min(self.poll, 1.0))

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
        return sorted(
            n["metadata"]["name"] for n in items if n["metadata"].get("labels", {}).get("ramen.io/group") == group
        )

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
        self.patch(
            "Deployment",
            ns,
            name,
            {"spec": {"template": {"metadata": {"annotations": {"ramen.io/restartedAt": now()}}}}},
        )

    def merge_secret(self, ns, name, data: dict[str, str], replace=False) -> None:
        body = {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": name, "namespace": ns},
            "type": "Opaque",
            "stringData": data,
        }
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
            if want == 0 or (
                st.get("observedGeneration", 0) >= d["metadata"].get("generation", 0)
                and st.get("updatedReplicas", 0) >= want
                and st.get("readyReplicas", 0) >= want
                and st.get("replicas", 0) == want
            ):
                return d
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"deployment {ns}/{name} not ready after {self.wait_secs}s "
                    f"({st.get('readyReplicas', 0)}/{want} ready)"
                )
            time.sleep(self.poll)

    def pods(self, ns, selector="app=worker") -> list[dict]:
        items = self.c.to_dict(self.c.core.list_namespaced_pod(ns, label_selector=selector)).get("items", [])
        return [p for p in items if not p["metadata"].get("deletionTimestamp")]
