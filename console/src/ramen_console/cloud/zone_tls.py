"""D86 (CONTRACTS §39.2–§39.3): what a zone's manifests gain on a TLS install — the leaf (Secret `ramen-tls`), the CA
bundle (ConfigMap `ramen-ca`), workers serving TLS with HTTPS probes, and each edge's TLS backend: GKE `HTTP2`
backends + an `HTTP2` health check, ALB `HTTPS` target groups, Envoy Gateway `BackendTLSPolicy` (the one edge that
verifies the worker). D92 (§39.2a): the leaf names its zone (`<group>.<zone>.worker.ramen.internal`), Envoy and the
activator verify that name."""

from .. import tls
from .gcp_k8s import PORT, ns_name

SECRET, CONFIGMAP = "ramen-tls", "ramen-ca"
TLS_DIR = "/etc/ramen/tls"
VOLUME = {"name": SECRET, "secret": {"secretName": SECRET}}  # a directory (no items/subPath): renewals land in place
MOUNT = {"name": SECRET, "mountPath": TLS_DIR, "readOnly": True}
ENV = {
    "RAMEN_TLS_CERT": f"{TLS_DIR}/tls.crt",
    "RAMEN_TLS_KEY": f"{TLS_DIR}/tls.key",
    "RAMEN_TLS_CA": f"{TLS_DIR}/ca.crt",
    "RAMEN_REQUIRE_TLS": "1",
}
SERVER_NAME = "RAMEN_TLS_SERVER_NAME"  # D92: the zone name the activator verifies `worker-direct` as
# `grpc: None` is sent as null: an update is a strategic merge patch, and only a null removes the pre-D86 grpc handler
# (otherwise "may not specify more than 1 handler type", GKE 1.0.0b30)
READY = {"httpGet": {"path": "/readyz", "port": PORT, "scheme": "HTTPS"}, "grpc": None}
ALIVE = {"httpGet": {"path": "/healthz", "port": PORT, "scheme": "HTTPS"}, "grpc": None}
APP_PROTOCOL = {"GCP": "HTTP2", "AWS": "HTTP2", "Azure": "kubernetes.io/h2c"}  # Envoy: h2c = HTTP/2; the policy = TLS
ALB = "alb.ingress.kubernetes.io/"
BTLS_GROUP, BTLS_VERSION, BTLS_PLURAL = "gateway.networking.k8s.io", "v1", "backendtlspolicies"
WORKERS = ("worker", "worker-canary", "worker-activator")


def ca() -> tls.CA:
    if (c := tls.current()) is None:
        raise tls.CaError("no install CA is loaded in this console (RAMEN_REQUIRE_TLS=1 needs one)")
    return c


def fingerprint() -> str:
    """The issuing CA's fingerprint, as the deploy log and the rotate answer print it."""
    return tls.fingerprint(ca().cert)


def _meta(group: str, zone: str) -> dict:
    return {"namespace": ns_name(group, zone), "labels": {"ramen.io/group": group, "ramen.io/zone": zone}}


def bundle_configmap(group: str, zone: str) -> dict:
    """ConfigMap `ramen-ca`: the bundle Envoy's BackendTLSPolicy and the Jobs verify the zone with."""
    meta = _meta(group, zone)
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": CONFIGMAP, **meta},
        "data": {"ca.crt": ca().bundle},
    }


def objects(group: str, zone: str, legacy: bool = False) -> list[dict]:
    """A fresh leaf for the zone and the bundle: Secret `ramen-tls` and ConfigMap `ramen-ca`. `legacy` (renewal of a
    zone not redeployed since D92): the leaf also keeps the generic name its activator and Envoy policy verify."""
    meta = _meta(group, zone)
    leaf = ca().issue(tls.zone_names(meta["namespace"], tls.worker_name(group, zone), legacy))
    return [
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": SECRET, **meta},
            "type": "kubernetes.io/tls",
            "stringData": {"tls.crt": leaf.cert, "tls.key": leaf.key, "ca.crt": leaf.ca},
        },
        bundle_configmap(group, zone),
    ]


def secure_pod(pod: dict, name: str | None = None) -> None:
    """Mount the leaf, point the node at it, and probe over HTTPS (kubelet's gRPC probe cannot speak TLS). `name`: the
    zone's worker name (D92), which the activator — derived from this pod's env — verifies `worker-direct` as."""
    c = pod["containers"][0]
    want = {**ENV, **({SERVER_NAME: name} if name else {})}
    env = [e for e in c.get("env") or [] if e["name"] not in want]
    c["env"] = [*env, *({"name": k, "value": v} for k, v in want.items())]
    c["volumeMounts"] = [m for m in c.get("volumeMounts") or [] if m["name"] != SECRET] + [dict(MOUNT)]
    pod["volumes"] = [v for v in pod.get("volumes") or [] if v["name"] != SECRET] + [
        {"name": SECRET, "secret": dict(VOLUME["secret"])}
    ]
    if "readinessProbe" in c:
        c["readinessProbe"] = {**{k: v for k, v in c["readinessProbe"].items() if k != "grpc"}, **READY}
    if "livenessProbe" in c:
        c["livenessProbe"] = {**{k: v for k, v in c["livenessProbe"].items() if k != "grpc"}, **ALIVE}


def backend_tls_policy(ns: str, labels: dict) -> dict:
    """Envoy verifies the zone's pods as the zone's own name (D92), never as another zone's."""
    return {
        "apiVersion": f"{BTLS_GROUP}/{BTLS_VERSION}",
        "kind": "BackendTLSPolicy",
        "metadata": {"name": "worker", "namespace": ns, "labels": labels},
        "spec": {
            "targetRefs": [{"group": "", "kind": "Service", "name": "worker"}],
            "validation": {
                "caCertificateRefs": [{"group": "", "kind": "ConfigMap", "name": CONFIGMAP}],
                "hostname": tls.worker_name(labels["ramen.io/group"], labels["ramen.io/zone"]),
            },
        },
    }


def secure(docs: list[dict], provider: str) -> list[dict]:
    """The zone's docs on a TLS install (idempotent). `provider`: GCP | AWS | Azure."""
    out, ns, labels = [], None, {}
    for d in docs:
        kind, name = d["kind"], d["metadata"]["name"]
        if kind == "Deployment" and name in WORKERS:
            ns, labels = (
                d["metadata"]["namespace"],
                {k: v for k, v in d["metadata"].get("labels", {}).items() if k in ("ramen.io/group", "ramen.io/zone")},
            )
            zname = (
                tls.worker_name(*(labels[k] for k in ("ramen.io/group", "ramen.io/zone"))) if len(labels) == 2 else None
            )
            secure_pod(d["spec"]["template"]["spec"], zname)
        elif kind == "Service" and name.startswith("worker"):
            for p in d["spec"]["ports"]:
                p["appProtocol"] = APP_PROTOCOL[provider]
        elif kind == "HealthCheckPolicy":
            d["spec"]["default"]["config"] = {
                "type": "HTTP2",
                "http2HealthCheck": {"port": PORT, "requestPath": "/readyz"},
                "grpcHealthCheck": None,  # null: a merge patch keeps the pre-D86 GRPC check otherwise (GKE 1.0.0b31)
            }
        elif kind == "Ingress":
            ann = d["metadata"].setdefault("annotations", {})
            ann[ALB + "backend-protocol"] = "HTTPS"
            ann[ALB + "healthcheck-protocol"] = "HTTPS"
        if kind != "BackendTLSPolicy":
            out.append(d)
    if provider == "Azure" and ns:
        out.append(backend_tls_policy(ns, labels))
    return out
