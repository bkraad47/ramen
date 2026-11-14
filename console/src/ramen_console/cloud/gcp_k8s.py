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
    "/mcp",  # §16.1: Streamable HTTP on the same port
    "/.well-known/oauth-protected-resource",  # §16.3: RFC 9728, unauthenticated by design
)
CANARY_KEEP = ("replicas",)  # never reset canary replicas on re-apply
GW_GROUP, GW_VERSION, GW_PLURAL = "gateway.networking.k8s.io", "v1", "httproutes"
GKE_GROUP, GKE_VERSION, HCP_PLURAL = "networking.gke.io", "v1", "healthcheckpolicies"
ZONE_CLUSTERROLE = "ramen-console-zone"  # unbound ClusterRole from deploy/helm/ramen; bound per zone namespace
# §24.1: real worker pods carry ROLE=worker, the activator of a sleeping zone ROLE=activator. `Service/worker` (the one
# every edge points at) ignores the role, so it selects both; `Service/worker-direct` selects real workers only.
ROLE = "ramen.io/role"
ACTIVATOR = "worker-activator"
DIRECT = "worker-direct"
WORKERS = f"app=worker,{ROLE}!=activator"  # pods that are real workers (pods from before 1.0 carry no role label)
BUILD_NS = "ramen-system"
# pinned (gcr.io tag list checked 2026-11-13); RAMEN_BUILDER_IMAGE overrides it
BUILDER_IMAGE = "gcr.io/kaniko-project/executor:v1.24.0"


def normalize_size(size) -> str | None:
    s = ALIASES.get(str(size), str(size))
    return s if s in SIZES else None


def ns_name(group, zone) -> str:
    return f"ramen-{group}-{zone}"


def _deployment(ns, name, track, group, zone, spec, image, bucket_uri, trust_proxy_hops=2):
    """`trust_proxy_hops`: which `x-forwarded-for` entry, counted from the right, is the client address
    (CONTRACTS §11). 2 on GCP — the external load balancer appends `<client>, <lb>`; AWS passes 1."""
    # ROLE is on the pod template only, never in spec.selector (immutable on an existing Deployment)
    labels = {"app": "worker", "ramen.io/track": track, "ramen.io/group": group, "ramen.io/zone": zone, ROLE: "worker"}
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
                        "RAMEN_TRUST_PROXY_HOPS": str(trust_proxy_hops),
                        # reflection is unauthenticated and reachable through the load balancer (CONTRACTS §11)
                        "RAMEN_REFLECTION": "0",
                    }.items()
                ],
                "envFrom": [{"secretRef": {"name": "ramen-deploy", "optional": True}}],
                "resources": {"requests": res, "limits": res},
                # grpc.health.v1: "" / ramen.v1.Mcp = SERVING once runtime.load succeeded (readiness),
                # ramen.v1.Admin = process alive (liveness) so a slow load never gets the pod killed
                "readinessProbe": {
                    "grpc": {"port": PORT},
                    "periodSeconds": 5,
                    "timeoutSeconds": 5,
                    "failureThreshold": 60,
                },
                "livenessProbe": {
                    "grpc": {"port": PORT, "service": "ramen.v1.Admin"},
                    "periodSeconds": 10,
                    "timeoutSeconds": 5,
                    "failureThreshold": 6,
                },
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
            # stable rolls with a surge pod so capacity never dips; the canary (one pod, pinned to the zone) rolls
            # in place, because a surge pod on a full node in that zone stays Pending and the deploy times out
            "strategy": {
                "type": "RollingUpdate",
                "rollingUpdate": (
                    {"maxUnavailable": 0, "maxSurge": 1} if track == "stable" else {"maxUnavailable": 1, "maxSurge": 0}
                ),
            },
            # 0.7.5: Ramen decides the worker count (§21.7); the cluster autoscaler must not evict a worker to empty a
            # node — a zone at count 1 would go dark. Ramen's own scale-down still frees nodes.
            "template": {
                "metadata": {"labels": labels, "annotations": {SAFE_TO_EVICT: "false"}},
                "spec": pod,
            },
        },
    }


SAFE_TO_EVICT = "cluster-autoscaler.kubernetes.io/safe-to-evict"


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


def direct_service(group, zone) -> dict:
    """`Service/worker-direct` (§24.1): real worker pods only. The activator of a sleeping zone forwards to it and
    polls its health while the zone wakes; no edge object points at it."""
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {
            "name": DIRECT,
            "namespace": ns_name(group, zone),
            "labels": {"ramen.io/group": group, "ramen.io/zone": zone},
        },
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": "worker", ROLE: "worker"},
            "ports": [{"name": PORT_NAME, "port": PORT, "targetPort": PORT_NAME, "appProtocol": "kubernetes.io/h2c"}],
        },
    }


def console_internal_url() -> str:
    """Where a pod inside the cluster reaches the console (the chart's `Service/console`, port 80)."""
    import os

    return os.environ.get("RAMEN_CONSOLE_INTERNAL_URL") or "http://console.ramen-system.svc.cluster.local:80"


def activator(worker: dict, service_selector: dict, console_url: str | None = None) -> dict:
    """`Deployment/worker-activator` (§24.1) derived from the zone's live `worker` Deployment: the same image, the same
    env and `envFrom` (the zone's whole worker config), the same readiness probe, ServiceAccount and node placement —
    minus the bucket volume, at 10m / 32Mi. Its pod labels are the live `Service/worker` selector plus
    ROLE=activator, so whatever the provider's edge selects (GKE NEG, ALB target group, Envoy route, NodePort) keeps
    selecting it; `worker-direct` (ROLE=worker) never does. On AWS that selector carries `ramen.io/track: stable`, which
    the `worker` Deployment's selector matches too: harmless, ReplicaSets own their pods by `pod-template-hash`, and
    every pod query of the adapters excludes ROLE=activator."""
    meta, tpl = worker["metadata"], worker["spec"]["template"]
    pod, src = tpl["spec"], tpl["spec"]["containers"][0]
    zone_labels = {k: v for k, v in (meta.get("labels") or {}).items() if k in ("ramen.io/group", "ramen.io/zone")}
    labels = {**zone_labels, **(service_selector or {"app": "worker"}), "app": "worker", ROLE: "activator"}
    env = [e for e in src.get("env") or [] if e["name"] not in ACTIVATOR_ENV]
    env += [
        {"name": "RAMEN_ACTIVATOR", "value": "1"},
        {"name": "RAMEN_CONSOLE_INTERNAL_URL", "value": console_url or console_internal_url()},
        {"name": "RAMEN_ACTIVATOR_UPSTREAM", "value": f"{DIRECT}:{PORT}"},
    ]
    container = {
        "name": "worker",  # the same container name: log queries by container keep finding it
        "image": src["image"],
        "ports": src.get("ports") or [{"name": PORT_NAME, "containerPort": PORT}],
        "env": env,
        "resources": {"requests": {"cpu": "10m", "memory": "32Mi"}, "limits": {"cpu": "250m", "memory": "128Mi"}},
        "readinessProbe": src.get("readinessProbe") or {"grpc": {"port": PORT}, "periodSeconds": 5},
    }
    for k in ("imagePullPolicy", "securityContext", "envFrom"):
        if src.get(k) is not None:
            container[k] = src[k]
    spec = {"containers": [container]}
    for k in ("serviceAccountName", "securityContext", "nodeSelector", "imagePullSecrets", "tolerations"):
        if pod.get(k) is not None:
            spec[k] = pod[k]
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": ACTIVATOR, "namespace": meta["namespace"], "labels": {**zone_labels, ROLE: "activator"}},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "worker", ROLE: "activator"}},
            "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
            "template": {"metadata": {"labels": labels, "annotations": {SAFE_TO_EVICT: "false"}}, "spec": spec},
        },
    }


ACTIVATOR_ENV = ("RAMEN_ACTIVATOR", "RAMEN_CONSOLE_INTERNAL_URL", "RAMEN_ACTIVATOR_UPSTREAM")


def selects(selector: dict, labels: dict) -> bool:
    """Does a Service selector (equality map) match these pod labels?"""
    return bool(selector) and all(labels.get(k) == v for k, v in selector.items())


def build_name(tag: str) -> str:
    import hashlib

    return f"ramen-build-{hashlib.sha256(tag.encode()).hexdigest()[:16]}"


def _from_image(dockerfile: str) -> str | None:
    for line in dockerfile.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0].upper() == "FROM":
            refs = [p for p in parts[1:] if not p.startswith("--")]
            return refs[0] if refs else None
    return None


def build_objects(
    group: str,
    tag: str,
    image: str,
    dockerfile: str,
    files: dict[str, str],
    *,
    builder_image: str = BUILDER_IMAGE,
    init_image: str | None = None,
    args: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    pod_labels: dict[str, str] | None = None,
    docker_config: dict | None = None,
    cpu: str = "250m",
    memory: str = "768Mi",
    memory_limit: str = "2Gi",
) -> tuple[dict, dict]:
    """(ConfigMap, Job) of one image build (§24.2). The ConfigMap is the build context; an init container (the base
    image of the build: the nodes already pull it and it has `cp`) copies it into an emptyDir, because kaniko copies a
    ConfigMap volume's symlinks as symlinks. kaniko runs as the image's root but unprivileged (GKE Autopilot refuses
    privileged pods); the registry credential is `ramen-builder`'s workload identity, nothing is mounted."""
    import json

    name = build_name(tag)
    labels = {"app.kubernetes.io/name": "ramen-build", "ramen.io/group": group}
    paths = {"Dockerfile": dockerfile, **files}
    keys = {path: f"f{i}" for i, path in enumerate(sorted(paths))}  # ConfigMap keys cannot hold a `/`
    data = {keys[p]: paths[p] for p in paths}
    volumes = [
        {"name": "context", "configMap": {"name": name, "items": [{"key": k, "path": p} for p, k in keys.items()]}},
        {"name": "workspace", "emptyDir": {}},
    ]
    mounts = [{"name": "workspace", "mountPath": "/workspace"}]
    if docker_config is not None:
        data["docker-config"] = json.dumps(docker_config)
        volumes.append(
            {"name": "docker", "configMap": {"name": name, "items": [{"key": "docker-config", "path": "config.json"}]}}
        )
        mounts.append({"name": "docker", "mountPath": "/kaniko/.docker"})
    # Requests a small node can place (a 2 GiB node allocates about 1.4 GiB: the first cloud run asked for 2Gi and
    # no EKS or AKS node could take the Job); the memory limit leaves room for a larger requirements file.
    requests = {"cpu": cpu, "memory": memory, "ephemeral-storage": "2Gi"}
    limits = {"cpu": cpu, "memory": memory_limit, "ephemeral-storage": "4Gi"}
    pod = {
        "serviceAccountName": "ramen-builder",
        "restartPolicy": "Never",
        "initContainers": [
            {
                "name": "context",
                "image": init_image or _from_image(dockerfile) or "busybox:1.37.0",
                "command": ["sh", "-c", "cp -RL /context/. /workspace/"],
                "securityContext": {"allowPrivilegeEscalation": False, "capabilities": {"drop": ["ALL"]}},
                "resources": {"requests": {"cpu": "50m", "memory": "64Mi"}, "limits": {"cpu": "50m", "memory": "64Mi"}},
                "volumeMounts": [{"name": "context", "mountPath": "/context"}, *mounts[:1]],
            }
        ],
        "containers": [
            {
                "name": "build",
                "image": builder_image,
                "args": [
                    "--context=dir:///workspace",
                    "--dockerfile=/workspace/Dockerfile",
                    f"--destination={image}",
                    "--push-retry=3",
                    *args,
                ],
                "env": [{"name": k, "value": v} for k, v in (env or {}).items()],
                "resources": {"requests": requests, "limits": limits},
                "volumeMounts": mounts,
            }
        ],
        "volumes": volumes,
    }
    meta = {"name": name, "namespace": BUILD_NS, "labels": labels, "annotations": {"ramen.io/image": image}}
    cm = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": dict(meta), "data": data}
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": dict(meta),
        "spec": {
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": 600,
            "template": {
                "metadata": {
                    "labels": {**labels, **(pod_labels or {})},
                    "annotations": {SAFE_TO_EVICT: "false"},
                },
                "spec": pod,
            },
        },
    }
    return cm, job


def job_state(job: dict | None) -> str:
    """`succeeded` | `failed` | `running`."""
    st = (job or {}).get("status") or {}
    conds = {c.get("type"): c.get("status") for c in st.get("conditions") or []}
    if st.get("succeeded", 0) >= 1 or conds.get("Complete") == "True":
        return "succeeded"
    if st.get("failed", 0) >= 1 or conds.get("Failed") == "True":
        return "failed"
    return "running"


def manifests(group, zone, spec, image, bucket_uri, gsa=None) -> list[dict]:
    ns = ns_name(group, zone)
    labels = {"ramen.io/group": group, "ramen.io/zone": zone}
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
        httproute(group, zone),
        healthcheckpolicy(group, zone),
        direct_service(group, zone),  # §24.1: real workers only, for the activator of a sleeping zone
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
        "hpa.enabled": "false",  # §21.7: the console scales workers (rebalance), not an HPA
    }
    cmd = ["helm", "template", "ramen-worker", str(chart), "--namespace", ns]
    for k, v in values.items():
        cmd += ["--set", f"{k}={v}"]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise ApiError(502, f"Helm template failed: {r.stderr.strip()[:500]}")
    docs = [d for d in yaml.safe_load_all(r.stdout) if d]
    for d in docs:
        if d["kind"] != "Namespace":
            d["metadata"].setdefault("namespace", ns)
        else:
            d["metadata"].setdefault("labels", {})["ramen.io/routes"] = "true"
    ensure_direct(docs, group, zone)
    if not any(d["kind"] == "HTTPRoute" for d in docs):
        docs.append(httproute(group, zone))
    if not any(d["kind"] == "HealthCheckPolicy" for d in docs):
        docs.append(healthcheckpolicy(group, zone))
    return docs


def ensure_direct(docs: list[dict], group, zone) -> None:
    """A chart from before 1.0 renders neither `worker-direct` nor the role label: add both (§24.1)."""
    if not any(d["kind"] == "Service" and d["metadata"]["name"] == DIRECT for d in docs):
        docs.append(direct_service(group, zone))
    for d in docs:
        if d["kind"] == "Deployment" and d["metadata"]["name"] in ("worker", "worker-canary"):
            d["spec"]["template"].setdefault("metadata", {}).setdefault("labels", {}).setdefault(ROLE, "worker")


SAME_ZONE_PEER = {"podSelector": {"matchLabels": {"app": "worker"}}}  # §24.1: the activator reaches worker-direct


def same_zone_rule() -> dict:
    return {"from": [dict(SAME_ZONE_PEER)], "ports": [{"port": PORT, "protocol": "TCP"}]}


GCP_LB_CIDRS = ["35.191.0.0/16", "130.211.0.0/22"]  # Google front ends / health checkers


def networkpolicy(ns: str, group: str, zone: str, lb_cidrs: list[str] | None = None) -> dict:
    """Workers accept ingress only from the console namespace, the load balancer (SEC-04) and the zone's own worker
    pods (§24.1: a sleeping zone's activator forwards to `worker-direct`)."""
    peers = [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ramen-system"}}}]
    peers += [{"ipBlock": {"cidr": c}} for c in (lb_cidrs if lb_cidrs is not None else GCP_LB_CIDRS)]
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": "worker", "namespace": ns, "labels": {"ramen.io/group": group, "ramen.io/zone": zone}},
        "spec": {
            "podSelector": {"matchLabels": {"app": "worker"}},
            "policyTypes": ["Ingress"],
            "ingress": [
                {"from": peers, "ports": [{"port": PORT, "protocol": "TCP"}]},
                same_zone_rule(),  # its own rule: the first one stays "console + load balancer" for every reader
            ],
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
            "Service": (
                c.core.create_namespaced_service,
                c.core.patch_namespaced_service,
                c.core.read_namespaced_service,
                True,
            ),
            "ConfigMap": (
                c.core.create_namespaced_config_map,
                c.core.patch_namespaced_config_map,
                c.core.read_namespaced_config_map,
                True,
            ),
            "Job": (c.batch.create_namespaced_job, c.batch.patch_namespaced_job, c.batch.read_namespaced_job, True),
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
            raise ApiError(502, f"Unsupported manifest kind {kind}")
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

    def delete(self, kind, ns, name) -> bool:
        """Delete one object; False when it was not there (404). Used to drop the pre-0.7.5 worker HPA (§21.7)."""
        fns = {
            "HorizontalPodAutoscaler": lambda: self.c.autoscaling.delete_namespaced_horizontal_pod_autoscaler(name, ns),
            "ConfigMap": lambda: self.c.core.delete_namespaced_config_map(name, ns),
            # Background: the Job object goes at once (a re-run may reuse the name), its pod is collected after
            "Job": lambda: self.c.batch.delete_namespaced_job(name, ns, propagation_policy="Background"),
        }
        if kind not in fns:
            raise ValueError(f"delete not supported for {kind}")
        try:
            fns[kind]()
            return True
        except Exception as e:  # noqa: BLE001
            if http_status(e) == 404:
                return False
            raise

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
                raise ApiError(502, f"Deployment {ns}/{name} not found")
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
                    f"({st.get('readyReplicas', 0)}/{want} ready{self._stuck_pods(ns, name)})"
                )
            time.sleep(self.poll)

    def wait_endpoint(self, ns, service, selector, timeout=None) -> bool:
        """True once a ready address of `Service/<service>` belongs to a pod matching `selector`, False on timeout.
        §24.1: kube-proxy keeps routing to terminating-but-serving workers until the activator is in the Service's
        endpoints, so a sleep that scales the workers to 0 before that sends calls to a draining worker, and
        nothing asks for the wake (1.0.0a P1). A console whose zone role cannot read Endpoints raises (403)."""
        deadline = time.monotonic() + (timeout or self.wait_secs)
        while True:
            ips = {(p.get("status") or {}).get("podIP") for p in self.pods(ns, selector)} - {None}
            try:
                ep = self.c.to_dict(self.c.core.read_namespaced_endpoints(service, ns)) or {}
            except Exception as e:  # noqa: BLE001
                if http_status(e) != 404:
                    raise
                ep = {}
            listed = {a.get("ip") for s in ep.get("subsets") or [] for a in s.get("addresses") or []}
            if ips and ips & listed:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(self.poll)

    def _stuck_pods(self, ns, name) -> str:
        """Why the rollout is stuck, from the pods themselves: a Pending pod's scheduler message, a waiting
        container's reason. The replica counts alone said "1/1 ready" while the new pod sat Unschedulable."""
        try:
            track = "canary" if name.endswith("-canary") else "stable"
            selector = f"app=worker,{ROLE}=activator" if name == ACTIVATOR else f"{WORKERS},ramen.io/track={track}"
            return self._stuck_pods_of(self.pods(ns, selector))
        except Exception:  # a diagnostic must never mask the timeout itself
            return ""

    def _stuck_pods_of(self, pods: list[dict]) -> str:
        notes = []
        for p in pods:
            st = p.get("status") or {}
            if st.get("phase") == "Running" and not any(
                (c.get("state") or {}).get("waiting") for c in st.get("containerStatuses") or []
            ):
                continue
            why = [
                f"{c.get('reason')}: {c.get('message')}"
                for c in st.get("conditions") or []
                if c.get("status") == "False" and c.get("reason")
            ] + [
                f"{(c.get('state') or {}).get('waiting', {}).get('reason')}"
                for c in [*(st.get("initContainerStatuses") or []), *(st.get("containerStatuses") or [])]
                if (c.get("state") or {}).get("waiting")
            ]
            why = list(dict.fromkeys(why))  # Ready and ContainersReady carry the same reason
            notes.append(f"{p['metadata']['name']} {st.get('phase')}" + (f" ({'; '.join(why)})" if why else ""))
        return f"; pods: {', '.join(notes)}" if notes else ""

    def pod_log(self, ns, pod, container=None, tail=40) -> str:
        kw = {"container": container} if container else {}
        return self.c.core.read_namespaced_pod_log(pod, ns, tail_lines=tail, **kw) or ""

    def pods(self, ns, selector=WORKERS) -> list[dict]:
        items = self.c.to_dict(self.c.core.list_namespaced_pod(ns, label_selector=selector)).get("items", [])
        return [p for p in items if not p["metadata"].get("deletionTimestamp")]
