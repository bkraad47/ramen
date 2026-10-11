"""AKS manifests (§21.6, D54): the GKE set (Gateway API `HTTPRoute`s served here by Envoy Gateway) minus the GKE-only
parts, plus Azure workload identity and the per-zone Envoy `SecurityPolicy` that mirrors the node's IP rules."""

from .gcp_clients import http_status
from .gcp_k8s import GW_GROUP, Kube, manifests, networkpolicy, ns_name

ENVOY_NAMESPACE = "envoy-gateway-system"  # where Envoy Gateway runs its proxies; the only edge peer of a worker
WI_CLIENT_ID = "azure.workload.identity/client-id"
WI_USE = "azure.workload.identity/use"
EG_GROUP, EG_VERSION, SP_PLURAL = "gateway.envoyproxy.io", "v1alpha1", "securitypolicies"


def zonal() -> bool:
    """`RAMEN_AZURE_ZONAL=0` when the AKS node pool has no availability zones (many subscriptions get
    `AvailabilityZoneNotSupported`): workers are then not pinned to `topology.kubernetes.io/zone`."""
    import os

    return os.environ.get("RAMEN_AZURE_ZONAL", "1") != "0"


def azure_manifests(group, zone, spec, image, bucket_uri, client_id=None) -> list[dict]:
    """GKE manifests adapted: no HealthCheckPolicy or NEG annotation (Envoy health-checks through the readiness probe),
    the zone KSA annotated with the managed identity's client id and pods labelled for the workload-identity webhook,
    `RAMEN_TRUST_PROXY_HOPS=1` (Envoy appends the client address the Azure load balancer preserves), and a NetworkPolicy
    that admits the console and the Envoy proxies only."""
    ns = ns_name(group, zone)
    docs = []
    for d in manifests(group, zone, spec, image, bucket_uri, None):
        kind = d["kind"]
        if kind == "HealthCheckPolicy":
            continue
        if kind == "Service":
            d["metadata"].pop("annotations", None)
        elif kind == "ServiceAccount" and client_id:
            d["metadata"]["annotations"] = {WI_CLIENT_ID: client_id}
        elif kind == "Deployment":
            tpl = d["spec"]["template"]
            tpl["metadata"]["labels"][WI_USE] = "true"
            if not zonal():  # a cluster without availability zones: the Ramen zone is a logical placement
                # an explicit null, not a missing key: `Kube.apply` merge-patches an existing Deployment, and only a
                # null removes a nodeSelector an earlier (zonal) deploy left behind
                tpl["spec"]["nodeSelector"] = None
            for e in tpl["spec"]["containers"][0]["env"]:
                if e["name"] == "RAMEN_TRUST_PROXY_HOPS":
                    e["value"] = "1"
        elif kind == "NetworkPolicy":
            d = networkpolicy(ns, group, zone, lb_cidrs=[])
            peer = {"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": ENVOY_NAMESPACE}}}
            d["spec"]["ingress"][0]["from"].append(peer)
        docs.append(d)
    return docs


def securitypolicy(group, zone, cidrs: list[str]) -> dict:
    """Envoy Gateway `SecurityPolicy` on the zone's HTTPRoute: deny unless the client address is in `cidrs`."""
    return {
        "apiVersion": f"{EG_GROUP}/{EG_VERSION}",
        "kind": "SecurityPolicy",
        "metadata": {
            "name": "worker",
            "namespace": ns_name(group, zone),
            "labels": {"ramen.io/group": group, "ramen.io/zone": zone},
        },
        "spec": {
            "targetRefs": [{"group": GW_GROUP, "kind": "HTTPRoute", "name": "worker"}],
            "authorization": {
                "defaultAction": "Deny",
                "rules": [{"name": "ramen-ip-rules", "action": "Allow", "principal": {"clientCIDRs": list(cidrs)}}],
            },
        },
    }


class AzureKube(Kube):
    def _fns(self, kind):
        if kind == "SecurityPolicy":
            return self._custom(EG_GROUP, EG_VERSION, SP_PLURAL)
        return super()._fns(kind)

    def delete_security_policy(self, ns: str) -> bool:
        try:
            self.c.custom.delete_namespaced_custom_object(EG_GROUP, EG_VERSION, ns, SP_PLURAL, "worker")
        except Exception as e:  # noqa: BLE001
            if http_status(e) != 404:
                raise
            return False
        return True
