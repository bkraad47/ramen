"""Worker namespace manifests for EKS (CONTRACTS §8, §11): ALB Ingress in IngressGroup `ramen` routing gRPC calls by
the `ramen-group`/`ramen-zone` headers (target groups `backend-protocol-version: GRPC`, health check gRPC code 0),
IRSA KSA, stable/canary Services behind one weighted forward action. Everything else (Deployments, HPA, apply/wait)
is shared with the GCP layer."""

import copy
import json
import os
import subprocess

import yaml

from ..errors import ApiError
from .gcp_k8s import (
    LB_PATHS,
    PORT,
    PORT_NAME,
    Kube,
    _deployment,
    helm_available,
    networkpolicy,
    normalize_size,
    ns_name,
)

__all__ = [
    "ACTION",
    "CONDITIONS",
    "ROLE_ANNOTATION",
    "AwsKube",
    "helm_available",
    "helm_manifests",
    "ingress",
    "manifests",
    "parse_weights",
    "split_weights",
    "weights",
]

ACTION = "alb.ingress.kubernetes.io/actions.worker"
CONDITIONS = "alb.ingress.kubernetes.io/conditions.worker"
ROLE_ANNOTATION = "eks.amazonaws.com/role-arn"


class AwsKube(Kube):
    def _fns(self, kind):
        if kind == "Ingress":
            n = self.c.networking
            return (n.create_namespaced_ingress, n.patch_namespaced_ingress, n.read_namespaced_ingress, True)
        return super()._fns(kind)


def weights(stable: int, canary: int) -> str:
    """Value of the `actions.worker` annotation: weighted forward to the stable and canary target groups."""
    tg = [
        {"serviceName": "worker", "servicePort": str(PORT), "weight": int(stable)},
        {"serviceName": "worker-canary", "servicePort": str(PORT), "weight": int(canary)},
    ]
    return json.dumps({"type": "forward", "forwardConfig": {"targetGroups": tg}})


def parse_weights(ingress: dict | None) -> dict[str, int]:
    try:
        tgs = json.loads(ingress["metadata"]["annotations"][ACTION])["forwardConfig"]["targetGroups"]
        return {t["serviceName"]: int(t["weight"]) for t in tgs}
    except KeyError, TypeError, ValueError:
        return {"worker": 100, "worker-canary": 0}


def split_weights(stable_replicas: int, canary_replicas: int) -> tuple[int, int]:
    """Canary share proportional to replicas (as the GCP Service selecting both tracks gives it), capped at 50%."""
    if canary_replicas <= 0:
        return 100, 0
    c = min(50, round(100 * canary_replicas / (stable_replicas + canary_replicas)))
    return 100 - c, c


def conditions(group, zone) -> str:
    """Value of the `conditions.worker` annotation: both routing headers must match (CONTRACTS §11)."""
    return json.dumps(
        [
            {"field": "http-header", "httpHeaderConfig": {"httpHeaderName": h, "values": [v]}}
            for h, v in (("ramen-group", group), ("ramen-zone", zone))
        ]
    )


def ingress(group, zone, alb_group="ramen", stable=100, canary=0) -> dict:
    ns = ns_name(group, zone)
    ann = {
        "alb.ingress.kubernetes.io/group.name": alb_group,
        "alb.ingress.kubernetes.io/scheme": "internet-facing",
        "alb.ingress.kubernetes.io/target-type": "ip",
        "alb.ingress.kubernetes.io/listen-ports": '[{"HTTPS":443}]',
        "alb.ingress.kubernetes.io/backend-protocol": "HTTP",
        "alb.ingress.kubernetes.io/backend-protocol-version": "GRPC",
        "alb.ingress.kubernetes.io/healthcheck-protocol": "HTTP",
        "alb.ingress.kubernetes.io/healthcheck-path": "/grpc.health.v1.Health/Check",
        "alb.ingress.kubernetes.io/success-codes": "0",
        "alb.ingress.kubernetes.io/group.order": "10",
        CONDITIONS: conditions(group, zone),
        ACTION: weights(stable, canary),
    }
    return {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {
            "name": "worker",
            "namespace": ns,
            "labels": {"ramen.io/group": group, "ramen.io/zone": zone},
            "annotations": ann,
        },
        "spec": {
            "ingressClassName": "alb",
            "rules": [
                {
                    "http": {
                        "paths": [
                            {
                                "path": path,
                                "pathType": "Prefix",
                                "backend": {"service": {"name": "worker", "port": {"name": "use-annotation"}}},
                            }
                            for path in LB_PATHS  # Mcp, Health, reflection; Admin stays internal (CONTRACTS §11)
                        ]
                    }
                }
            ],
        },
    }


def _service(ns, name, track, labels) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns, "labels": labels},
        "spec": {
            "type": "ClusterIP",
            "selector": {"app": "worker", "ramen.io/track": track},
            "ports": [{"name": PORT_NAME, "port": PORT, "targetPort": PORT_NAME, "appProtocol": "kubernetes.io/h2c"}],
        },
    }


def manifests(
    group, zone, spec, image, bucket_uri, role_arn=None, alb_group="ramen", stable=100, canary=0
) -> list[dict]:
    ns = ns_name(group, zone)
    labels = {"ramen.io/group": group, "ramen.io/zone": zone}
    count = int(spec.get("count", 1))
    ksa = {
        "apiVersion": "v1",
        "kind": "ServiceAccount",
        "metadata": {"name": "worker", "namespace": ns, "labels": labels},
    }
    if role_arn:
        ksa["metadata"]["annotations"] = {ROLE_ANNOTATION: role_arn}
    deps = [
        _deployment(ns, name, track, group, zone, spec, image, bucket_uri)
        for name, track in (("worker", "stable"), ("worker-canary", "canary"))
    ]
    return [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": ns, "labels": labels}},
        networkpolicy(ns, group, zone, [os.environ.get("RAMEN_AWS_VPC_CIDR", "10.0.0.0/16")]),
        ksa,
        _service(ns, "worker", "stable", labels),
        _service(ns, "worker-canary", "canary", labels),
        *deps,
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
        ingress(group, zone, alb_group, stable, canary),
    ]


def helm_manifests(
    chart, group, zone, spec, image, bucket_uri, role_arn=None, alb_group="ramen", region="", stable=100, canary=0
) -> list[dict]:
    """Render deploy/helm/ramen-worker with provider=aws when RAMEN_WORKER_CHART points at it.

    Values mirror manifests()."""
    ns = ns_name(group, zone)
    count = int(spec.get("count", 1))
    values = {
        "provider": "aws",
        "group": group,
        "zone": zone,
        "aws.zone": spec.get("region", ""),
        "aws.region": region,
        "aws.albGroup": alb_group,
        "aws.roleArn": role_arn or "",
        "image": image,
        "bucketUri": bucket_uri,
        "size": normalize_size(spec.get("size")) or "s",
        "replicas": count,
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
        if d["kind"] == "Ingress":
            ann = d["metadata"].setdefault("annotations", {})
            ann[ACTION], ann[CONDITIONS] = weights(stable, canary), conditions(group, zone)
    if not any(d["kind"] == "Ingress" for d in docs):
        docs.append(ingress(group, zone, alb_group, stable, canary))
    return copy.deepcopy(docs)
