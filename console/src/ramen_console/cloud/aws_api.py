"""boto3 clients + S3 repo sync, CloudWatch Logs Insights, ALB/WAFv2 and IAM (IRSA) helpers (CONTRACTS §8, UNTESTED on
a real account: everything below is exercised against moto/fakes only). Sync functions, run in threads."""
import hashlib
import json
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from ..errors import ApiError
from .gcp_clients import GcpClients
from .local import LocalCloud

ROLE_PATH = "/ramen/"
WAF_SCOPE = "REGIONAL"
STACK_TAG = "ingress.k8s.aws/stack"  # the AWS Load Balancer Controller tags an IngressGroup's ALB with the group name
RETRYABLE = ("WAFOptimisticLockException", "WAFUnavailableEntityException", "Throttling", "ThrottlingException")


def aws_error_code(e: BaseException) -> str | None:
    r = getattr(e, "response", None)
    return (r.get("Error") or {}).get("Code") if isinstance(r, dict) else None


class AwsClients(GcpClients):
    """kubernetes (in-cluster, fallback KUBECONFIG; inherited) + boto3 clients. boto3 clients are thread-safe once
    built, building them from the default session is not: creation is serialized and cached. Adaptive retries absorb
    API throttling (§8 lessons)."""

    def __init__(self, region: str):
        super().__init__(project="")
        self.region = region
        self._lock = threading.Lock()

    def _cached(self, key, make):
        with self._lock:
            if key not in self._c:
                self._c[key] = make()
            return self._c[key]

    @property
    def networking(self):
        return self._cached("networking", lambda: self._kube().NetworkingV1Api())

    def _boto(self, service):
        def make():
            import boto3
            from botocore.config import Config
            return boto3.client(service, region_name=self.region, config=Config(retries={"mode": "adaptive", "max_attempts": 8}))
        return self._cached(f"boto:{service}", make)

    @property
    def s3(self):
        return self._boto("s3")

    @property
    def secretsmanager(self):
        return self._boto("secretsmanager")

    @property
    def logs(self):
        return self._boto("logs")

    @property
    def wafv2(self):
        return self._boto("wafv2")

    @property
    def elbv2(self):
        return self._boto("elbv2")

    @property
    def iam(self):
        return self._boto("iam")

    @property
    def sts(self):
        return self._boto("sts")

    @property
    def eks(self):
        return self._boto("eks")


def _retry(fn, attempts=6, delay=2.0):
    """WAF/ELB reject edits while a previous change propagates (lock tokens, 'unavailable entity'): retry, then raise."""
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            if aws_error_code(e) not in RETRYABLE or i == attempts - 1:
                raise
            time.sleep(delay)


# S3 -------------------------------------------------------------------------
def list_prefix(s3, bucket, prefix) -> dict[str, str]:
    """{key: md5 hex} for every object under prefix (multipart ETags are not an md5 → '' so they always re-upload)."""
    out = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            etag = o.get("ETag", "").strip('"')
            out[o["Key"]] = "" if "-" in etag else etag
    return out


def sync_repo_to_s3(s3, bucket, group, repo_url, ref, token) -> str:
    url = LocalCloud.auth_url(repo_url, token)
    with tempfile.TemporaryDirectory() as tmp:
        r = subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", ref, url, tmp], capture_output=True, text=True)
        if r.returncode != 0:
            raise ApiError(502, "git failed: " + (r.stderr.replace(token, "***") if token else r.stderr).strip()[:500])
        root, prefix = Path(tmp), f"{group}/"
        local = {prefix + p.relative_to(root).as_posix(): p for p in root.rglob("*")
                 if p.is_file() and ".git" not in p.relative_to(root).parts}
        remote = list_prefix(s3, bucket, prefix)
        for key, path in local.items():
            if remote.get(key) != hashlib.md5(path.read_bytes()).hexdigest():
                s3.upload_file(str(path), bucket, key)
        stale = [k for k in remote if k not in local]
        for i in range(0, len(stale), 1000):
            s3.delete_objects(Bucket=bucket, Delete={"Objects": [{"Key": k} for k in stale[i:i + 1000]], "Quiet": True})
    return f"s3://{bucket}/{group}"


# CloudWatch Logs Insights ----------------------------------------------------
def log_group_name(cluster) -> str:
    return f"/aws/containerinsights/{cluster}/application"


def fetch_logs(logs, log_group, ns, worker, tail, poll=1.0, timeout=60.0, days=7) -> str:
    q = f'fields @timestamp, kubernetes.pod_name, stream, log | filter kubernetes.namespace_name = "{ns}"'
    if worker:
        q += f' and kubernetes.pod_name = "{worker}"'
    q += f" | sort @timestamp desc | limit {max(1, min(int(tail), 10000))}"
    now = int(time.time())
    try:
        qid = logs.start_query(logGroupName=log_group, startTime=now - days * 86400, endTime=now, queryString=q)["queryId"]
    except Exception as e:  # noqa: BLE001
        if aws_error_code(e) == "ResourceNotFoundException":
            return f"# log group {log_group} not found: Fluent Bit / Container Insights not installed yet (deploy/terraform/aws installs it)\n"
        raise
    deadline = time.monotonic() + timeout
    while True:
        r = logs.get_query_results(queryId=qid)
        if r.get("status") in ("Complete", "Failed", "Cancelled", "Timeout"):
            break
        if time.monotonic() > deadline:
            logs.stop_query(queryId=qid)
            raise ApiError(504, f"logs insights query still {r.get('status')} after {timeout:.0f}s")
        time.sleep(poll)
    if r["status"] != "Complete":
        raise ApiError(502, f"logs insights query {r['status']}")
    lines = []
    for row in reversed(r.get("results", [])):
        f = {c["field"]: c["value"] for c in row}
        lines.append(f"{f.get('@timestamp', '-')} {f.get('stream', '-')} {f.get('kubernetes.pod_name', '-')} {f.get('log') or f.get('@message', '')}")
    return "\n".join(lines) + ("\n" if lines else "")


# ALB (ingress group) -----------------------------------------------------------
class Alb:
    def __init__(self, elbv2):
        self.api = elbv2

    def find(self, group_name) -> dict | None:
        """The ALB the Load Balancer Controller created for IngressGroup `group_name` (tag ingress.k8s.aws/stack)."""
        arns = {}
        for page in self.api.get_paginator("describe_load_balancers").paginate():
            for lb in page.get("LoadBalancers", []):
                arns[lb["LoadBalancerArn"]] = lb
        keys = list(arns)
        for i in range(0, len(keys), 20):
            for td in self.api.describe_tags(ResourceArns=keys[i:i + 20]).get("TagDescriptions", []):
                if any(t["Key"] == STACK_TAG and t["Value"] == group_name for t in td.get("Tags", [])):
                    lb = arns[td["ResourceArn"]]
                    return {"arn": lb["LoadBalancerArn"], "dns": lb.get("DNSName"), "name": lb.get("LoadBalancerName")}
        return None


# WAFv2 ------------------------------------------------------------------------
class Waf:
    """One web ACL `ramen` (default allow: the console path stays open) with one rule per group that blocks
    `/mcp/<group>/` unless the source IP is in IPSet `ramen-<group>` (v4) / `ramen-<group>-v6`."""

    def __init__(self, wafv2):
        self.api = wafv2

    def _find(self, method, key, name) -> dict | None:
        marker = None
        while True:
            kw = {"Scope": WAF_SCOPE, "Limit": 100}
            if marker:
                kw["NextMarker"] = marker
            r = getattr(self.api, method)(**kw)
            for s in r.get(key, []):
                if s["Name"] == name:
                    return s
            marker = r.get("NextMarker")
            if not marker:
                return None

    def ensure_ip_set(self, name, cidrs: list[str], version="IPV4") -> str:
        s = self._find("list_ip_sets", "IPSets", name)
        if s is None:
            r = _retry(lambda: self.api.create_ip_set(Name=name, Scope=WAF_SCOPE, IPAddressVersion=version, Addresses=cidrs,
                                                      Description="ramen ip-rules", Tags=[{"Key": "ramen", "Value": "ip-rules"}]))
            return r["Summary"]["ARN"]

        def update():
            g = self.api.get_ip_set(Name=name, Scope=WAF_SCOPE, Id=s["Id"])
            self.api.update_ip_set(Name=name, Scope=WAF_SCOPE, Id=s["Id"], Addresses=cidrs, LockToken=g["LockToken"])
        _retry(update)
        return s["ARN"]

    def delete_ip_set(self, name) -> bool:
        s = self._find("list_ip_sets", "IPSets", name)
        if s is None:
            return False
        _retry(lambda: self.api.delete_ip_set(Name=name, Scope=WAF_SCOPE, Id=s["Id"],
                                              LockToken=self.api.get_ip_set(Name=name, Scope=WAF_SCOPE, Id=s["Id"])["LockToken"]))
        return True

    @staticmethod
    def group_rule(group, ipset_arns: list[str], priority) -> dict:
        refs = [{"IPSetReferenceStatement": {"ARN": a}} for a in ipset_arns]
        allowed = refs[0] if len(refs) == 1 else {"OrStatement": {"Statements": refs}}
        return {"Name": f"ramen-{group}", "Priority": priority, "Action": {"Block": {}},
                "VisibilityConfig": {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": f"ramen-{group}"},
                "Statement": {"AndStatement": {"Statements": [
                    {"ByteMatchStatement": {"FieldToMatch": {"UriPath": {}}, "PositionalConstraint": "STARTS_WITH",
                                            "SearchString": f"/mcp/{group}/".encode(), "TextTransformations": [{"Priority": 0, "Type": "NONE"}]}},
                    {"NotStatement": {"Statement": allowed}}]}}}

    def set_group_rule(self, acl_name, group, ipset_arns: list[str]) -> str:
        """Replace the group's rule in the web ACL (remove it when `ipset_arns` is empty = allow all). Returns the ACL ARN."""
        vis = {"SampledRequestsEnabled": True, "CloudWatchMetricsEnabled": True, "MetricName": acl_name}
        s = self._find("list_web_acls", "WebACLs", acl_name)
        if s is None:
            _retry(lambda: self.api.create_web_acl(Name=acl_name, Scope=WAF_SCOPE, DefaultAction={"Allow": {}}, Rules=[], VisibilityConfig=vis,
                                                   Description="ramen per-group MCP allow-lists", Tags=[{"Key": "ramen", "Value": "ip-rules"}]))
            s = self._find("list_web_acls", "WebACLs", acl_name)

        def update():
            g = self.api.get_web_acl(Name=acl_name, Scope=WAF_SCOPE, Id=s["Id"])
            rules = [r for r in g["WebACL"].get("Rules", []) if r["Name"] != f"ramen-{group}"]
            if ipset_arns:
                rules.append(self.group_rule(group, ipset_arns, 0))
            for i, r in enumerate(sorted(rules, key=lambda r: r["Name"]), start=1):
                r["Priority"] = i
            self.api.update_web_acl(Name=acl_name, Scope=WAF_SCOPE, Id=s["Id"], DefaultAction=g["WebACL"]["DefaultAction"],
                                    Rules=rules, VisibilityConfig=g["WebACL"]["VisibilityConfig"], LockToken=g["LockToken"])
        _retry(update)
        return s["ARN"]

    def associate(self, acl_arn, lb_arn, attempts=6) -> None:
        current = self.api.get_web_acl_for_resource(ResourceArn=lb_arn).get("WebACL") or {}
        if current.get("ARN") == acl_arn:
            return
        _retry(lambda: self.api.associate_web_acl(WebACLArn=acl_arn, ResourceArn=lb_arn), attempts=attempts, delay=5.0)


# IAM (IRSA) ---------------------------------------------------------------------
class Iam:
    def __init__(self, iam, sts, eks, region, cluster):
        self.iam, self.sts, self.eks, self.region, self.cluster = iam, sts, eks, region, cluster
        self._account = self._issuer = None

    def account(self) -> str:
        if self._account is None:
            self._account = self.sts.get_caller_identity()["Account"]
        return self._account

    def issuer(self) -> str:
        """OIDC issuer host/path of the EKS cluster (without https://), e.g. oidc.eks.us-east-1.amazonaws.com/id/ABC."""
        if self._issuer is None:
            url = self.eks.describe_cluster(name=self.cluster)["cluster"]["identity"]["oidc"]["issuer"]
            self._issuer = url.removeprefix("https://")
        return self._issuer

    @staticmethod
    def role_name(group, zone) -> str:
        name = f"ramen-{group}-{zone}"
        return name if len(name) <= 64 else "ramen-" + hashlib.sha1(name.encode()).hexdigest()[:24]

    def trust_policy(self, ns, ksa) -> dict:
        iss = self.issuer()
        return {"Version": "2012-10-17", "Statement": [{"Effect": "Allow", "Action": "sts:AssumeRoleWithWebIdentity",
                                                        "Principal": {"Federated": f"arn:aws:iam::{self.account()}:oidc-provider/{iss}"},
                                                        "Condition": {"StringEquals": {f"{iss}:sub": f"system:serviceaccount:{ns}:{ksa}",
                                                                                       f"{iss}:aud": "sts.amazonaws.com"}}}]}

    def worker_policy(self, bucket, group) -> dict:
        return {"Version": "2012-10-17", "Statement": [
            {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}",
             "Condition": {"StringLike": {"s3:prefix": [f"{group}/*", f"{group}/"]}}},
            {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": f"arn:aws:s3:::{bucket}/{group}/*"},
            {"Effect": "Allow", "Action": ["secretsmanager:GetSecretValue"],
             "Resource": f"arn:aws:secretsmanager:{self.region}:{self.account()}:secret:ramen/{group}/*"}]}

    def ensure_role(self, name, ns, ksa, group, zone) -> tuple[str, bool]:
        trust = json.dumps(self.trust_policy(ns, ksa))
        tags = [{"Key": "ramen", "Value": "worker"}, {"Key": "group", "Value": group}, {"Key": "zone", "Value": zone}]
        try:
            r = self.iam.create_role(RoleName=name, Path=ROLE_PATH, AssumeRolePolicyDocument=trust, Tags=tags,
                                     Description=f"ramen worker {group}/{zone}")
            return r["Role"]["Arn"], True
        except Exception as e:  # noqa: BLE001
            if aws_error_code(e) != "EntityAlreadyExists":
                raise
        self.iam.update_assume_role_policy(RoleName=name, PolicyDocument=trust)
        return self.iam.get_role(RoleName=name)["Role"]["Arn"], False

    SA_POLICY = "ramen-sa-permissions"

    def sa_permissions_policy(self, bucket, group, actions: list[str]) -> dict | None:
        """Approved-permission actions (policy/permissions.py, provider aws): s3:* scoped to the group prefix,
        secretsmanager:* to the group's secrets path (as gcp.py conditions its bindings), the rest unconditional."""
        s3 = [a for a in actions if a.startswith("s3:")]
        sm = [a for a in actions if a.startswith("secretsmanager:")]
        other = [a for a in actions if not (a.startswith("s3:") or a.startswith("secretsmanager:"))]
        stmts = []
        if "s3:ListBucket" in s3:
            stmts.append({"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": f"arn:aws:s3:::{bucket}",
                          "Condition": {"StringLike": {"s3:prefix": [f"{group}/*", f"{group}/"]}}})
        if objs := [a for a in s3 if a != "s3:ListBucket"]:
            stmts.append({"Effect": "Allow", "Action": objs, "Resource": f"arn:aws:s3:::{bucket}/{group}/*"})
        if sm:
            stmts.append({"Effect": "Allow", "Action": sm, "Resource": f"arn:aws:secretsmanager:{self.region}:{self.account()}:secret:ramen/{group}/*"})
        if other:
            stmts.append({"Effect": "Allow", "Action": other, "Resource": "*"})
        return {"Version": "2012-10-17", "Statement": stmts} if stmts else None

    def put_sa_permissions(self, name, bucket, group, actions: list[str]) -> list[str]:
        """Replace inline policy `ramen-sa-permissions` on the role (put_role_policy overwrites: idempotent); no actions = remove it."""
        doc = self.sa_permissions_policy(bucket, group, actions)
        if doc is None:
            try:
                self.iam.delete_role_policy(RoleName=name, PolicyName=self.SA_POLICY)
            except Exception as e:  # noqa: BLE001
                if aws_error_code(e) != "NoSuchEntity":
                    raise
            return []
        self.iam.put_role_policy(RoleName=name, PolicyName=self.SA_POLICY, PolicyDocument=json.dumps(doc))
        return list(actions)

    def put_worker_policy(self, name, bucket, group) -> str:
        self.iam.put_role_policy(RoleName=name, PolicyName="ramen-worker", PolicyDocument=json.dumps(self.worker_policy(bucket, group)))
        return "ramen-worker"

    def list_roles(self, prefix="ramen-") -> list[dict]:
        out = []
        for page in self.iam.get_paginator("list_roles").paginate(PathPrefix=ROLE_PATH):
            out += [r for r in page.get("Roles", []) if r["RoleName"].startswith(prefix)]
        return sorted(out, key=lambda r: r["RoleName"])

    def delete_role(self, name) -> None:
        try:
            for p in self.iam.list_role_policies(RoleName=name).get("PolicyNames", []):
                self.iam.delete_role_policy(RoleName=name, PolicyName=p)
            for p in self.iam.list_attached_role_policies(RoleName=name).get("AttachedPolicies", []):
                self.iam.detach_role_policy(RoleName=name, PolicyArn=p["PolicyArn"])
            self.iam.delete_role(RoleName=name)
        except Exception as e:  # noqa: BLE001
            if aws_error_code(e) != "NoSuchEntity":
                raise
