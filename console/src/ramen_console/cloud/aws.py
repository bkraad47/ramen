"""AWS cloud adapter (CONTRACTS §8, UNTESTED on a real account): EKS namespaces per zone, S3 group prefix, CloudWatch
Logs Insights, ALB weighted target groups, WAFv2 allow-lists, IAM roles for service accounts (IRSA).
The Kubernetes-side flow (canary deploy, workers, abort) is the GCP one, reused verbatim."""

import asyncio
import functools
import logging
import os
from typing import Any

from .. import __version__, imagebuild
from ..errors import ApiError
from ..policy import permissions as perm
from ..util import now
from . import aws_api
from .aws_api import AwsClients
from .aws_k8s import (
    ACTION,
    INGRESSES,
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
from .gcp_k8s import ACTIVATOR, BUILD_NS, ns_name


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


def _waf_halves(cidr: str) -> list[str]:
    """A match-all CIDR as the two halves WAF accepts (0.0.0.0/0 → 0.0.0.0/1 + 128.0.0.0/1); anything else as is."""
    if cidr.strip() in ("0.0.0.0/0", "::/0"):
        return ["0.0.0.0/1", "128.0.0.0/1"] if "." in cidr else ["::/1", "8000::/1"]
    return [cidr]


class AwsCloud(GcpCloud):
    PROVIDER = "AWS"

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
        node_wait_secs=None,
        chart=None,
        log_group=None,
        boundary=None,
        build_boundary=None,
    ):
        clients = clients or AwsClients(region)
        super().__init__("", region, bucket, image, admin_key, clients, rpc, timeout, wait_secs, poll, chart)
        self.kube = AwsKube(clients, poll=poll, wait_secs=wait_secs, node_wait_secs=node_wait_secs)
        self.cluster, self.alb_group, self.boundary = cluster, alb_group, boundary
        self.build_boundary = build_boundary
        # the controller reconciles an Ingress change into the ALB rule within seconds (7 s measured on EKS)
        self.split_drain_secs = float(os.environ.get("RAMEN_ALB_SPLIT_DRAIN_SECS", "15"))
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
            node_wait_secs=int(os.environ.get("RAMEN_NODE_WAIT_SECS", "1200")),
            chart=os.environ.get("RAMEN_WORKER_CHART"),
            log_group=os.environ.get("RAMEN_LOG_GROUP"),
            boundary=os.environ.get("RAMEN_AWS_PERMISSIONS_BOUNDARY"),
            build_boundary=os.environ.get("RAMEN_AWS_BUILD_BOUNDARY"),
        )

    # the k8s-side flow is the GCP one; re-guard so errors say "aws"
    workers = _guard(GcpCloud.workers.__wrapped__)
    abort_deploy = _guard(GcpCloud.abort_deploy.__wrapped__)
    attach_zone = _guard(GcpCloud.attach_zone.__wrapped__)
    scale = _guard(GcpCloud.scale.__wrapped__)
    zone_runtime = _guard(GcpCloud.zone_runtime.__wrapped__)
    sleep_zone = _guard(GcpCloud.sleep_zone.__wrapped__)
    wake_zone = _guard(GcpCloud.wake_zone.__wrapped__)

    def _after_wake(self, ns, count, canary) -> None:
        """The split `sleep_zone` took off the canary (via `_canary_out`) comes back with the woken replicas."""
        self._set_weights(ns, *split_weights(count, canary))

    def _build_identity(self, group, repository) -> dict[str, str]:
        """§38.2: ECR repository `ramen/groups/<group>/worker` (tagged with the group) and IAM role
        `/ramen/build/ramen-build-<group>` (IRSA for `ramen-system/ramen-build-<group>`, build boundary) that may push
        to that repository only and pull the base image when it lives in this registry."""
        iam = self._iam()
        name = repository.split("/", 1)[1]
        repo_arn, _ = aws_api.Ecr(self.c.ecr).ensure(name, {"ramen": "group-worker", "group": group})
        role = iam.build_role_name(group)
        arn = iam.ensure_build_role(role, BUILD_NS, self.build_ksa(group), group, name.split("/")[-2])
        base = aws_api.ecr_repo_arn(imagebuild.base_image(self), self.region, iam.account())
        iam.put_build_policy(role, repo_arn, [base] if base else [])
        return {ROLE_ANNOTATION: arn}

    def _drop_build_identity(self, group) -> dict[str, list[str]]:
        iam, out = self._iam(), {"repositories": [], "service_accounts": []}
        repository = self.build_repository(group)
        if repository and aws_api.Ecr(self.c.ecr).delete(repository.split("/", 1)[1]):
            out["repositories"].append(repository)
        role = iam.build_role_name(group)
        if any(r["RoleName"] == role for r in iam.list_roles(role)):
            iam.delete_role(role)
            out["service_accounts"].append(role)
        return out

    def _build_extras(self, registry) -> dict:
        """kaniko's bundled `ecr-login` helper signs in with the pod's IRSA web identity (`ramen-build-<group>`)."""
        host = registry.split("/")[0]
        return {
            "docker_config": {"credHelpers": {host: "ecr-login"}},
            "env": {"AWS_REGION": self.region, "AWS_SDK_LOAD_CONFIG": "true"},
        }

    # clusters (§28.2): a satellite is an EKS cluster in another region; its boto3 clients are that region's
    def _satellite_clients(self, entry: dict):
        return self.c.for_cluster(entry["region"], entry["name"])

    def _cluster_name(self) -> str:
        c = self.current_cluster
        return self.cluster if c == "home" else c["name"]

    def _cluster_region(self) -> str:
        c = self.current_cluster
        return self.region if c == "home" else c["region"]

    def _iam(self):
        # the zone role's IRSA trust names the OIDC provider of the cluster the zone runs on (the satellite's for a
        # satellite zone); secrets and the bucket stay in the home region, so the policy ARNs keep `self.region`
        return aws_api.Iam(
            self.c.iam, self.c.sts, self.cc.eks, self.region, self._cluster_name(), self.boundary, self.build_boundary
        )

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

    async def _canary_out(self, ns, note) -> None:
        """Take the canary out of the ALB split before its pods change, then give the ALB time to apply it: an empty
        target group with weight answers 503 (0.6.1 EKS run). `deploy` sets the split again at the end."""
        if parse_weights(await asyncio.to_thread(self.kube.read, "Ingress", ns, "worker")).get("worker-canary", 0) <= 0:
            return
        await asyncio.to_thread(self._set_weights, ns, 100, 0)
        note("alb: canary taken out of the traffic split")
        await asyncio.sleep(self.split_drain_secs)

    def _set_weights(self, ns, stable, canary) -> dict:
        if self.kube.read("Ingress", ns, "worker") is None:
            return {"worker": stable, "worker-canary": canary, "ingress": None}
        for name in INGRESSES:  # gRPC and Streamable HTTP share the split
            if name == "worker" or self.kube.read("Ingress", ns, name) is not None:
                self.kube.patch("Ingress", ns, name, {"metadata": {"annotations": {ACTION: weights(stable, canary)}}})
        return {"worker": stable, "worker-canary": canary, "ingress": "worker"}

    # repo sync ----------------------------------------------------------
    def home_region(self) -> str | None:
        return self.region

    @_guard
    async def sync_repo(self, group, repo_url, ref, token):
        return await asyncio.to_thread(aws_api.sync_repo_to_s3, self.c.s3, self._bucket(), group, repo_url, ref, token)

    # deploy -------------------------------------------------------------
    @_guard
    async def read_file(self, group, path):
        try:
            obj = await asyncio.to_thread(self.c.s3.get_object, Bucket=self._bucket(), Key=f"{group}/{path}")
        except self.c.s3.exceptions.NoSuchKey:
            return None
        return obj["Body"].read()

    @_guard
    async def list_files(self, group, prefix):
        def run():
            pages = self.c.s3.get_paginator("list_objects_v2").paginate(
                Bucket=self._bucket(), Prefix=f"{group}/{prefix}"
            )
            return sorted(o["Key"][len(group) + 1 :] for page in pages for o in page.get("Contents") or [])

        return await asyncio.to_thread(run)

    @_guard
    async def delete_files(self, group):
        def run():
            keys = sorted(aws_api.list_prefix(self.c.s3, self._bucket(), f"{group}/"))
            for i in range(0, len(keys), 1000):  # delete_objects takes at most 1000 keys
                batch = [{"Key": k} for k in keys[i : i + 1000]]
                self.c.s3.delete_objects(Bucket=self._bucket(), Delete={"Objects": batch, "Quiet": True})
            return len(keys)

        return await asyncio.to_thread(run)

    async def deploy(
        self, group, env, zone, canary=True, config=None, spec=None, log=None, gate=None
    ) -> dict[str, Any]:
        res = await super().deploy(group, env, zone, canary, config, spec, log, gate)
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

    async def push_config(self, group, env, zone, config) -> dict[str, Any]:
        """§26.3 / P4-B1: the canary rolls in place, so `_canary_out` takes it out of the ALB split first; its share
        comes back in the background once it is Ready again (a canary that had no share keeps none)."""
        ns = ns_name(group, zone)
        before = parse_weights(await asyncio.to_thread(self.kube.read, "Ingress", ns, "worker"))
        res = await super().push_config(group, env, zone, config)
        if "worker-canary" in res["restarted"] and before["worker-canary"] > 0:
            self._background(self._restore_split, ns, before["worker"], before["worker-canary"])
        return res

    def _restore_split(self, ns, stable, canary) -> None:
        try:
            self.kube.wait_ready(ns, "worker-canary")
            self._set_weights(ns, stable, canary)
        except Exception as e:  # noqa: BLE001 - the canary stays out of the split: the stable track serves everything
            logging.getLogger("ramen.cloud").warning("%s: canary share not restored after a push: %s", ns, str(e)[:300])

    # observe ------------------------------------------------------------
    @_guard
    async def logs(self, group, zone, worker=None, tail=500):
        await self._select(zone)  # a satellite zone's logs sit in its region's `/aws/containerinsights/<cluster>/…`
        group_name = self.log_group if self.current_cluster == "home" else aws_api.log_group_name(self._cluster_name())
        return await asyncio.to_thread(aws_api.fetch_logs, self.cc.logs, group_name, ns_name(group, zone), worker, tail)

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
        alb = await asyncio.to_thread(aws_api.Alb(self.cc.elbv2).find, self.alb_group)  # the zone's region (§28.2)
        if alb is None:
            out["note"] = (
                f"weights written to Ingress {ns}/worker but ALB group {self.alb_group} is not provisioned yet "
                "(Load Balancer Controller reconciling?)"
            )
        else:
            out.update(applied=True, alb=alb["dns"])
        return out

    @_guard
    async def set_ip_rules(self, group, zone, cidrs):
        ns = ns_name(group, zone)
        await self._select(zone)  # §28.2: the WAF web ACL and the ALB are the zone's region's
        cidrs = list(cidrs)
        edge = [h for c in cidrs for h in _waf_halves(c)]  # WAF IP sets take /1-/32 and /1-/128, never /0
        v4, v6 = [c for c in edge if ":" not in c], [c for c in edge if ":" in c]
        # Node enforcement first: it is the control that actually gates a call and it needs no cloud API.
        # The WAF rule at the edge is defence in depth and is attempted afterwards, best effort.
        await asyncio.to_thread(
            self.kube.merge_secret, ns, "ramen-deploy", {"RAMEN_ALLOWED_CIDRS": ",".join(cidrs) or "0.0.0.0/0"}
        )
        # env comes from the Secret at pod start: roll the workers so the node enforces the new list now
        for dep in ("worker", "worker-canary", ACTIVATOR):  # a sleeping zone's activator enforces the list too
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
            waf = aws_api.Waf(self.cc.wafv2)
            acl = await asyncio.to_thread(write_waf, waf)
            alb = await asyncio.to_thread(aws_api.Alb(self.cc.elbv2).find, self.alb_group)
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
    async def detach_zone(self, group, zone):
        """Destroy one group's deployment in one zone: its namespace and the zone IAM role (0.6.0)."""
        await self._select(zone)
        ns, iam = ns_name(group, zone), self._iam()
        await asyncio.to_thread(self.kube.delete_namespace, ns)
        role = aws_api.Iam.role_name(group, zone)
        removed = {"namespaces": [ns], "service_accounts": []}
        if any(r["RoleName"] == role for r in await asyncio.to_thread(iam.list_roles, role)):
            await asyncio.to_thread(iam.delete_role, role)
            removed["service_accounts"].append(role)
        return removed

    async def detach_group(self, group):
        """Destroy the group's infra: every `ramen-<group>-*` namespace and IAM role, its WAF IP sets (F4.1)."""
        removed = {"namespaces": [], "service_accounts": []}
        clusters = await self._all_clusters()
        for cluster in clusters:  # §28.2: the group's namespaces and WAF objects on every cluster / in every region
            kube = self._use(cluster)
            for ns in await asyncio.to_thread(kube.list_namespaces, group):
                await asyncio.to_thread(kube.delete_namespace, ns)
                removed["namespaces"].append(ns)
        self._use("home")
        iam = self._iam()
        for r in await asyncio.to_thread(iam.list_roles, f"ramen-{group}-"):
            if r.get("Path", aws_api.ROLE_PATH) != aws_api.ROLE_PATH:
                continue  # §38.2: a build role (/ramen/build/) is not a zone role, whatever its name
            await asyncio.to_thread(iam.delete_role, r["RoleName"])
            removed["service_accounts"].append(r["Arn"])
        for cluster in clusters:
            self._use(cluster)
            waf = aws_api.Waf(self.cc.wafv2)
            await asyncio.to_thread(waf.set_group_rule, self.alb_group, group, [])
            for name in (f"ramen-{group}", f"ramen-{group}-v6"):
                await asyncio.to_thread(waf.delete_ip_set, name)
        self._use("home")
        await self._drop_builder(group, removed)
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
        await self._select(zone)

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

    SA_ANNOTATION = ROLE_ANNOTATION

    def _survey_extra(self, kube, ns) -> dict:
        return {"weights": parse_weights(kube.read("Ingress", ns, "worker"))}

    def _accounts(self) -> list:
        return [r["Arn"] for r in self._iam().list_roles() if r.get("Path", aws_api.ROLE_PATH) == aws_api.ROLE_PATH]

    refresh = _guard(GcpCloud.refresh.__wrapped__)
