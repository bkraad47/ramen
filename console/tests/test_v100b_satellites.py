"""CONTRACTS §28 (v1.0.0b, D71): satellite clusters — zones in other regions, driven from the one console."""

import asyncio

import pytest

from ramen_console import regions
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_v100b_envs_zones import as_user, spy, worker

URL, ZONES = "/api/v1/config/regions", "/api/v1/zones"
SAT = {"provider": "gcp", "region": "australia-southeast1", "name": "ramen-au", "host": "au.example.net"}


def run(coro):
    return asyncio.run(coro)


# --- 28.1 regions.cluster_for / clean_clusters ---------------------------------------------------------------------
def test_cluster_for_resolves_home_then_satellites_then_none():
    assert regions.cluster_for("gcp", "us-central1-a", [SAT], "us-central1") == "home"
    assert regions.cluster_for("gcp", "us-central1", [], "us-central1") == "home"
    assert regions.cluster_for("gcp", "australia-southeast1-b", [SAT], "us-central1") == SAT
    assert regions.cluster_for("gcp", "australia-southeast1", [SAT], "us-central1") == SAT
    assert regions.cluster_for("gcp", "europe-west1-b", [SAT], "us-central1") is None
    assert regions.cluster_for("aws", "australia-southeast1-b", [SAT], "us-central1") is None  # another provider
    assert regions.cluster_for("gcp", "us-central12-a", [SAT], "us-central1") is None  # prefix, not substring
    assert regions.cluster_for("gcp", "us-central1-a", [SAT], None) is None  # no home (local adapter)


def test_clean_clusters_validates_and_normalises():
    out = regions.clean_clusters([{**SAT, "host": " https://au.example.net/ ", "project": " p2 "}])
    assert out == [{**SAT, "project": "p2"}]
    assert regions.clean_clusters([]) == []
    for bad, msg in [
        ({**SAT, "provider": "ibm"}, "provider"),
        ({**SAT, "region": "mars-1"}, "mars-1"),
        ({**SAT, "name": ""}, "name"),
        ({**SAT, "host": ""}, "host"),
    ]:
        with pytest.raises(ValueError, match=msg):
            regions.clean_clusters([bad])
    with pytest.raises(ValueError, match="australia-southeast1"):
        regions.clean_clusters([SAT, {**SAT, "name": "other"}])
    assert len(regions.clean_clusters([SAT, {**SAT, "provider": "aws", "region": "eu-west-1"}])) == 2


# --- 28.1 config/regions carries clusters; the 422s; zones carry cluster and host -------------------------------------
def gcp_console(demo, monkeypatch, home="us-central1"):  # noqa: F811
    monkeypatch.setenv("RAMEN_CLOUD", "gcp")
    monkeypatch.setattr(demo.app.state.cloud, "home_region", lambda: home, raising=False)


def test_the_clusters_card_round_trips_validates_and_refuses_to_drop_a_used_satellite(demo, monkeypatch):  # noqa: F811
    gcp_console(demo, monkeypatch)
    assert demo.get(URL).json()["clusters"] == [] and demo.get(URL).json()["home"] == "us-central1"
    r = demo.put(URL, json={"clusters": [SAT]})
    assert r.status_code == 200 and r.json()["clusters"] == [SAT] and r.json()["blocked"]["gcp"] == []
    assert demo.put(URL, json={"gcp": ["us-west1"]}).json()["clusters"] == [SAT]  # blocked and clusters coexist
    assert demo.get(URL).json() | {"regions": 0, "note": 0} == {
        "blocked": {"gcp": ["us-west1"], "aws": [], "azure": []},
        "clusters": [SAT],
        "home": "us-central1",
        "regions": 0,
        "note": 0,
    }
    assert demo.put(URL, json={"clusters": [{**SAT, "region": "mars-1"}]}).status_code == 422
    assert demo.put(URL, json={"clusters": [SAT, {**SAT, "name": "dup"}]}).status_code == 422
    r = demo.post(f"{URL}/clusters", json={"provider": "gcp", "region": "europe-west1", "name": "eu", "host": "eu.x"})
    assert r.status_code == 201 and [c["name"] for c in r.json()["clusters"]] == ["ramen-au", "eu"]
    assert (
        demo.post(ZONES, json={"name": "au", "provider": "gcp", "region": "australia-southeast1-b"}).status_code == 201
    )
    r = demo.delete(f"{URL}/clusters/ramen-au")
    assert r.status_code == 409 and "au" in r.json()["detail"]
    assert demo.put(URL, json={"clusters": []}).status_code == 409
    assert demo.delete(f"{URL}/clusters/eu").status_code == 200 and demo.delete(f"{URL}/clusters/eu").status_code == 404
    assert demo.delete(f"{ZONES}/au").status_code == 200
    assert demo.delete(f"{URL}/clusters/ramen-au").json()["clusters"] == []
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "config.clusters"]
    assert any("gcp:australia-southeast1:ramen-au@au.example.net" in a["tags"] for a in rows) and len(rows) >= 4
    ga = as_user(demo.app, demo, "ga@ramen.local", "group_admin", ["demo"])
    assert ga.put(URL, json={"clusters": []}).status_code == 403
    assert ga.post(f"{URL}/clusters", json=SAT).status_code == 403


def test_a_zone_needs_a_cluster_in_its_region_and_a_blocked_region_still_wins(demo, monkeypatch):  # noqa: F811
    gcp_console(demo, monkeypatch)
    r = demo.post(ZONES, json={"name": "au", "provider": "gcp", "region": "australia-southeast1-b"})
    assert r.status_code == 422 and "Config → Clusters" in r.json()["detail"] and "us-central1-f" in r.json()["detail"]
    demo.put(URL, json={"clusters": [SAT]})
    r = demo.post(ZONES, json={"name": "eu", "provider": "gcp", "region": "europe-west1-b"})
    assert r.status_code == 422 and "australia-southeast1" in r.json()["detail"] and "us-central1" in r.json()["detail"]
    r = demo.post(ZONES, json={"name": "au", "provider": "gcp", "region": "australia-southeast1-b"})
    assert r.status_code == 201 and r.json()["cluster"] == "ramen-au" and r.json()["host"] == "au.example.net"
    demo.put(URL, json={"gcp": ["australia-southeast1"]})  # §28.2: a blocked region wins over the satellite
    r = demo.post(ZONES, json={"name": "au2", "provider": "gcp", "region": "australia-southeast1-a"})
    assert r.status_code == 403 and "blocked" in r.json()["detail"]
    assert demo.put(f"{ZONES}/zone-a", json={"region": "australia-southeast1-a"}).status_code == 200  # local: unchecked
    rows = {z["id"]: z for z in demo.get(ZONES).json()}
    assert rows["au"]["cluster"] == "ramen-au" and rows["au"]["host"] == "au.example.net"
    assert rows["zone-a"]["cluster"] is None and rows["zone-a"]["host"] is None
    assert (
        demo.post(ZONES, json={"name": "home", "provider": "gcp", "region": "us-central1-a"}).json()["cluster"]
        == "home"
    )
    assert demo.get(f"{ZONES}/home").json() | {"created": 0} == {
        "id": "home",
        "name": "home",
        "provider": "gcp",
        "region": "us-central1-a",
        "environments": [],
        "groups": [],
        "cluster": "home",
        "host": None,
        "created": 0,
    }
    assert demo.get(f"{ZONES}?format=csv").text.splitlines()[0].startswith("cluster,created")


@pytest.mark.parametrize(
    "cloud_name, region, sat", [("aws", "eu-west-1a", "eu-west-1"), ("azure", "westeurope-1", "westeurope")]
)
def test_aws_and_azure_satellites_host_zones_too(demo, monkeypatch, cloud_name, region, sat):  # noqa: F811
    monkeypatch.setenv("RAMEN_CLOUD", cloud_name)
    monkeypatch.setattr(demo.app.state.cloud, "home_region", lambda: "us-east-1" if cloud_name == "aws" else "eastus")
    assert demo.post(ZONES, json={"name": "far", "provider": cloud_name, "region": region}).status_code == 422
    entry = {"provider": cloud_name, "region": sat, "name": f"ramen-{sat}", "host": f"{sat}.example.net"}
    assert demo.put(URL, json={"clusters": [entry]}).status_code == 200
    r = demo.post(ZONES, json={"name": "far", "provider": cloud_name, "region": region})
    assert r.status_code == 201 and r.json()["cluster"] == f"ramen-{sat}" and r.json()["host"] == entry["host"]


def test_a_region_change_across_clusters_detaches_then_attaches_and_deploys(demo, monkeypatch):  # noqa: F811
    gcp_console(demo, monkeypatch)
    demo.put(URL, json={"clusters": [SAT]})
    r = demo.post(ZONES, json={"name": "mv", "provider": "gcp", "region": "us-central1-a", "environments": ["prod"]})
    assert r.status_code == 201 and r.json()["groups"] == ["demo"]
    demo.put("/api/v1/groups/demo/zones/mv/workers", json={"count": 2, "max_count": 3})
    detached, attached = spy(demo, "detach_zone"), spy(demo, "attach_zone")
    r = demo.put(f"{ZONES}/mv", json={"region": "us-central1-b"})  # same cluster: D66 in place
    assert r.status_code == 200 and detached == [] and attached == [("demo", "mv")]
    r = demo.put(f"{ZONES}/mv", json={"region": "australia-southeast1-b"})  # another cluster: detach + attach
    assert r.status_code == 200 and r.json()["cluster"] == "ramen-au" and len(r.json()["jobs"]) == 1
    assert detached == [("demo", "mv")] and attached == [("demo", "mv"), ("demo", "mv")]
    w = worker(demo, "demo", "mv")
    assert w["count"] == 2 and w["max_count"] == 3  # the records survive the move
    assert demo.get(f"{ZONES}/mv").json()["region"] == "australia-southeast1-b"


# --- 28.1 pages: the location picker, the Zones table, the Clusters card -------------------------------------------
def test_the_pages_show_satellite_regions_clusters_and_the_clusters_card(demo, monkeypatch):  # noqa: F811
    gcp_console(demo, monkeypatch)
    page = demo.get("/environments").text
    assert "us-central1-f" in page and "australia-southeast1-b" not in page
    demo.put(URL, json={"clusters": [SAT]})
    demo.post(ZONES, json={"name": "au", "provider": "gcp", "region": "australia-southeast1-b"})
    demo.post(ZONES, json={"name": "home", "provider": "gcp", "region": "us-central1-a"})
    page = demo.get("/environments").text
    assert "australia-southeast1-b" in page and "europe-west1-b" not in page and "<th>Cluster</th>" in page
    assert "ramen-au" in page and "au.example.net" in page and ">home<" in page
    cfg = demo.get("/config").text
    assert '<h2 style="margin-top:0">Clusters</h2>' in cfg and 'hx-post="/api/v1/config/regions/clusters"' in cfg
    assert 'hx-delete="/api/v1/config/regions/clusters/ramen-au"' in cfg and "au.example.net" in cfg
    assert 'name="region"' in cfg and 'name="host"' in cfg


# --- 28.1 clients reach a zone at its cluster's host ----------------------------------------------------------------
def test_the_sign_in_guide_names_each_zones_host():
    from types import SimpleNamespace

    from ramen_console.web import signin_guide as g

    auth = SimpleNamespace(
        role_map={}, role_claim={}, source_of=lambda n: "claim", password_login=True, magic_link=False
    )
    clients = [{"client_id": "b1", "name": "bridge", "redirect_uris": ["http://127.0.0.1/cb"]}]
    out = g.build("https://edge.example", "demo", ["a", "au"], clients, auth, [], ["google"], {"au": "au.example.net"})
    assert out["hosts"] == {"a": "edge.example", "au": "au.example.net"}
    by = {b["label"]: b["text"] for b in out["key_boxes"]}
    assert (
        "https://edge.example/mcp" in by["Claude Code · zone a"]
        and "edge.example:443" in by["MCP bridge — run it yourself (any stdio client) · zone a"]
    )
    assert (
        "https://au.example.net/mcp" in by["Claude Code · zone au"]
        and "edge.example" not in by["Claude Code · zone au"]
    )
    assert "au.example.net:443" in by["Claude Code — through the bridge (stdio) · zone au"]
    oauth = {b["label"]: b["text"] for b in out["account_boxes"]}
    au = oauth["Claude Code — Streamable HTTP · zone au"]
    assert '"url": "https://au.example.net/mcp"' in au and "mcp:demo:au" in au  # the token and scope stay the console's
    assert "--oauth https://edge.example" in oauth["Claude Code — through the bridge (stdio) · zone au"]
    assert "--target au.example.net:443" in oauth["Claude Code — through the bridge (stdio) · zone au"]
    assert g.build("https://edge.example", "demo", [], clients, auth, [], [])["hosts"] == {"<zone>": "edge.example"}


def test_the_group_and_mcp_user_pages_name_a_satellite_host(demo, monkeypatch):  # noqa: F811
    gcp_console(demo, monkeypatch)
    demo.put(URL, json={"clusters": [SAT]})
    r = demo.post(
        ZONES, json={"name": "au", "provider": "gcp", "region": "australia-southeast1-b", "environments": ["prod"]}
    )
    assert r.status_code == 201
    html = demo.get("/groups/demo").text
    assert '<code>au</code> <small class="muted">at <code>au.example.net</code></small>' in html
    assert "https://au.example.net/mcp" in html and "testserver/mcp" in html
    assert "<code>zone-a</code> <small" not in html  # the home zones carry no host note
    mu = as_user(demo.app, demo, "mu@ramen.local", "mcp_user", ["demo"]).get("/").text
    assert "au.example.net" in mu and ">au<" in mu
    assert "au.example.net" not in as_user(demo.app, demo, "mo@ramen.local", "mcp_user", ["other"]).get("/").text


# --- 28.2 adapters: every kube call goes to the zone's cluster (fakes: two clusters) ------------------------------
SAT_ZONE, HOME_ZONE = "australia-southeast1-b", "us-central1-a"
NS_FAR, NS_NEAR = ("Namespace", None, "ramen-demo-far"), ("Namespace", None, "ramen-demo-near")


class Lookup:
    """What `services.ClusterLookup` answers, without a store: one satellite, zones by name."""

    def __init__(self, sat, zones):
        self.sat, self.zones = sat, zones

    async def __call__(self, region):
        return self.sat if regions.cluster_for(self.sat["provider"], region, [self.sat], None) else "home"

    async def zone_region(self, zone):
        return self.zones[zone]

    async def clusters(self):
        return [self.sat]


@pytest.fixture
def grpc_worker():
    from tests.fake_grpc import FakeWorker

    w = FakeWorker(admin_key="adm-key", mcp_keys=("rmk_1",)).start()
    yield w
    w.stop()


def gcp_pair(grpc_worker):
    from ramen_console.cloud.gcp import GcpCloud
    from ramen_console.grpcclient import Client
    from tests.fakes_gcp import FakeClients

    fk = FakeClients()
    cloud = GcpCloud("p1", "us-central1", "p1-groups", "img/worker:1", "adm-key", clients=fk, wait_secs=1, poll=0)
    cloud.rpc = Client(deadline=2, resolve=grpc_worker.resolve)
    return cloud, fk, SAT, {"far": SAT_ZONE, "near": HOME_ZONE}


def aws_pair(grpc_worker):
    from ramen_console.cloud.aws import AwsCloud
    from ramen_console.grpcclient import Client
    from tests.fakes_aws import BUCKET, FakeAwsClients

    fk = FakeAwsClients().seed()
    cloud = AwsCloud("us-east-1", BUCKET, "img/worker:1", "adm-key", clients=fk, wait_secs=1, poll=0)
    cloud.rpc, cloud.split_drain_secs = Client(deadline=2, resolve=grpc_worker.resolve), 0
    sat = {"provider": "aws", "region": "eu-west-1", "name": "ramen-eu", "host": "eu.example.net"}
    return cloud, fk, sat, {"far": "eu-west-1a", "near": "us-east-1a"}


def azure_pair(grpc_worker):
    from ramen_console.cloud.azure import AzureCloud
    from ramen_console.grpcclient import Client
    from tests.fakes_azure import ACCOUNT, ISSUER, LOCATION, RG, SUB, VAULT, FakeAzureClients

    fk = FakeAzureClients()
    cloud = AzureCloud(SUB, RG, LOCATION, ACCOUNT, VAULT, ISSUER, "acr/worker:1", "adm-key", clients=fk, wait_secs=1)
    cloud.rpc = Client(deadline=2, resolve=grpc_worker.resolve)
    sat = {
        "provider": "azure",
        "region": "westeurope",
        "name": "ramen-weu",
        "host": "weu.example.net",
        "project": "weu-rg",
    }
    return cloud, fk, sat, {"far": "westeurope-1", "near": "eastus-1"}


def _run_pair(make, grpc_worker):
    cloud, fk, sat, zones = make(grpc_worker)
    cloud.set_cluster_lookup(Lookup(sat, zones))
    far, near = {"region": zones["far"], "size": "s", "count": 1}, {"region": zones["near"], "size": "s", "count": 1}
    cfg = {
        "RAMEN_MCP_KEYS": "rmk_1",
        "RAMEN_PUBLIC_URL": "https://c.example",
        "RAMEN_OAUTH_ISSUER": "https://c.example",
    }

    def secret(k8s, ns):
        return k8s.objs[("Secret", ns, "ramen-deploy")]["stringData"]

    async def go():
        assert fk.satellites == {}
        r = await cloud.attach_zone("demo", "far", far)
        assert r["ok"] and r["namespace"] == "ramen-demo-far"
        sk = fk.satellites[sat["name"]].k8s
        assert NS_FAR in sk.objs and NS_FAR not in fk.k8s.objs
        await cloud.attach_zone("demo", "near", near)
        assert NS_NEAR in fk.k8s.objs and NS_NEAR not in sk.objs
        res = await cloud.deploy("demo", "prod", "far", canary=True, config=cfg, spec=far)
        assert res["ok"], res
        sec = secret(sk, "ramen-demo-far")
        assert sec["RAMEN_PUBLIC_URL"] == f"https://{sat['host']}" and sec["RAMEN_OAUTH_ISSUER"] == "https://c.example"
        res = await cloud.deploy("demo", "prod", "near", canary=False, config=cfg, spec=near)
        assert res["ok"] and secret(fk.k8s, "ramen-demo-near")["RAMEN_PUBLIC_URL"] == "https://c.example"
        assert any(w["id"].startswith("worker") for w in await cloud.workers("demo", "far"))
        dep = ("Deployment", "ramen-demo-far", "worker")
        assert dep in sk.objs and dep not in fk.k8s.objs
        assert (await cloud.zone_runtime("demo", "far"))["workers"] >= 1
        await cloud.push_config("demo", "prod", "far", {**cfg, "RAMEN_VERBOSE": "1"})
        assert secret(sk, "ramen-demo-far")["RAMEN_VERBOSE"] == "1"
        ip = await cloud.set_ip_rules("demo", "far", ["10.0.0.0/8"])
        assert ip["ok"] and secret(sk, "ramen-demo-far")["RAMEN_ALLOWED_CIDRS"] == "10.0.0.0/8"
        assert (await cloud.rebalance("demo", "far"))["ok"]
        by = {z["zone"]: z for z in (await cloud.refresh())["zones"]}
        assert by["far"]["cluster"] == sat["name"] and "cluster" not in by["near"]
        gone = await cloud.detach_zone("demo", "far")
        assert gone["namespaces"] == ["ramen-demo-far"] and NS_FAR not in sk.objs and NS_NEAR in fk.k8s.objs
        await cloud.attach_zone("demo", "far", far)
        gone = await cloud.detach_group("demo")
        assert sorted(gone["namespaces"]) == ["ramen-demo-far", "ramen-demo-near"]
        assert not [k for k in [*sk.objs, *fk.k8s.objs] if k[0] == "Namespace"]
        return cloud, fk, sk

    return run(go())


def test_gcp_routes_every_kube_call_to_the_zones_cluster(grpc_worker):
    cloud, fk, sk = _run_pair(gcp_pair, grpc_worker)
    assert fk.satellites["ramen-au"].cluster == ("p1", "australia-southeast1", "ramen-au")
    assert sorted(cloud._kubes) == ["home", "ramen-au"] and cloud.kube_for("home") is cloud._kubes["home"]


def test_aws_routes_kube_calls_regional_objects_logs_and_the_oidc_trust_to_the_zones_cluster(grpc_worker):
    import json

    from tests.fakes_aws import aws_env

    with aws_env():
        cloud, fk, sk = _run_pair(aws_pair, grpc_worker)
        sat = fk.satellites["ramen-eu"]
        assert sat.region == "eu-west-1" and sat.eks.describe_cluster(name="ramen-eu")["cluster"]["name"] == "ramen-eu"
        assert not fk.iam.list_roles(PathPrefix="/ramen/")["Roles"]  # detach_group took the zone roles
        run(cloud.create_service_account("demo", "far"))
        run(cloud.create_service_account("demo", "near"))
        trust = {
            n: json.dumps(fk.iam.get_role(RoleName=n)["Role"]["AssumeRolePolicyDocument"])
            for n in ("ramen-demo-far", "ramen-demo-near")
        }

        def issuer(c, name):
            return c.eks.describe_cluster(name=name)["cluster"]["identity"]["oidc"]["issuer"].removeprefix("https://")

        far_issuer, home_issuer = issuer(sat, "ramen-eu"), issuer(fk, "ramen")
        assert far_issuer in trust["ramen-demo-far"] and home_issuer not in trust["ramen-demo-far"]
        assert home_issuer in trust["ramen-demo-near"]
        run(cloud.logs("demo", "far"))  # the satellite region's Insights client and log group
        assert sat.logs.queries[-1]["logGroupName"] == "/aws/containerinsights/ramen-eu/application"
        assert not fk.logs.queries


def test_azure_routes_every_kube_call_to_the_zones_cluster_and_federates_its_issuer(grpc_worker):
    cloud, fk, sk = _run_pair(azure_pair, grpc_worker)
    sat = fk.satellites["ramen-weu"]
    assert sat.resource_group == "weu-rg" and sat.arm is fk.arm
    run(cloud.create_service_account("demo", "far"))
    run(cloud.create_service_account("demo", "near"))
    fics = {k[0]: v for k, v in fk.arm.fics.items()}
    assert fics["ramen-demo-far"]["properties"]["issuer"] == "https://oidc.example/ramen-weu/"
    assert fics["ramen-demo-near"]["properties"]["issuer"] == "https://oidc.example/issuer/"
    run(
        cloud.attach_zone("demo", "far", {"region": "westeurope-1", "size": "s", "count": 1})
    )  # detach_group emptied it
    sk.pod_logs[("ramen-demo-far", "worker-0")] = ["hello from weu"]
    assert "hello from weu" in run(cloud.logs("demo", "far"))


def test_without_a_lookup_everything_is_the_home_cluster(grpc_worker):
    cloud, fk, sat, zones = gcp_pair(grpc_worker)
    run(cloud.attach_zone("demo", "far", {"region": SAT_ZONE, "size": "s", "count": 1}))
    assert NS_FAR in fk.k8s.objs and fk.satellites == {}
    ref = run(cloud.refresh())
    assert ref["zones"] and all("cluster" not in z for z in ref["zones"])


# --- 28.2 the satellite kube clients: GKE API + ADC, EKS + presigned STS, AKS credentials + AAD token --------------
def test_gke_satellite_client_builds_from_the_container_api_and_adc(monkeypatch):
    import base64
    from types import SimpleNamespace as NS

    from ramen_console.cloud import gcp_clients

    seen = {}

    def get(name):
        seen["name"] = name
        ca = base64.b64encode(b"CA").decode()
        return NS(execute=lambda: {"endpoint": "10.1.2.3", "masterAuth": {"clusterCaCertificate": ca}})

    disc = NS(projects=lambda: NS(locations=lambda: NS(clusters=lambda: NS(get=get))))
    home = gcp_clients.GcpClients("p1")
    monkeypatch.setattr(home, "_discovery", lambda n, v: disc)
    creds = NS(valid=False, token=None)

    def refresh(req):
        creds.valid, creds.token = True, "tok-1"

    creds.refresh = refresh
    monkeypatch.setattr(gcp_clients, "_adc", lambda: creds)
    sat = home.for_cluster("p2", "australia-southeast1", "ramen-au")
    assert isinstance(sat, gcp_clients.GkeClients) and sat.compute is disc  # the home bundle's planes
    cfg = sat.core.api_client.configuration
    assert seen["name"] == "projects/p2/locations/australia-southeast1/clusters/ramen-au"
    assert cfg.host == "https://10.1.2.3" and cfg.get_api_key_with_prefix("authorization") == "Bearer tok-1"
    assert open(cfg.ssl_ca_cert, "rb").read() == b"CA" and sat.apps.api_client is sat.core.api_client
    monkeypatch.setattr(gcp_clients, "_adc", lambda: None)
    with pytest.raises(RuntimeError, match="no Google credentials"):
        gcp_clients.google_token()


def test_eks_satellite_client_presigns_an_sts_token():
    import base64

    from ramen_console.cloud.aws_api import AwsClients, eks_token
    from tests.fakes_aws import ACCOUNT, aws_env

    with aws_env():
        import boto3

        eks = boto3.client("eks", region_name="eu-west-1")
        eks.create_cluster(
            name="ramen-eu", roleArn=f"arn:aws:iam::{ACCOUNT}:role/eks", resourcesVpcConfig={"subnetIds": ["s"]}
        )
        sat = AwsClients("us-east-1").for_cluster("eu-west-1", "ramen-eu")
        cfg = sat.core.api_client.configuration
        assert cfg.host.endswith("eu-west-1.eks.amazonaws.com/") and sat.region == "eu-west-1"
        tok = cfg.get_api_key_with_prefix("authorization")
        assert tok.startswith("Bearer k8s-aws-v1.")
        url = base64.urlsafe_b64decode(tok.split("k8s-aws-v1.")[1] + "==").decode()
        assert url.startswith("https://sts.eu-west-1.amazonaws.com/?Action=GetCallerIdentity") and "x-k8s-aws-id" in url
        assert eks_token(sat.sts, "eu-west-1", "ramen-eu").startswith("k8s-aws-v1.")


def test_aks_satellite_client_takes_server_and_ca_from_the_kubeconfig_and_an_aad_token():
    from types import SimpleNamespace as NS

    from ramen_console.cloud.azure_api import AKS_AUDIENCE, AzureClients
    from tests.fakes_azure import ACCOUNT, SUB, VAULT, FakeArm

    home = AzureClients(SUB, ACCOUNT, VAULT)
    scopes = []
    home._c["credential"] = NS(get_token=lambda scope: (scopes.append(scope), NS(token="aad-tok"))[1])
    home._c["arm"] = FakeArm()
    sat = home.for_cluster("weu-rg", "ramen-weu")
    cfg = sat.core.api_client.configuration
    assert cfg.host == "https://ramen-weu.hcp.westeurope.azmk8s.io:443"
    assert cfg.get_api_key_with_prefix("authorization") == "Bearer aad-tok" and set(scopes) == {
        f"{AKS_AUDIENCE}/.default"
    }
    assert sat.oidc_issuer == "https://oidc.example/ramen-weu/" and sat.arm is home.arm and sat.vault is home.vault
    assert sat.path.endswith("/resourceGroups/weu-rg/providers/Microsoft.ContainerService/managedClusters/ramen-weu")


def test_the_console_identity_and_source_networks_reach_every_zone_namespace(monkeypatch):
    """§28: on a satellite the console is a User/Group, not the home ServiceAccount, and it arrives from the home
    network — both are handed in by the chart and land in every RoleBinding and every satellite worker NetworkPolicy."""
    from ramen_console.cloud.gcp_k8s import networkpolicy, rolebinding, with_console_sources

    monkeypatch.delenv("RAMEN_CONSOLE_SUBJECTS", raising=False)
    monkeypatch.delenv("RAMEN_CONSOLE_SOURCE_CIDRS", raising=False)
    assert rolebinding("demo", "a")["subjects"] == [
        {"kind": "ServiceAccount", "name": "console", "namespace": "ramen-system"}
    ]
    peers = networkpolicy("ramen-demo-a", "demo", "a")["spec"]["ingress"][0]["from"]
    assert [p for p in peers if "ipBlock" in p] == [
        {"ipBlock": {"cidr": "35.191.0.0/16"}},
        {"ipBlock": {"cidr": "130.211.0.0/22"}},
    ]
    monkeypatch.setenv(
        "RAMEN_CONSOLE_SUBJECTS",
        '[{"kind":"Group","name":"ramen-console"},{"kind":"User","name":"ramen-console@p.iam.gserviceaccount.com"},{"kind":"Role","name":"x"}]',
    )
    monkeypatch.setenv("RAMEN_CONSOLE_SOURCE_CIDRS", "10.7.128.0/17, 10.42.0.0/16")
    subj = rolebinding("demo", "a")["subjects"]
    assert subj[1:] == [
        {"kind": "Group", "name": "ramen-console"},
        {"kind": "User", "name": "ramen-console@p.iam.gserviceaccount.com"},
    ]
    # S6: a satellite zone's policy only, in its own rule (tests/test_sec_gaps_netpol.py)
    peers = with_console_sources([networkpolicy("ramen-demo-a", "demo", "a")], True)[0]["spec"]["ingress"][-1]["from"]
    assert {"ipBlock": {"cidr": "10.7.128.0/17"}} in peers and {"ipBlock": {"cidr": "10.42.0.0/16"}} in peers
