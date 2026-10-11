"""Azure adapter (§21.6, D54) against fakes for Resource Manager, Blob, Key Vault and the shared k8s fake."""

import asyncio
import json
import subprocess

import pytest

from ramen_console.cloud import azure_api, make_cloud
from ramen_console.cloud.azure import AzureCloud
from ramen_console.cloud.azure_k8s import ENVOY_NAMESPACE, WI_CLIENT_ID, WI_USE, azure_manifests, securitypolicy
from ramen_console.errors import ApiError
from ramen_console.grpcclient import Client
from tests.fake_grpc import FakeWorker
from tests.fakes_azure import ACCOUNT, ISSUER, LOCATION, RG, SUB, VAULT, FakeAzureClients, jsonl

SPEC = {"region": "eastus-1", "size": "s", "count": 2}
ADMIN = "adm-key"
NS = "ramen-demo-eus1"
ROLE = azure_api.ROLE_IDS


@pytest.fixture
def http_state():
    w = FakeWorker(admin_key=ADMIN, mcp_keys=("rmk_1",)).start()
    w.load_fn = lambda n: "high" if n % 2 else "even"
    yield w
    w.stop()


@pytest.fixture
def fk():
    return FakeAzureClients()


@pytest.fixture
def cloud(fk, http_state):
    return AzureCloud(
        subscription=SUB,
        resource_group=RG,
        location=LOCATION,
        storage_account=ACCOUNT,
        key_vault=VAULT,
        oidc_issuer=ISSUER,
        image="acr.example/worker:0.7.5",
        admin_key=ADMIN,
        clients=fk,
        rpc=Client(deadline=2, resolve=http_state.resolve),
        wait_secs=1,
        poll=0,
    )


def obj(fk, kind, ns, name):
    return fk.k8s.objs[(kind, ns, name)]


def container_scope():
    return (
        f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Storage/storageAccounts/{ACCOUNT}"
        "/blobServices/default/containers/groups"
    )


def secret_scope(name):
    return f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.KeyVault/vaults/{VAULT}/secrets/{name}"


# manifests ---------------------------------------------------------------------
def test_manifests_drop_gke_parts_and_add_workload_identity():
    docs = azure_manifests("demo", "eus1", SPEC, "img", "az://ramenacct/groups/demo", "cid-1")
    kinds = [d["kind"] for d in docs]
    assert "HealthCheckPolicy" not in kinds and "HTTPRoute" in kinds and kinds[0] == "Namespace"
    svc = next(d for d in docs if d["kind"] == "Service")
    assert "cloud.google.com/neg" not in (svc["metadata"].get("annotations") or {})
    ksa = next(d for d in docs if d["kind"] == "ServiceAccount")
    assert ksa["metadata"]["annotations"] == {WI_CLIENT_ID: "cid-1"}
    for dep in (d for d in docs if d["kind"] == "Deployment"):
        tpl = dep["spec"]["template"]
        assert tpl["metadata"]["labels"][WI_USE] == "true"
        env = {e["name"]: e["value"] for e in tpl["spec"]["containers"][0]["env"]}
        assert env["RAMEN_TRUST_PROXY_HOPS"] == "1" and env["RAMEN_BUCKET_URI"] == "az://ramenacct/groups/demo"
    peers = next(d for d in docs if d["kind"] == "NetworkPolicy")["spec"]["ingress"][0]["from"]
    names = [p.get("namespaceSelector", {}).get("matchLabels", {}).get("kubernetes.io/metadata.name") for p in peers]
    assert names == ["ramen-system", ENVOY_NAMESPACE] and not any("ipBlock" in p for p in peers)
    route = next(d for d in docs if d["kind"] == "HTTPRoute")
    assert {"name": "ramen-zone", "value": "eus1"} in route["spec"]["rules"][0]["matches"][0]["headers"]
    without = azure_manifests("demo", "eus1", SPEC, "img", "az://x/groups/demo", None)
    assert "annotations" not in next(d for d in without if d["kind"] == "ServiceAccount")["metadata"]


def test_security_policy_shape():
    sp = securitypolicy("demo", "eus1", ["10.0.0.0/8", "2001:db8::/32"])
    assert sp["kind"] == "SecurityPolicy" and sp["metadata"] == {
        "name": "worker",
        "namespace": NS,
        "labels": {"ramen.io/group": "demo", "ramen.io/zone": "eus1"},
    }
    assert sp["spec"]["targetRefs"] == [{"group": "gateway.networking.k8s.io", "kind": "HTTPRoute", "name": "worker"}]
    auth = sp["spec"]["authorization"]
    assert auth["defaultAction"] == "Deny" and auth["rules"][0]["principal"]["clientCIDRs"] == [
        "10.0.0.0/8",
        "2001:db8::/32",
    ]


# zone lifecycle ----------------------------------------------------------------
async def test_attach_zone_creates_identity_federation_and_baseline_roles(cloud, fk):
    res = await cloud.attach_zone("demo", "eus1", SPEC)
    assert res["ok"] and res["namespace"] == NS
    ident = fk.arm.identities["ramen-demo-eus1"]
    cid, pid = ident["properties"]["clientId"], ident["properties"]["principalId"]
    assert ident["location"] == LOCATION and ident["tags"] == {"ramen-group": "demo", "ramen-zone": "eus1"}
    fic = fk.arm.fics[("ramen-demo-eus1", "worker")]["properties"]
    assert fic == {
        "issuer": ISSUER,
        "subject": f"system:serviceaccount:{NS}:worker",
        "audiences": ["api://AzureADTokenExchange"],
    }
    assert obj(fk, "ServiceAccount", NS, "worker")["metadata"]["annotations"] == {WI_CLIENT_ID: cid}
    assert (ROLE["Storage Blob Data Reader"], container_scope(), "ramen-baseline") in fk.arm.roles_of(pid)
    calls = len(fk.arm.calls)
    await cloud.attach_zone("demo", "eus1", SPEC)  # idempotent: the existing identity is reused
    assert (
        len(fk.arm.identities) == 1
        and len([c for c in fk.arm.calls[calls:] if c[0] == "PUT" and "roleAssign" in c[1]]) == 0
    )


async def test_new_secret_is_bound_for_every_zone_identity_of_its_group(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    await cloud.attach_zone("demo", "weu1", {**SPEC, "region": "westeurope-1"})
    await cloud.attach_zone("other", "eus1", SPEC)
    assert cloud.bind_new_secret("secrets/ramen--demo--all--all--K", "demo") is True
    for name in ("ramen-demo-eus1", "ramen-demo-weu1"):
        pid = fk.arm.identities[name]["properties"]["principalId"]
        assert (
            ROLE["Key Vault Secrets User"],
            secret_scope("ramen--demo--all--all--K"),
            "ramen-baseline",
        ) in fk.arm.roles_of(pid)
    other = fk.arm.identities["ramen-other-eus1"]["properties"]["principalId"]
    assert not [r for r in fk.arm.roles_of(other) if "secrets/" in r[1]]
    assert cloud.bind_new_secret("secrets/ramen--nobody--all--all--K", "nobody") is False


async def test_existing_group_secrets_are_granted_when_a_zone_identity_is_created(cloud, fk):
    fk.vault.set_secret("ramen--demo--all--all--A", "x", tags={"group": "demo"})
    fk.vault.set_secret("ramen--other--all--all--B", "y", tags={"group": "other"})
    await cloud.attach_zone("demo", "eus1", SPEC)
    pid = fk.arm.identities["ramen-demo-eus1"]["properties"]["principalId"]
    scopes = [r[1] for r in fk.arm.roles_of(pid) if r[0] == ROLE["Key Vault Secrets User"]]
    assert scopes == [secret_scope("ramen--demo--all--all--A")]


async def test_deploy_canary_then_stable(cloud, fk, http_state):
    lines = []
    cfg = {"RAMEN_SECRET_DEMO__TOKEN": "s3cret", "RAMEN_MCP_KEYS": "rmk_1"}
    res = await cloud.deploy("demo", "prod", "eus1", canary=True, config=cfg, spec=SPEC, log=lines.append)
    assert res["ok"] is True, res
    sec = obj(fk, "Secret", NS, "ramen-deploy")["stringData"]
    assert sec["RAMEN_ENV"] == "prod" and sec["RAMEN_ADMIN_KEY"] == ADMIN
    paths = http_state.paths()
    assert paths.index("Admin/Reload") < paths.index("Mcp/Call")
    assert "s3cret" not in json.dumps(res) and "s3cret" not in "\n".join(lines)
    http_state.smoke_ok = False
    res = await cloud.deploy("demo", "prod", "eus1", config={"RAMEN_MCP_KEYS": "rmk_1"}, spec=SPEC)
    assert res["ok"] is False and obj(fk, "Deployment", NS, "worker-canary")["spec"]["replicas"] == 0


async def test_workers_and_error_prefix(cloud, fk, monkeypatch):
    assert await cloud.workers("demo", "eus1") == []
    await cloud.attach_zone("demo", "eus1", SPEC)
    ws = await cloud.workers("demo", "eus1")
    assert [w["id"] for w in ws] == ["worker-0", "worker-1"] and ws[1]["load"] == "high"
    monkeypatch.setattr(
        type(fk.k8s.core.base), "list_namespaced_pod", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("down"))
    )
    with pytest.raises(ApiError) as e:
        await cloud.workers("demo", "eus1")
    assert e.value.status_code == 502 and e.value.detail.startswith("Azure workers:")


async def test_logs_come_from_pod_logs_newest_last_and_bounded(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    fk.k8s.pod_logs[(NS, "worker-0")] = jsonl(
        {"ts": "2026-10-11T00:00:01Z", "msg": "a"}, {"ts": "2026-10-11T00:00:03Z", "msg": "c"}
    ) + ["startup banner, not json"]
    fk.k8s.pod_logs[(NS, "worker-1")] = jsonl({"ts": "2026-10-11T00:00:02Z", "msg": "b"})
    out = await cloud.logs("demo", "eus1", tail=500)
    docs = [json.loads(x) for x in out.splitlines() if x.startswith("{")]
    assert [d["msg"] for d in docs] == ["a", "b", "c"] and docs[1]["pod"] == "worker-1"
    assert "startup banner, not json" in out
    one = await cloud.logs("demo", "eus1", worker="worker-1", tail=500)
    assert '"msg": "b"' in one and '"msg": "a"' not in one
    assert len((await cloud.logs("demo", "eus1", tail=2)).splitlines()) == 2
    with pytest.raises(ApiError) as e:
        await cloud.logs("demo", "eus1", worker="x;rm -rf")
    assert e.value.status_code == 422
    assert await cloud.logs("demo", "nozone") == ""


async def test_set_ip_rules_writes_the_node_list_and_the_edge_policy(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    res = await cloud.set_ip_rules("demo", "eus1", ["10.0.0.0/8"])
    assert res["ok"] and res["attached"] and res["policy"] == f"{NS}/worker"
    assert obj(fk, "Secret", NS, "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"
    sp = obj(fk, "SecurityPolicy", NS, "worker")
    assert sp["spec"]["authorization"]["rules"][0]["principal"]["clientCIDRs"] == ["10.0.0.0/8"]
    res = await cloud.set_ip_rules("demo", "eus1", ["10.0.0.0/8", "192.168.0.0/16"])
    assert obj(fk, "SecurityPolicy", NS, "worker")["spec"]["authorization"]["rules"][0]["principal"]["clientCIDRs"] == [
        "10.0.0.0/8",
        "192.168.0.0/16",
    ]
    res = await cloud.set_ip_rules("demo", "eus1", [])  # open again: the edge policy goes away
    assert ("SecurityPolicy", NS, "worker") not in fk.k8s.objs and res["attached"] is False
    assert obj(fk, "Secret", NS, "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "0.0.0.0/0"
    await cloud.set_ip_rules("demo", "eus1", ["0.0.0.0/0"])
    assert ("SecurityPolicy", NS, "worker") not in fk.k8s.objs


async def test_set_ip_rules_degrades_to_the_node_when_the_edge_api_fails(cloud, fk, monkeypatch):
    await cloud.attach_zone("demo", "eus1", SPEC)

    def boom(*a, **k):
        raise RuntimeError("envoy CRD missing")

    monkeypatch.setattr(type(fk.k8s.custom), "create_namespaced_custom_object", boom)
    res = await cloud.set_ip_rules("demo", "eus1", ["10.0.0.0/8"])
    assert res["ok"] and not res["attached"] and "enforced at the node" in res["note"]
    assert obj(fk, "Secret", NS, "ramen-deploy")["stringData"]["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"


async def test_rebalance_rescales_to_the_hpa_floor(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    fk.k8s.objs[("Deployment", NS, "worker")]["spec"]["replicas"] = 5
    res = await cloud.rebalance("demo", "eus1")
    assert res["ok"] and res["scaled_to"] == 2 and res["load"] in ("high", "even", "low")
    assert obj(fk, "Deployment", NS, "worker")["spec"]["replicas"] == 2


async def test_detach_zone_removes_namespace_identity_and_its_role_assignments(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    await cloud.attach_zone("demo", "weu1", {**SPEC, "region": "westeurope-1"})
    pid = fk.arm.identities["ramen-demo-eus1"]["properties"]["principalId"]
    assert fk.arm.roles_of(pid)
    removed = await cloud.detach_zone("demo", "eus1")
    assert removed == {"namespaces": [NS], "service_accounts": ["ramen-demo-eus1"]}
    assert "ramen-demo-eus1" not in fk.arm.identities and not fk.arm.roles_of(pid)
    assert ("Namespace", None, NS) not in fk.k8s.objs and ("ramen-demo-eus1", "worker") not in fk.arm.fics
    assert "ramen-demo-weu1" in fk.arm.identities  # the other zone is untouched
    assert await cloud.detach_zone("demo", "eus1") == {"namespaces": [NS], "service_accounts": []}


async def test_detach_group_removes_every_zone_of_the_group(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    await cloud.attach_zone("demo", "weu1", {**SPEC, "region": "westeurope-1"})
    await cloud.attach_zone("other", "eus1", SPEC)
    removed = await cloud.detach_group("demo")
    assert sorted(removed["namespaces"]) == ["ramen-demo-eus1", "ramen-demo-weu1"]
    assert sorted(removed["service_accounts"]) == ["ramen-demo-eus1", "ramen-demo-weu1"]
    assert list(fk.arm.identities) == ["ramen-other-eus1"]


async def test_apply_sa_permissions_scopes_and_replaces(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    pid = fk.arm.identities["ramen-demo-eus1"]["properties"]["principalId"]
    res = await cloud.apply_sa_permissions(
        "demo", "eus1", ["bucket.write", "logs.write"], scopes={"bucket.write": ["*"], "logs.write": ["*"]}
    )
    assert res["ok"] and res["service_account"] == fk.arm.identities["ramen-demo-eus1"]["properties"]["clientId"]
    rg = f"/subscriptions/{SUB}/resourceGroups/{RG}"
    managed = [r for r in fk.arm.roles_of(pid) if r[2] == "ramen-sa-permissions"]
    assert (ROLE["Storage Blob Data Contributor"], container_scope(), "ramen-sa-permissions") in managed
    assert (ROLE["Log Analytics Contributor"], rg, "ramen-sa-permissions") in managed
    # a named bucket scope binds that container; dropping logs.write revokes it; the baseline stays
    res = await cloud.apply_sa_permissions("demo", "eus1", ["bucket.write"], scopes={"bucket.write": ["reports"]})
    managed = [r for r in fk.arm.roles_of(pid) if r[2] == "ramen-sa-permissions"]
    assert managed == [
        (
            ROLE["Storage Blob Data Contributor"],
            container_scope().replace("/groups", "/reports"),
            "ramen-sa-permissions",
        )
    ]
    assert (ROLE["Storage Blob Data Reader"], container_scope(), "ramen-baseline") in fk.arm.roles_of(pid)
    res = await cloud.apply_sa_permissions("demo", "eus1", ["ai.user"], scopes={"ai.user": ["model-a"]})
    assert res["unscoped"] == ["ai.user"]


async def test_arm_errors_surface_as_502_with_the_adapter_name(cloud, fk):
    fk.arm.fail["/userAssignedIdentities/ramen-demo-eus1"] = 403
    with pytest.raises(ApiError) as e:
        await cloud.create_service_account("demo", "eus1")
    assert e.value.status_code == 502 and e.value.detail.startswith("Azure create_service_account:")


async def test_refresh_surveys_zones(cloud, fk):
    await cloud.attach_zone("demo", "eus1", SPEC)
    out = await cloud.refresh()
    assert out["groups"] == ["demo"] and out["zones"][0]["namespace"] == NS
    assert out["zones"][0]["service_account"] == fk.arm.identities["ramen-demo-eus1"]["properties"]["clientId"]
    assert out["service_accounts"] == ["ramen-demo-eus1"]


# code bucket -------------------------------------------------------------------
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


async def test_sync_repo_mirrors_the_group_prefix_and_read_file(cloud, fk, repo):
    fk.container.blobs["demo/stale.txt"] = b"stale"
    fk.container.blobs["other/keep.txt"] = b"keep"
    assert await cloud.sync_repo("demo", str(repo), "main", None) == f"az://{ACCOUNT}/groups/demo"
    assert sorted(fk.container.blobs) == ["demo/mcp/requirements.txt", "demo/old.txt", "other/keep.txt"]
    fk.container.uploads.clear()
    await cloud.sync_repo("demo", str(repo), "main", None)
    assert fk.container.uploads == []  # unchanged files are not uploaded again
    assert await cloud.read_file("demo", "mcp/requirements.txt") == b"x"
    assert await cloud.read_file("demo", "nope") is None
    with pytest.raises(ApiError) as e:
        await cloud.sync_repo("demo", str(repo / "nope"), "main", "tok")
    assert e.value.status_code == 502 and "tok" not in str(e.value.detail)


# factory -----------------------------------------------------------------------
def test_factory_reads_the_environment(monkeypatch):
    for k, v in {
        "RAMEN_CLOUD": "azure",
        "RAMEN_AZURE_SUBSCRIPTION_ID": SUB,
        "RAMEN_AZURE_RESOURCE_GROUP": RG,
        "RAMEN_AZURE_LOCATION": LOCATION,
        "RAMEN_AZURE_STORAGE_ACCOUNT": ACCOUNT,
        "RAMEN_AZURE_KEY_VAULT": VAULT,
        "RAMEN_AZURE_OIDC_ISSUER": ISSUER,
    }.items():
        monkeypatch.setenv(k, v)
    c = make_cloud()
    assert isinstance(c, AzureCloud) and c.bucket_uri("demo") == f"az://{ACCOUNT}/groups/demo"
    assert c.subscription == SUB and c.oidc_issuer == ISSUER
    monkeypatch.delenv("RAMEN_AZURE_SUBSCRIPTION_ID")
    with pytest.raises(ValueError, match="RAMEN_AZURE_SUBSCRIPTION_ID"):
        make_cloud()


def test_real_clients_are_lazy(monkeypatch):
    c = azure_api.AzureClients(SUB, ACCOUNT, VAULT)
    assert c._c == {}  # nothing is built (or authenticated) until first use


def test_arm_request_maps_http_errors(monkeypatch):
    class Resp:
        def __init__(self, status, body):
            self.status_code, self._b = status, body
            self.text = json.dumps(body)

        def json(self):
            return self._b

    calls = []

    class Http:
        def request(self, method, url, json=None, params=None, headers=None, timeout=None):
            calls.append((method, url, params, headers["Authorization"]))
            return {"/ok": Resp(200, {"a": 1}), "/gone": Resp(204, None)}.get(
                url.split(".com")[1].split("?")[0],
                Resp(403, {"error": {"code": "AuthorizationFailed", "message": "no"}}),
            )

    arm = azure_api.Arm(lambda: "tok", http=Http())
    assert arm.request("GET", "/ok") == {"a": 1} and arm.request("DELETE", "/gone") is None
    with pytest.raises(azure_api.ArmError) as e:
        arm.request("PUT", "/denied", {"x": 1})
    assert e.value.status == 403 and "AuthorizationFailed" in str(e.value)
    assert calls[0][3] == "Bearer tok" and calls[0][2]["api-version"]
    asyncio.run(asyncio.sleep(0))


def test_identity_api_error_paths():
    from tests.fakes_azure import FakeArm

    arm = FakeArm()
    ids = azure_api.Identities(arm, SUB, RG, LOCATION)
    assert ids.get("nope") is None
    ids.delete("nope")  # deleting what is not there is fine
    ident, created = ids.ensure("ramen-demo-x", {"ramen-group": "demo"})
    assert created and ids.ensure("ramen-demo-x", {})[1] is False
    pid = ident["properties"]["principalId"]
    scope = f"/subscriptions/{SUB}/resourceGroups/{RG}"
    assert ids.grant(pid, "Cognitive Services User", scope) is True
    assert ids.grant(pid, "Cognitive Services User", scope, existing=[]) is False  # 409 from Azure: already there
    gone = f"{scope}/providers/Microsoft.Authorization/roleAssignments/none"
    ids.revoke(gone)  # already gone is fine
    arm.fail["roleAssignments/other"] = 500
    with pytest.raises(azure_api.ArmError):
        ids.revoke(f"{scope}/providers/Microsoft.Authorization/roleAssignments/other")
    arm.fail["/userAssignedIdentities/boom"] = 500
    with pytest.raises(azure_api.ArmError):
        ids.get("boom")
    arm.fail["/userAssignedIdentities/boom2"] = 500
    with pytest.raises(azure_api.ArmError):
        ids.delete("boom2")
    assert ids.all() == ["ramen-demo-x"]


def test_real_clients_build_lazily_once(monkeypatch):
    import sys
    import types

    built = []

    class Cred:
        def get_token(self, scope):
            built.append(("token", scope))
            return types.SimpleNamespace(token="t")

    class Svc:
        def __init__(self, url, credential):
            built.append(("blob", url))

        def get_container_client(self, name):
            return ("container", name)

    class Secrets:
        def __init__(self, url, credential):
            built.append(("kv", url))

    monkeypatch.setitem(sys.modules, "azure.identity", types.SimpleNamespace(DefaultAzureCredential=Cred))
    monkeypatch.setitem(sys.modules, "azure.storage.blob", types.SimpleNamespace(BlobServiceClient=Svc))
    monkeypatch.setitem(sys.modules, "azure.keyvault.secrets", types.SimpleNamespace(SecretClient=Secrets))
    c = azure_api.AzureClients(SUB, ACCOUNT, VAULT)
    assert c.container == ("container", "groups") and c.container is c.container
    assert c.vault is c.vault
    assert ("blob", f"https://{ACCOUNT}.blob.core.windows.net") in built and (
        "kv",
        f"https://{VAULT}.vault.azure.net",
    ) in built
    assert c.arm.token() == "t" and ("token", "https://management.azure.com/.default") in built
