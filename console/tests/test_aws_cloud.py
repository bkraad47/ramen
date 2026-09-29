"""AWS adapter (CONTRACTS §8) against moto + the shared k8s fake. Untested on a real account."""

import asyncio
import json
import subprocess
from types import SimpleNamespace as NS

import pytest

from ramen_console.cloud import aws_api, make_cloud
from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.aws_k8s import ACTION, CONDITIONS, manifests, parse_weights, split_weights, weights
from ramen_console.errors import ApiError
from ramen_console.grpcclient import Client
from tests.fake_grpc import FakeWorker
from tests.fakes_aws import ACCOUNT, BUCKET, FakeAwsClients, FakeLogsInsights, aws_env, ip_set, web_acl

SPEC = {"region": "us-east-1a", "size": "s", "count": 2}
ADMIN = "adm-key"


@pytest.fixture
def http_state():
    w = FakeWorker(admin_key=ADMIN, mcp_keys=("rmk_1",)).start()
    w.load_fn = lambda n: "high" if n % 2 else "even"
    yield w
    w.stop()


@pytest.fixture
def fk():
    with aws_env():
        yield FakeAwsClients().seed()


@pytest.fixture
def cloud(fk, http_state, monkeypatch):
    monkeypatch.setattr(aws_api.time, "sleep", lambda s: None)
    return AwsCloud(
        region="us-east-1",
        bucket=BUCKET,
        image="img/worker:0.3.0",
        admin_key=ADMIN,
        clients=fk,
        rpc=Client(deadline=2, resolve=http_state.resolve),
        wait_secs=1,
        poll=0,
    )


def obj(fk, kind, ns, name):
    return fk.k8s.objs[(kind, ns, name)]


def test_weights_helpers():
    assert split_weights(2, 0) == (100, 0) and split_weights(2, 1) == (67, 33) and split_weights(1, 5) == (50, 50)
    assert parse_weights(None) == {"worker": 100, "worker-canary": 0}
    assert parse_weights({"metadata": {"annotations": {ACTION: weights(80, 20)}}}) == {
        "worker": 80,
        "worker-canary": 20,
    }
    assert parse_weights({"metadata": {"annotations": {ACTION: "not json"}}}) == {"worker": 100, "worker-canary": 0}


def test_manifests_shape():
    docs = manifests(
        "demo", "a", SPEC, "img", "s3://b/demo", role_arn=f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-a"
    )
    assert [d["kind"] for d in docs] == [
        "Namespace",
        "NetworkPolicy",
        "ServiceAccount",
        "Service",
        "Service",
        "Deployment",
        "Deployment",
        "HorizontalPodAutoscaler",
        "Ingress",
    ]
    assert docs[2]["metadata"]["annotations"]["eks.amazonaws.com/role-arn"].endswith("role/ramen/ramen-demo-a")
    assert docs[1]["spec"]["ingress"][0]["from"][1]["ipBlock"]["cidr"] == "10.0.0.0/16"
    assert (
        docs[3]["spec"]["selector"] == {"app": "worker", "ramen.io/track": "stable"}
        and docs[4]["spec"]["selector"]["ramen.io/track"] == "canary"
    )
    assert "cloud.google.com/neg" not in json.dumps(docs)
    dep = docs[5]
    env = {e["name"]: e["value"] for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["RAMEN_BUCKET_URI"] == "s3://b/demo" and "RAMEN_MCP_PATH_PREFIX" not in env
    assert docs[3]["spec"]["ports"][0]["appProtocol"] == "kubernetes.io/h2c"
    assert (
        dep["spec"]["template"]["spec"]["nodeSelector"] == {"topology.kubernetes.io/zone": "us-east-1a"}
        and dep["spec"]["replicas"] == 2
    )
    assert docs[6]["metadata"]["name"] == "worker-canary" and docs[6]["spec"]["replicas"] == 0
    ing = docs[8]
    ann = ing["metadata"]["annotations"]
    assert ing["spec"]["ingressClassName"] == "alb" and ann["alb.ingress.kubernetes.io/group.name"] == "ramen"
    assert (
        ann["alb.ingress.kubernetes.io/listen-ports"] == '[{"HTTPS":443}]'
        and ann["alb.ingress.kubernetes.io/scheme"] == "internet-facing"
    )
    path = ing["spec"]["rules"][0]["http"]["paths"][0]  # CONTRACTS §11: gRPC target groups, header conditions
    assert path["path"] == "/ramen.v1.Mcp" and path["backend"]["service"] == {
        "name": "worker",
        "port": {"name": "use-annotation"},
    }
    assert ann["alb.ingress.kubernetes.io/backend-protocol-version"] == "GRPC"
    assert ann["alb.ingress.kubernetes.io/success-codes"] == "0"
    assert ann["alb.ingress.kubernetes.io/healthcheck-path"] == "/grpc.health.v1.Health/Check"
    assert json.loads(ann[CONDITIONS]) == [
        {"field": "http-header", "httpHeaderConfig": {"httpHeaderName": "ramen-group", "values": ["demo"]}},
        {"field": "http-header", "httpHeaderConfig": {"httpHeaderName": "ramen-zone", "values": ["a"]}},
    ]
    assert parse_weights(ing) == {"worker": 100, "worker-canary": 0}
    assert "annotations" not in manifests("demo", "a", SPEC, "img", "s3://b/demo")[1]["metadata"]


async def test_attach_zone_idempotent_keeps_weights(cloud, fk):
    r = await cloud.attach_zone("demo", "a", SPEC)
    assert r["ok"] and r["namespace"] == "ramen-demo-a" and r["renderer"] == "python" and "Ingress" in r["applied"]
    fk.k8s.objs[("Ingress", "ramen-demo-a", "worker")]["metadata"]["annotations"][ACTION] = weights(70, 30)
    obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] = 1
    r2 = await cloud.attach_zone("demo", "a", {**SPEC, "count": 3})
    assert r2["ok"] and obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["replicas"] == 3
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 1
    assert parse_weights(obj(fk, "Ingress", "ramen-demo-a", "worker")) == {"worker": 70, "worker-canary": 30}
    assert ("patch", "Ingress", "ramen-demo-a", "worker") in fk.k8s.calls


async def test_deploy_canary_success_sets_split(cloud, fk, http_state):
    lines = []
    cfg = {"RAMEN_SECRET_DEMO__TOKEN": "s3cret", "RAMEN_MCP_KEYS": "rmk_1"}
    res = await cloud.deploy("demo", "prod", "a", canary=True, config=cfg, spec=SPEC, log=lines.append)
    assert res["ok"] is True, res
    sec = obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]
    assert (
        sec["RAMEN_SECRET_DEMO__TOKEN"] == "s3cret" and sec["RAMEN_ENV"] == "prod" and sec["RAMEN_ADMIN_KEY"] == ADMIN
    )
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 1
    paths = http_state.paths()
    assert paths.index("Admin/Reload") < paths.index("Mcp/Call")
    assert res["weights"] == {"worker": 67, "worker-canary": 33, "ingress": "worker"}
    assert parse_weights(obj(fk, "Ingress", "ramen-demo-a", "worker")) == {"worker": 67, "worker-canary": 33}
    assert any("traffic split stable 67% / canary 33%" in x for x in lines)
    assert "s3cret" not in json.dumps(res) and "s3cret" not in "\n".join(lines)


async def test_deploy_failure_scales_canary_and_zeroes_its_weight(cloud, fk, http_state):
    await cloud.attach_zone("demo", "a", SPEC)
    fk.k8s.objs[("Ingress", "ramen-demo-a", "worker")]["metadata"]["annotations"][ACTION] = weights(67, 33)
    http_state.smoke_ok = False
    res = await cloud.deploy("demo", "prod", "a", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec=SPEC)
    assert res["ok"] is False and "smoke" in res["error"]
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0
    assert parse_weights(obj(fk, "Ingress", "ramen-demo-a", "worker")) == {"worker": 100, "worker-canary": 0}
    assert any("scaled canary to 0" in x for x in res["log"])


async def test_deploy_split_error_is_logged_not_raised(cloud, fk):
    res = await cloud.deploy("demo", "prod", "a", canary=False, config={}, spec=SPEC)
    assert res["ok"]
    del fk.k8s.objs[("Ingress", "ramen-demo-a", "worker")]
    res = await cloud.deploy("demo", "prod", "a", canary=False, config={}, spec=SPEC)  # attach re-creates it
    assert res["ok"] and res["weights"]["ingress"] == "worker"
    cloud._set_weights = lambda *a: (_ for _ in ()).throw(RuntimeError("alb boom"))
    res = await cloud.deploy("demo", "prod", "a", canary=False, config={}, spec=SPEC)
    assert res["ok"] and any("traffic split not updated: RuntimeError: alb boom" in x for x in res["log"])


async def test_workers_and_error_prefix(cloud, fk, monkeypatch):
    assert await cloud.workers("demo", "a") == []
    await cloud.attach_zone("demo", "a", SPEC)
    ws = await cloud.workers("demo", "a")
    assert [w["id"] for w in ws] == ["worker-0", "worker-1"] and ws[1]["load"] == "high" and ws[0]["track"] == "stable"
    monkeypatch.setattr(
        type(fk.k8s.core), "list_namespaced_pod", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("apiserver down"))
    )
    with pytest.raises(ApiError) as e:
        await cloud.workers("demo", "a")
    assert e.value.status_code == 502 and e.value.detail.startswith("AWS workers:")


async def test_logs(cloud, fk):
    fk.logs.results = [
        {
            "@timestamp": "2026-09-28 01:00:01.000",
            "kubernetes.pod_name": "worker-0",
            "stream": "stdout",
            "log": '{"msg":"b"}',
        },
        {"@timestamp": "2026-09-28 01:00:00.000", "kubernetes.pod_name": "worker-1", "stream": "stderr", "log": "a"},
    ]
    fk.logs.pending = 2
    text = await cloud.logs("demo", "a", tail=5)
    lines = text.splitlines()
    assert lines[0].endswith("stderr worker-1 a") and '{"msg":"b"}' in lines[1]
    q = fk.logs.queries[0]
    assert (
        q["logGroupName"] == "/aws/containerinsights/ramen/application"
        and 'kubernetes.namespace_name = "ramen-demo-a"' in q["queryString"]
        and "limit 5" in q["queryString"]
    )
    await cloud.logs("demo", "a", worker="worker-0", tail=1)
    assert 'kubernetes.pod_name = "worker-0"' in fk.logs.queries[1]["queryString"]
    fk.logs.results = []
    assert await cloud.logs("demo", "a") == ""
    fk.logs.fail = "Failed"
    with pytest.raises(ApiError, match="Failed"):
        await cloud.logs("demo", "a")
    fk.logs.fail, fk.logs.missing = None, True
    assert "not found" in await cloud.logs("demo", "a")
    fk.logs = FakeLogsInsights(pending=10**6)
    with pytest.raises(ApiError) as e:
        await asyncio.to_thread(aws_api.fetch_logs, fk.logs, "g", "ns", None, 5, poll=0, timeout=0)
    assert e.value.status_code == 504 and fk.logs.stopped == ["q1"]


async def test_rebalance(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    r = await cloud.rebalance("demo", "a")  # no ALB yet: weights written, applied False, note
    assert r["ok"] and not r["applied"] and r["alb"] is None and "not provisioned" in r["note"]
    assert r["load"] == "high" and r["weights"] == {"worker": 100, "worker-canary": 0, "ingress": "worker"}
    lb = fk.alb()
    obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] = 1
    r = await cloud.rebalance("demo", "a")
    assert r["applied"] and r["alb"] == lb["DNSName"] and r["weights"]["worker-canary"] == 33
    assert parse_weights(obj(fk, "Ingress", "ramen-demo-a", "worker"))["worker-canary"] == 33
    fk.k8s.ready = False  # canary down → no traffic to it
    r = await cloud.rebalance("demo", "a")
    assert r["load"] == "low" and r["weights"]["worker-canary"] == 0
    fk.k8s.ready = True
    fk.k8s.objs[("HorizontalPodAutoscaler", "ramen-demo-a", "worker")]["spec"]["minReplicas"] = 3
    r = await cloud.rebalance("demo", "a")
    assert r["scaled_to"] == 3 and obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["replicas"] == 3
    del fk.k8s.objs[("Ingress", "ramen-demo-a", "worker")]
    assert (await cloud.rebalance("demo", "a"))["weights"]["ingress"] is None


async def test_set_ip_rules(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8", "192.168.0.0/16"])
    assert r["ok"] and not r["attached"] and "not attached" in r["note"] and r["policy"] == "ramen-demo"
    assert (
        obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"]
        == "10.0.0.0/8,192.168.0.0/16"
    )
    assert obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["template"]["metadata"]["annotations"][
        "ramen.io/restartedAt"
    ]
    assert ip_set(fk.wafv2, "ramen-demo")["Addresses"] == ["10.0.0.0/8", "192.168.0.0/16"]
    acl = web_acl(fk.wafv2)
    assert acl["DefaultAction"] == {"Allow": {}} and [x["Name"] for x in acl["Rules"]] == ["ramen-demo"]
    stmt = acl["Rules"][0]["Statement"]["AndStatement"]["Statements"]
    assert (
        stmt[0]["ByteMatchStatement"]["SearchString"] == b"/mcp/demo/"
        and "IPSetReferenceStatement" in stmt[1]["NotStatement"]["Statement"]
    )
    lb = fk.alb()
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8", "2001:db8::/32"])
    assert r["attached"] and r["alb"] == lb["DNSName"] and r["web_acl"].endswith("/webacl/ramen/" + acl["Id"])
    assert ip_set(fk.wafv2, "ramen-demo")["Addresses"] == ["10.0.0.0/8"] and ip_set(fk.wafv2, "ramen-demo-v6")[
        "Addresses"
    ] == ["2001:db8::/32"]
    stmt = web_acl(fk.wafv2)["Rules"][0]["Statement"]["AndStatement"]["Statements"]
    assert len(stmt[1]["NotStatement"]["Statement"]["OrStatement"]["Statements"]) == 2
    assert fk.wafv2.get_web_acl_for_resource(ResourceArn=lb["LoadBalancerArn"])["WebACL"]["Name"] == "ramen"
    await cloud.attach_zone("other", "a", SPEC)
    await cloud.set_ip_rules("other", "a", ["10.1.0.0/16"])
    assert [(x["Name"], x["Priority"]) for x in web_acl(fk.wafv2)["Rules"]] == [("ramen-demo", 1), ("ramen-other", 2)]
    r = await cloud.set_ip_rules("demo", "a", [])  # empty = allow all: rule removed, Secret opened
    assert r["ok"] and [x["Name"] for x in web_acl(fk.wafv2)["Rules"]] == ["ramen-other"]
    assert obj(fk, "Secret", "ramen-demo-a", "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "0.0.0.0/0"
    assert r["attached"]  # already associated: no-op


async def test_ip_rules_association_retries_in_background(cloud, fk, monkeypatch):
    from botocore.exceptions import ClientError

    await cloud.attach_zone("demo", "a", SPEC)
    fk.alb()
    real = fk.wafv2.associate_web_acl
    state = {"fail": 5}

    def flaky(**kw):
        if state["fail"] > 0:
            state["fail"] -= 1
            raise ClientError(
                {"Error": {"Code": "WAFUnavailableEntityException", "Message": "ALB not ready"}}, "AssociateWebACL"
            )
        return real(**kw)

    monkeypatch.setattr(fk.wafv2, "associate_web_acl", flaky)
    r = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    assert r["ok"] and not r["attached"] and "pending" in r["note"]
    for t in list(getattr(cloud, "_bg", ())):
        await t
    assert (
        state["fail"] == 0
        and fk.wafv2.get_web_acl_for_resource(
            ResourceArn=fk.elbv2.describe_load_balancers()["LoadBalancers"][0]["LoadBalancerArn"]
        )["WebACL"]
    )

    def hard(**kw):
        raise ClientError({"Error": {"Code": "AccessDeniedException", "Message": "nope"}}, "AssociateWebACL")

    monkeypatch.setattr(fk.wafv2, "associate_web_acl", hard)
    fk.wafv2.disassociate_web_acl(ResourceArn=fk.elbv2.describe_load_balancers()["LoadBalancers"][0]["LoadBalancerArn"])
    with pytest.raises(ApiError, match="AccessDenied"):
        await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])


async def test_create_service_account_idempotent(cloud, fk):
    r = await cloud.create_service_account("demo", "a")
    assert (
        r["name"] == f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-a"
        and r["created"] is True
        and r["ksa"] == "ramen-demo-a/worker"
    )
    assert r["workload_identity"].startswith("system:serviceaccount:ramen-demo-a:worker@oidc.eks.")
    role = fk.iam.get_role(RoleName="ramen-demo-a")["Role"]
    # SEC-10: worker roles always carry the permissions boundary the console policy demands on iam:CreateRole
    assert role["PermissionsBoundary"]["PermissionsBoundaryArn"] == (
        f"arn:aws:iam::{ACCOUNT}:policy/ramen/ramen-worker-boundary"
    )
    trust = role["AssumeRolePolicyDocument"]
    trust = json.loads(trust) if isinstance(trust, str) else trust
    cond = trust["Statement"][0]["Condition"]["StringEquals"]
    iss = trust["Statement"][0]["Principal"]["Federated"].split("oidc-provider/")[1]
    assert (
        cond[f"{iss}:sub"] == "system:serviceaccount:ramen-demo-a:worker" and cond[f"{iss}:aud"] == "sts.amazonaws.com"
    )
    pol = fk.iam.get_role_policy(RoleName="ramen-demo-a", PolicyName="ramen-worker")["PolicyDocument"]
    pol = json.loads(pol) if isinstance(pol, str) else pol
    res = [s["Resource"] for s in pol["Statement"]]
    assert (
        f"arn:aws:s3:::{BUCKET}/demo/*" in res
        and f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:ramen/demo/*" in res
    )
    assert (
        obj(fk, "ServiceAccount", "ramen-demo-a", "worker")["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
        == r["name"]
    )
    r2 = await cloud.create_service_account("demo", "a")
    assert r2["created"] is False and r2["name"] == r["name"]
    long = await cloud.create_service_account(
        "a-very-long-group-name-that-goes-on-and-on-forever", "zone-name-with-more-chars"
    )
    name = long["name"].rsplit("/", 1)[1]
    assert len(name) <= 64 and name.startswith("ramen-")
    # the role annotation is picked up by later manifests
    await cloud.attach_zone("demo", "a", SPEC)
    assert (
        obj(fk, "ServiceAccount", "ramen-demo-a", "worker")["metadata"]["annotations"]["eks.amazonaws.com/role-arn"]
        == r["name"]
    )


async def test_refresh_scale_and_detach(cloud, fk):
    await cloud.attach_zone("demo", "a", SPEC)
    await cloud.attach_zone("demo", "b", SPEC)
    await cloud.attach_zone("other", "a", SPEC)
    await cloud.create_service_account("demo", "a")
    await cloud.create_service_account("other", "a")
    await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8", "2001:db8::/32"])
    await cloud.set_ip_rules(
        "other", "a", ["10.1.0.0/16"]
    )  # moto ignores update_web_acl(Rules=[]) (AWS clears); keep one rule
    r = await cloud.refresh()
    assert (
        r["groups"] == ["demo", "other"]
        and r["zones"][0]["namespace"] == "ramen-demo-a"
        and r["zones"][0]["ready"] == 2
    )
    assert r["zones"][0]["service_account"].endswith("role/ramen/ramen-demo-a") and r["zones"][0]["weights"] == {
        "worker": 100,
        "worker-canary": 0,
    }
    assert r["service_accounts"] == [  # every attached zone gets its role on attach (§7/§8)
        f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-a",
        f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-b",
        f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-other-a",
    ]
    s = await cloud.scale("demo", "a", {**SPEC, "count": 4, "size": "l"})
    assert s["ok"] and obj(fk, "Deployment", "ramen-demo-a", "worker")["spec"]["replicas"] == 4
    d = await cloud.detach_group("demo")
    assert d["namespaces"] == ["ramen-demo-a", "ramen-demo-b"] and d["service_accounts"] == [
        f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-a",
        f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-b",
    ]
    assert ("Namespace", None, "ramen-other-a") in fk.k8s.objs and (
        "Namespace",
        None,
        "ramen-demo-a",
    ) not in fk.k8s.objs
    assert [x["RoleName"] for x in fk.iam.list_roles(PathPrefix="/ramen/")["Roles"]] == ["ramen-other-a"]
    assert ip_set(fk.wafv2, "ramen-demo") is None and ip_set(fk.wafv2, "ramen-demo-v6") is None
    assert [x["Name"] for x in web_acl(fk.wafv2)["Rules"]] == ["ramen-other"]
    assert (await cloud.detach_group("demo")) == {"namespaces": [], "service_accounts": []}


@pytest.fixture
def repo(tmp_path):
    src = tmp_path / "src"
    (src / "mcp").mkdir(parents=True)
    (src / "mcp" / "requirements.txt").write_text("x")
    (src / "old.txt").write_text("old")
    subprocess.run(["git", "init", "-q", "-b", "main", str(src)], check=True)
    for cmd in (["add", "."], ["commit", "-qm", "init"]):
        subprocess.run(["git", "-C", str(src), "-c", "user.email=t@t", "-c", "user.name=t", *cmd], check=True)
    return src


def keys(s3, prefix=""):
    return sorted(o["Key"] for o in s3.list_objects_v2(Bucket=BUCKET, Prefix=prefix).get("Contents", []))


async def test_sync_repo(cloud, fk, repo):
    fk.s3.put_object(Bucket=BUCKET, Key="demo/stale.txt", Body=b"stale")
    fk.s3.put_object(Bucket=BUCKET, Key="other/keep.txt", Body=b"keep")
    assert await cloud.sync_repo("demo", str(repo), "main", None) == f"s3://{BUCKET}/demo"
    assert keys(fk.s3) == ["demo/mcp/requirements.txt", "demo/old.txt", "other/keep.txt"]
    (repo / "old.txt").unlink()
    (repo / "new.txt").write_text("new")
    for cmd in (["add", "-A"], ["commit", "-qm", "2"]):
        subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *cmd], check=True)
    await cloud.sync_repo("demo", str(repo), "main", "tok")
    assert keys(fk.s3) == ["demo/mcp/requirements.txt", "demo/new.txt", "other/keep.txt"]
    with pytest.raises(ApiError) as e:
        await cloud.sync_repo("demo", str(repo / "nope"), "main", "tok")
    assert e.value.status_code == 502 and "tok" not in str(e.value.detail)
    assert aws_api.list_prefix(fk.s3, BUCKET, "demo/")["demo/new.txt"] == "22af645d1859cb5ca6da0c484f1f37ea"


async def test_bucket_defaults_to_account(fk, monkeypatch):
    c = AwsCloud(clients=fk)
    assert c.bucket_uri("demo") == f"s3://{BUCKET}/demo" and c.bucket == BUCKET


async def test_helm_template_path(cloud, fk, tmp_path, monkeypatch):
    chart = tmp_path / "chart"
    chart.mkdir()
    seen = {}

    def fake_run(cmd, capture_output, text):
        seen["cmd"] = cmd
        return NS(
            returncode=0,
            stdout="apiVersion: v1\nkind: Namespace\nmetadata:\n  name: ramen-demo-a\n---\n"
            "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: worker\n  namespace: ramen-demo-a\n"
            "spec:\n  replicas: 2\n  template:\n    metadata:\n      labels: {app: worker, ramen.io/track: stable}\n",
            stderr="",
        )

    monkeypatch.setattr("ramen_console.cloud.aws_k8s.subprocess.run", fake_run)
    monkeypatch.setattr("ramen_console.cloud.gcp_k8s.shutil.which", lambda _: "/usr/bin/helm")
    cloud.chart = str(chart)
    r = await cloud.attach_zone("demo", "a", SPEC)
    assert (
        r["renderer"] == "helm"
        and "provider=aws" in seen["cmd"]
        and "aws.zone=us-east-1a" in seen["cmd"]
        and "secret.create=false" in seen["cmd"]
    )
    assert ("Ingress", "ramen-demo-a", "worker") in fk.k8s.objs  # appended when the chart renders none
    fk.k8s.objs[("Ingress", "ramen-demo-a", "worker")]["metadata"]["annotations"][ACTION] = weights(60, 40)
    monkeypatch.setattr(
        "ramen_console.cloud.aws_k8s.subprocess.run",
        lambda cmd, capture_output, text: NS(
            returncode=0,
            stderr="",
            stdout=("apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata:\n  name: worker\nspec: {}\n"),
        ),
    )
    await cloud.attach_zone("demo", "a", SPEC)
    live = obj(fk, "Ingress", "ramen-demo-a", "worker")
    assert parse_weights(live) == {"worker": 60, "worker-canary": 40}  # chart output gets the live split
    assert "ramen-zone" in live["metadata"]["annotations"][CONDITIONS]
    monkeypatch.setattr(
        "ramen_console.cloud.aws_k8s.subprocess.run", lambda *a, **k: NS(returncode=1, stdout="", stderr="bad chart")
    )
    with pytest.raises(ApiError, match="Helm"):
        await cloud.attach_zone("demo", "a", SPEC)


def test_factory_and_env(monkeypatch):
    for k, v in {
        "RAMEN_CLOUD": "aws",
        "RAMEN_AWS_REGION": "eu-west-1",
        "RAMEN_GROUPS_BUCKET": "bkt",
        "RAMEN_IMAGE_WORKER": "img:1",
        "RAMEN_EKS_CLUSTER": "c1",
        "RAMEN_ALB_GROUP": "grp",
    }.items():
        monkeypatch.setenv(k, v)
    c = make_cloud()
    assert isinstance(c, AwsCloud) and c.region == "eu-west-1" and c.bucket == "bkt" and c.image == "img:1"
    assert c.rpc.deadline == 10.0
    assert c.cluster == "c1" and c.alb_group == "grp" and c.log_group == "/aws/containerinsights/c1/application"
    assert c.boundary is None  # default: arn:aws:iam::<account>:policy/ramen/ramen-worker-boundary (resolved lazily)
    monkeypatch.setenv("RAMEN_AWS_PERMISSIONS_BOUNDARY", "-")
    assert make_cloud()._iam().boundary() is None
    monkeypatch.setenv("RAMEN_AWS_PERMISSIONS_BOUNDARY", "arn:aws:iam::1:policy/x")
    assert make_cloud()._iam().boundary() == "arn:aws:iam::1:policy/x"
    assert isinstance(c.c, aws_api.AwsClients) and c.c.region == "eu-west-1"


def test_real_clients_lazy(monkeypatch):
    import types

    from botocore.exceptions import ClientError

    assert aws_api.aws_error_code(ClientError({"Error": {"Code": "Throttling", "Message": ""}}, "Op")) == "Throttling"
    assert aws_api.aws_error_code(RuntimeError()) is None
    a = aws_api.AwsClients("us-east-1")
    fake_client = types.SimpleNamespace(
        CoreV1Api=lambda: "core",
        NetworkingV1Api=lambda: "net",
        AppsV1Api=lambda: "apps",
        AutoscalingV2Api=lambda: "hpa",
        CustomObjectsApi=lambda: "custom",
        RbacAuthorizationV1Api=lambda: "rbac",
        ApiClient=lambda: None,
    )
    monkeypatch.setattr(
        a,
        "_kube_modules",
        lambda: (types.SimpleNamespace(load_incluster_config=lambda: None, load_kube_config=lambda: None), fake_client),
    )
    assert a.networking == "net" and a.core == "core" and a.rbac == "rbac"
    with aws_env():
        assert (
            a.s3.meta.region_name == "us-east-1"
            and a.s3 is a.s3
            and a.wafv2
            and a.elbv2
            and a.iam
            and a.sts
            and a.eks
            and a.logs
            and a.secretsmanager
        )
        assert a.s3.meta.config.retries["mode"] == "adaptive"
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ClientError({"Error": {"Code": "WAFOptimisticLockException", "Message": "x"}}, "Op")
        return "ok"

    monkeypatch.setattr(aws_api.time, "sleep", lambda s: None)
    assert aws_api._retry(flaky) == "ok" and calls["n"] == 3
    with pytest.raises(ClientError):
        aws_api._retry(
            lambda: (_ for _ in ()).throw(ClientError({"Error": {"Code": "AccessDenied", "Message": "x"}}, "Op"))
        )


def _policy(fk, role, name):
    d = fk.iam.get_role_policy(RoleName=role, PolicyName=name)["PolicyDocument"]
    return json.loads(d) if isinstance(d, str) else d


async def test_apply_sa_permissions_puts_scoped_inline_policy(cloud, fk):
    r = await cloud.apply_sa_permissions(
        "demo", "a", ["bucket.read", "bucket.write", "logs.write", "secrets.read", "unknown.perm"]
    )
    assert (
        r["ok"]
        and r["service_account"] == f"arn:aws:iam::{ACCOUNT}:role/ramen/ramen-demo-a"
        and r["ksa"] == "ramen-demo-a/worker"
    )
    assert r["applied"] == [
        "s3:GetObject",
        "s3:ListBucket",
        "s3:PutObject",
        "s3:DeleteObject",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
        "secretsmanager:GetSecretValue",
    ]
    assert r["policy"] == "ramen-sa-permissions" and r["permissions"][-1] == "unknown.perm"
    assert sorted(fk.iam.list_role_policies(RoleName="ramen-demo-a")["PolicyNames"]) == [
        "ramen-sa-permissions",
        "ramen-worker",
    ]
    st = _policy(fk, "ramen-demo-a", "ramen-sa-permissions")["Statement"]
    by_res = {json.dumps(s["Resource"]): s for s in st}
    lst = by_res[json.dumps(f"arn:aws:s3:::{BUCKET}")]
    assert lst["Action"] == ["s3:ListBucket"] and lst["Condition"]["StringLike"]["s3:prefix"] == ["demo/*", "demo/"]
    assert set(by_res[json.dumps(f"arn:aws:s3:::{BUCKET}/demo/*")]["Action"]) == {
        "s3:GetObject",
        "s3:PutObject",
        "s3:DeleteObject",
    }
    assert by_res[json.dumps(f"arn:aws:secretsmanager:us-east-1:{ACCOUNT}:secret:ramen/demo/*")]["Action"] == [
        "secretsmanager:GetSecretValue"
    ]
    assert by_res[json.dumps("*")]["Action"] == ["logs:CreateLogStream", "logs:PutLogEvents"]
    # idempotent replace: fewer permissions shrink the policy; the base worker policy is untouched
    again = await cloud.apply_sa_permissions("demo", "a", ["metrics.write"])
    assert again["applied"] == ["cloudwatch:PutMetricData"]
    st = _policy(fk, "ramen-demo-a", "ramen-sa-permissions")["Statement"]
    assert len(st) == 1 and st[0] == {"Effect": "Allow", "Action": ["cloudwatch:PutMetricData"], "Resource": "*"}
    assert _policy(fk, "ramen-demo-a", "ramen-worker")["Statement"]
    # no permissions: policy removed (and removing twice is fine)
    for _ in range(2):
        r = await cloud.apply_sa_permissions("demo", "a", [])
        assert r["applied"] == [] and r["policy"] is None
        assert fk.iam.list_role_policies(RoleName="ramen-demo-a")["PolicyNames"] == ["ramen-worker"]
    # bucket.read alone: only the two s3 statements
    r = await cloud.apply_sa_permissions("demo", "a", ["bucket.read"])
    assert [s["Resource"] for s in _policy(fk, "ramen-demo-a", "ramen-sa-permissions")["Statement"]] == [
        f"arn:aws:s3:::{BUCKET}",
        f"arn:aws:s3:::{BUCKET}/demo/*",
    ]


async def test_set_ip_rules_enforces_at_the_node_when_waf_is_unreachable(cloud, fk):
    """The node list is the control that gates a call; a WAF failure must not leave the zone unlocked
    (found on kind as D1, same shape as the GCP adapter)."""
    await cloud.attach_zone("demo", "a", SPEC)

    def boom(*a, **k):
        raise RuntimeError("Unable to locate credentials")

    fk.wafv2.create_ip_set = boom
    fk.wafv2.update_ip_set = boom
    out = await cloud.set_ip_rules("demo", "a", ["10.0.0.0/8"])
    assert out["ok"] and out["attached"] is False and "enforced at the node" in out["note"]
    sec = fk.k8s.objs[("Secret", "ramen-demo-a", "ramen-deploy")]["stringData"]
    assert sec["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"
