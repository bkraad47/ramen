"""AWS cloud adapter (CONTRACTS §8, UNTESTED on a real account): EKS namespaces per zone, S3 group prefix, CloudWatch
Logs Insights, ALB weighted target groups, WAFv2 allow-lists, IAM roles for service accounts (IRSA).
The Kubernetes-side flow (canary deploy, workers, abort) is the GCP one, reused verbatim."""

import asyncio
import functools
import os
from typing import Any

from .. import __version__
from ..errors import ApiError
from ..policy import permissions as perm
from ..util import now
from . import aws_api
from .aws_api import AwsClients
from .aws_k8s import (
    ACTION,
    ROLE_ANNOTATION,
    AwsKube,
    helm_available,
    helm_manifests,
    manifests,
    parse_weights,
    split_weights,
    weights,
)
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
            raise ApiError(502, f"AWS {fn.__name__}: {type(e).__name__}: {str(e)[:300]}") from e

    return wrapper


class AwsCloud(GcpCloud):
    def __init__(
        self,
        region="us-east-1",
        bucket="",
        image="",
        admin_key="",
        cluster="ramen",
        alb_group="ramen",
        clients=None,
        rpc=None,
        timeout=10.0,
        wait_secs=300,
        poll=2.0,
        chart=None,
        log_group=None,
        boundary=None,
    ):
        clients = clients or AwsClients(region)
        super().__init__("", region, bucket, image, admin_key, clients, rpc, timeout, wait_secs, poll, chart)
        self.kube = AwsKube(clients, poll=poll, wait_secs=wait_secs)
        self.cluster, self.alb_group, self.boundary = cluster, alb_group, boundary
        self.log_group = log_group or aws_api.log_group_name(cluster)

    @classmethod
    def from_env(cls):
        region = os.environ.get("RAMEN_AWS_REGION") or os.environ.get("AWS_REGION") or "us-east-1"
        return cls(
            region,
            os.environ.get("RAMEN_GROUPS_BUCKET", ""),
            os.environ.get("RAMEN_IMAGE_WORKER", f"ramen-worker:{__version__}"),
            os.environ.get("RAMEN_ADMIN_KEY", ""),
            os.environ.get("RAMEN_EKS_CLUSTER", "ramen"),
            os.environ.get("RAMEN_ALB_GROUP", "ramen"),
            timeout=float(os.environ.get("RAMEN_WORKER_DEADLINE", "10")),
            wait_secs=int(os.environ.get("RAMEN_DEPLOY_TIMEOUT_SECS", "300")),
            chart=os.environ.get("RAMEN_WORKER_CHART"),
            log_group=os.environ.get("RAMEN_LOG_GROUP"),
            boundary=os.environ.get("RAMEN_AWS_PERMISSIONS_BOUNDARY"),
        )

    # the k8s-side flow is the GCP one; re-guard so errors say "aws"
    workers = _guard(GcpCloud.workers.__wrapped__)
    abort_deploy = _guard(GcpCloud.abort_deploy.__wrapped__)
    attach_zone = _guard(GcpCloud.attach_zone.__wrapped__)
    scale = _guard(GcpCloud.scale.__wrapped__)

    def _iam(self):
        return aws_api.Iam(self.c.iam, self.c.sts, self.c.eks, self.region, self.cluster, self.boundary)

    def _bucket(self) -> str:
        if not self.bucket:
            self.bucket = f"ramen-{self._iam().account()}-groups"
        return self.bucket

    def bucket_uri(self, group) -> str:
        return f"s3://{self._bucket()}/{group}"

    # zone lifecycle -----------------------------------------------------
    def _render(self, group, zone, spec):
        ns = ns_name(group, zone)
        ksa = self.kube.read("ServiceAccount", ns, "worker") or {}
        role = (ksa.get("metadata", {}).get("annotations") or {}).get(ROLE_ANNOTATION) or spec.get("service_account")
        w = parse_weights(self.kube.read("Ingress", ns, "worker"))  # re-applying never resets the traffic split
        stable, canary = w.get("worker", 100), w.get("worker-canary", 0)
        image = spec.get("image") or self.image  # §13.3: the group's pinned image, else the release image
        if helm_available(self.chart):
            return "helm", helm_manifests(
                self.chart,
                group,
                zone,
                spec,
                image,
                self.bucket_uri(group),
                role,
                self.alb_group,
                self.region,
                stable,
                canary,
            )
        return "python", manifests(
            group, zone, spec, image, self.bucket_uri(group), role, self.alb_group, stable, canary
        )

    def _set_weights(self, ns, stable, canary) -> dict:
        if self.kube.read("Ingress", ns, "worker") is None:
            return {"worker": stable, "worker-canary": canary, "ingress": None}
        self.kube.patch("Ingress", ns, "worker", {"metadata": {"annotations": {ACTION: weights(stable, canary)}}})
        return {"worker": stable, "worker-canary": canary, "ingress": "worker"}

    # repo sync ----------------------------------------------------------
    @_guard
    async def sync_repo(self, group, repo_url, ref, token):
        return await asyncio.to_thread(aws_api.sync_repo_to_s3, self.c.s3, self._bucket(), group, repo_url, ref, token)

    # deploy -------------------------------------------------------------
    async def deploy(self, group, env, zone, canary=True, config=None, spec=None, log=None) -> dict[str, Any]:
        res = await super().deploy(group, env, zone, canary, config, spec, log)
        ns = ns_name(group, zone)
        try:
            main = await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker") or {}
            can = await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker-canary") or {}
            s, c = split_weights(
                main.get("spec", {}).get("replicas", 0), can.get("spec", {}).get("replicas", 0) if res["ok"] else 0
            )
            res["weights"] = await asyncio.to_thread(self._set_weights, ns, s, c)
            line = f"{now()} alb: traffic split stable {s}% / canary {c}%"
        except Exception as e:  # noqa: BLE001 - the rollout itself succeeded; the split is reconciled by the next rebalance
            line = f"{now()} alb: traffic split not updated: {type(e).__name__}: {e}"
        res.setdefault("log", []).append(line)
        if log:
            log(line)
        return res

    # observe ------------------------------------------------------------
    @_guard
    async def logs(self, group, zone, worker=None, tail=500):
        return await asyncio.to_thread(
            aws_api.fetch_logs, self.c.logs, self.log_group, ns_name(group, zone), worker, tail
        )

    # traffic ------------------------------------------------------------
    @_guard
    async def rebalance(self, group, zone):
        ns = ns_name(group, zone)
        ws = await self.workers(group, zone)
        loads = [w["load"] for w in ws]
        load = "high" if "high" in loads else ("low" if loads and all(x in ("low", "down") for x in loads) else "even")
        canary_ok = [w for w in ws if w["track"] == "canary" and w["load"] not in ("down", "high")]
        stable_n = len([w for w in ws if w["track"] == "stable"])
        s, c = split_weights(stable_n, len(canary_ok))  # a busy or unhealthy canary gets no traffic
        out = {
            "ok": True,
            "load": load,
            "weights": await asyncio.to_thread(self._set_weights, ns, s, c),
            "applied": False,
            "alb": None,
        }
        alb = await asyncio.to_thread(aws_api.Alb(self.c.elbv2).find, self.alb_group)
        if alb is None:
            out["note"] = (
                f"weights written to Ingress {ns}/worker but ALB group {self.alb_group} is not provisioned yet "
                "(Load Balancer Controller reconciling?)"
            )
        else:
            out.update(applied=True, alb=alb["dns"])
        hpa = await asyncio.to_thread(self.kube.read, "HorizontalPodAutoscaler", ns, "worker")
        dep = await asyncio.to_thread(self.kube.read, "Deployment", ns, "worker")
        if hpa and dep and dep["spec"].get("replicas") != hpa["spec"].get("minReplicas"):
            await asyncio.to_thread(self.kube.set_replicas, ns, "worker", hpa["spec"]["minReplicas"])
            out["scaled_to"] = hpa["spec"]["minReplicas"]
        return out

    @_guard
    async def set_ip_rules(self, group, zone, cidrs):
        ns = ns_name(group, zone)
        cidrs = list(cidrs)
        v4, v6 = [c for c in cidrs if ":" not in c], [c for c in cidrs if ":" in c]
        # Node enforcement first: it is the control that actually gates a call and it needs no cloud API.
        # The WAF rule at the edge is defence in depth and is attempted afterwards, best effort.
        await asyncio.to_thread(
            self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"}
        )
        # env comes from the Secret at pod start: roll the workers so the node enforces the new list now
        for dep in ("worker", "worker-canary"):
            d = await asyncio.to_thread(self.kube.read, "Deployment", ns, dep)
            if d and d.get("spec", {}).get("replicas", 0) > 0:
                await asyncio.to_thread(self.kube.restart, ns, dep)
                await asyncio.to_thread(self.kube.wait_ready, ns, dep)
        out = {"ok": True, "policy": f"ramen-{group}", "cidrs": cidrs, "web_acl": None, "alb": None, "attached": False}

        def write_waf(waf):
            arns = []
            if v4:
                arns.append(waf.ensure_ip_set(f"ramen-{group}", v4, "IPV4"))
            if v6:
                arns.append(waf.ensure_ip_set(f"ramen-{group}-v6", v6, "IPV6"))
            return waf.set_group_rule(self.alb_group, group, arns)

        try:
            # Built here, not above: the boto3 client is lazy, so absent credentials raise on first use
            # and must land inside the degrading path, not outside it.
            waf = aws_api.Waf(self.c.wafv2)
            acl = await asyncio.to_thread(write_waf, waf)
            alb = await asyncio.to_thread(aws_api.Alb(self.c.elbv2).find, self.alb_group)
        except Exception as e:  # noqa: BLE001 - the node already enforces; the edge rule is best effort.
            out["note"] = f"enforced at the node; web ACL not updated: {detail_of(e)}"
            return out
        out["web_acl"] = acl
        if alb is None:
            out["note"] = (
                f"rules written to Secret and web ACL but not attached: "
                f"ALB group {self.alb_group} is not provisioned yet"
            )
            return out
        try:
            await asyncio.to_thread(waf.associate, acl, alb["arn"], 3)  # ~15s; a new ALB is not associable for a while
        except Exception as e:  # noqa: BLE001 - keep attaching in the background, the Secret already protects the node
            if aws_api.aws_error_code(e) not in aws_api.RETRYABLE:
                raise
            self._background(waf.associate, acl, alb["arn"], 60)
            return {
                **out,
                "alb": alb["dns"],
                "note": "web ACL association pending: ALB not ready, retrying in background",
            }
        return {**out, "alb": alb["dns"], "attached": True}

    @_guard
    async def detach_group(self, group):
        """Destroy the group's infra: every `ramen-<group>-*` namespace and IAM role, its WAF IP sets (F4.1)."""
        removed = {"namespaces": [], "service_accounts": []}
        for ns in await asyncio.to_thread(self.kube.list_namespaces, group):
            await asyncio.to_thread(self.kube.delete_namespace, ns)
            removed["namespaces"].append(ns)
        iam = self._iam()
        for r in await asyncio.to_thread(iam.list_roles, f"ramen-{group}-"):
            await asyncio.to_thread(iam.delete_role, r["RoleName"])
            removed["service_accounts"].append(r["Arn"])
        waf = aws_api.Waf(self.c.wafv2)
        await asyncio.to_thread(waf.set_group_rule, self.alb_group, group, [])
        for name in (f"ramen-{group}", f"ramen-{group}-v6"):
            await asyncio.to_thread(waf.delete_ip_set, name)
        return removed

    # identity -----------------------------------------------------------
    def _ksa_gsa(self, group, zone) -> str | None:
        ksa = self.kube.read("ServiceAccount", ns_name(group, zone), "worker") or {}
        return (ksa.get("metadata", {}).get("annotations") or {}).get(ROLE_ANNOTATION)

    def _identity(self, group, zone) -> dict:
        """IAM role ramen-<group>-<zone> (path /ramen/, IRSA trust for KSA <ns>/worker) with the baseline worker policy
        (group prefix in the bucket, group secrets). Idempotent. Created on attach when the KSA has no role."""
        ns, iam = ns_name(group, zone), self._iam()
        name = iam.role_name(group, zone)
        arn, created = iam.ensure_role(name, ns, "worker", group, zone)
        policy = iam.put_worker_policy(name, self._bucket(), group)
        return {
            "name": arn,
            "created": created,
            "roles": [policy],
            "ksa": f"{ns}/worker",
            "workload_identity": f"system:serviceaccount:{ns}:worker@{iam.issuer()}",
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
                    "metadata": {"name": "worker", "namespace": ns, "annotations": {ROLE_ANNOTATION: out["name"]}},
                }
            )
            return out

        return await asyncio.to_thread(run)

    @_guard
    async def apply_sa_permissions(self, group, zone, permissions, scopes=None, previous=None):
        """Put the mapped IAM actions as inline policy `ramen-sa-permissions` on the zone role (created if missing, §9).
        Unscoped, s3/secretsmanager actions stay on the group's prefix / secrets path; a scope names the buckets /
        secrets instead (0.5.93). The rest are unconditional. The policy is replaced whole, so nothing lingers."""
        ns, actions = ns_name(group, zone), perm.mapped(permissions, "aws")
        sa = await self.create_service_account(group, zone)
        applied = await asyncio.to_thread(
            self._iam().put_sa_permissions,
            aws_api.Iam.role_name(group, zone),
            self._bucket(),
            group,
            actions,
            perm.role_scopes(permissions, scopes, "aws"),
        )
        out = {
            "ok": True,
            "service_account": sa["name"],
            "applied": applied,
            "permissions": list(permissions),
            "scopes": dict(scopes or {}),
            "ksa": f"{ns}/worker",
            "policy": aws_api.Iam.SA_POLICY if applied else None,
        }
        if unscoped := perm.unscoped(permissions, scopes):
            out["unscoped"] = unscoped
        return out

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
                        "service_account": (ksa.get("metadata", {}).get("annotations") or {}).get(ROLE_ANNOTATION),
                        "weights": parse_weights(self.kube.read("Ingress", ns, "worker")),
                    }
                )
            return {
                "groups": sorted({z["group"] for z in zones if z.get("group")}),
                "zones": zones,
                "service_accounts": [r["Arn"] for r in self._iam().list_roles()],
                "at": now(),
            }

        return await asyncio.to_thread(run)
