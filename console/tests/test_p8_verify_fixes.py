"""Phase 8 docs verification (§34.3) code findings: N1 credentials in shown/logged values, N5 satellite RBAC, the
satellite console URL (§39.5a), N6–N9 stale text."""

import asyncio
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ramen_console import llm_providers as lp
from ramen_console import otel, redact, security
from tests.test_api import app, client, cloud, demo, root  # noqa: F401 - fixtures

ROOT = Path(__file__).resolve().parents[2]
PW = "redis-pw-not-real-0042"
REDIS = f"rediss://:{PW}@redis.ramen-system:6379/0"


def run(coro):
    return asyncio.run(coro)


# --- N1: the shared redaction --------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, shown",
    [
        (REDIS, "rediss://***@redis.ramen-system:6379/0"),
        ("redis://default:pw@host:6379", "redis://***@host:6379"),
        ("postgresql://u:s3cr3t@db/ramen", "postgresql://***@db/ramen"),
        ("https://ghp_tokentokentoken@github.com/o/r", "https://***@github.com/o/r"),
        ("rediss://:p@ss@host:6380", "rediss://***@host:6380"),  # an unencoded @ in the password
        ("https://h/v1?api_key=abc&model=x&token=t#f", "https://h/v1?api_key=***&model=x&token=***#f"),
        ("https://h/x?sig=1&X-Amz-Signature=2&password=3", "https://h/x?sig=***&X-Amz-Signature=***&password=***"),
        ("https://phoenix.ramen-system:4318", "https://phoenix.ramen-system:4318"),
        ("user@example.com", "user@example.com"),
        ("", ""),
    ],
)
def test_redact_masks_userinfo_and_secret_query_params(raw, shown):
    assert redact.redact(raw) == shown


def test_redact_finds_urls_inside_text():
    text = f"connect {REDIS} failed; retry https://h/?secret=s ok"
    shown = "connect rediss://***@redis.ramen-system:6379/0 failed; retry https://h/?secret=*** ok"
    assert redact.redact(text) == shown


def test_keep_maps_a_masked_round_trip_back_to_the_stored_value():
    assert redact.keep(redact.redact(REDIS), REDIS) == REDIS
    assert redact.keep("https://new.example", REDIS) == "https://new.example"
    assert redact.keep("", REDIS) == ""
    assert redact.keep(None, REDIS) is None
    assert redact.keep("x", None) == "x"


def test_masked_env_hides_a_url_password_under_any_name(monkeypatch):
    from ramen_console.web.api_admin import masked_env

    monkeypatch.setenv("RAMEN_REDIS_URL", REDIS)
    monkeypatch.setenv("RAMEN_SOME_ENDPOINT", "https://h/v1?token=abc")
    monkeypatch.setenv("RAMEN_SESSION_SECRET", "s3cr3t-value")
    env = masked_env()
    assert env["RAMEN_REDIS_URL"] == "rediss://***@redis.ramen-system:6379/0"
    assert env["RAMEN_SOME_ENDPOINT"] == "https://h/v1?token=***"
    assert env["RAMEN_SESSION_SECRET"] == "***"


def test_config_api_and_page_never_show_the_redis_password(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_REDIS_URL", REDIS)
    api = demo.get("/api/v1/config")
    assert api.status_code == 200 and PW not in api.text and "redis.ramen-system" in api.text
    page = demo.get("/config")
    assert page.status_code == 200 and PW not in page.text and "rediss://***@redis.ramen-system" in page.text


# --- N1: the URL fields of telemetry and LLM providers -------------------------------------------------------------
class Store:
    def __init__(self):
        self.docs = {}

    async def get(self, coll, key):
        return self.docs.get((coll, key))

    async def put(self, coll, key, doc):
        self.docs[(coll, key)] = doc
        return doc


def test_telemetry_endpoints_are_shown_masked_and_a_masked_save_keeps_the_stored_url():
    s = Store()
    url = "https://u:collector-pw@otel.example:4318"
    run(otel.set_config(s, enabled=True, endpoint=url, client_endpoint=url))
    pub = otel.public_config(run(otel.get_config(s)))
    assert pub["endpoint"] == pub["client_endpoint"] == "https://***@otel.example:4318"
    run(otel.set_config(s, endpoint=pub["endpoint"], client_endpoint=pub["client_endpoint"]))  # the form posts it back
    cfg = run(otel.get_config(s))
    assert cfg["endpoint"] == cfg["client_endpoint"] == url
    run(otel.set_config(s, endpoint="https://other.example:4318"))
    assert run(otel.get_config(s))["endpoint"] == "https://other.example:4318"


def test_telemetry_audit_tag_carries_no_password(demo):
    r = demo.put("/api/v1/config/otel", json={"enabled": True, "endpoint": "https://u:otel-pw-x@otel.example:4318"})
    assert r.status_code == 200, r.text
    assert "otel-pw-x" not in r.text
    assert "otel-pw-x" not in demo.get("/api/v1/audit?limit=50").text


def test_llm_base_urls_are_shown_masked_and_a_masked_save_keeps_the_stored_url():
    url = "https://gw:gw-pw-1@llm.example/v1"
    cfg = lp.merge({}, {"openai_compatible": {"base_url": url}})
    shown = lp.public(cfg)["openai_compatible"]["base_url"]
    assert shown == "https://***@llm.example/v1"
    assert lp.merge(cfg, {"openai_compatible": {"base_url": shown}})["openai_compatible"]["base_url"] == url
    assert lp.merge(cfg, {"openai_compatible": {"base_url": ""}}) == {}


# --- N1: logs, job records, worker logs ----------------------------------------------------------------------------
def test_scrub_masks_query_secrets_and_remembers_url_passwords():
    pw = "bare-url-pw-not-real-77"
    security.remember_secret(f"rediss://:{pw}@redis.x:6379")
    assert security.scrub(f"WRONGPASS {pw}") == "WRONGPASS ***"
    assert security.scrub("GET https://h/x?api_key=k1 200") == "GET https://h/x?api_key=*** 200"


def test_the_console_remembers_the_redis_password_from_its_env_at_start(app, monkeypatch):  # noqa: F811
    pw = "env-redis-pw-not-real-91"
    monkeypatch.setenv("RAMEN_REDIS_URL", f"rediss://:{pw}@redis.ramen-system:6379")
    security.remember_env_secrets()
    assert security.scrub(f"auth failed for {pw}") == "auth failed for ***"


def test_a_job_record_keeps_no_secret_in_its_error(app):  # noqa: F811
    from ramen_console.deploy import Jobs

    jobs = Jobs(app.state.store)

    async def go():
        job = await jobs.create("deploy", "demo/dev")
        job.update(status="error", error=f"ConnectionError: {REDIS}")
        job["result"] = {"a": {"ok": False, "error": f"redis {REDIS}", "evals": {"error": f"at {REDIS}"}}}
        await jobs.save(job)
        return await app.state.store.get("jobs", job["id"])

    doc = run(go())
    assert PW not in str(doc) and "rediss://***@redis.ramen-system" in doc["error"]


def test_worker_logs_are_scrubbed_before_anyone_sees_them(demo, tmp_path):
    p = tmp_path / "logs" / "demo" / "zone-a" / "worker.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f'{{"msg": "redis connect {REDIS} failed"}}\n')
    api = demo.get("/api/v1/logs?group=demo&zone=zone-a")
    assert api.status_code == 200 and PW not in api.text and "rediss://***@" in api.text
    assert PW not in demo.get("/logs?group=demo&zone=zone-a").text


# --- N8: the cluster card is "Clusters" / "Add satellite" ----------------------------------------------------------
def test_texts_name_the_clusters_card_not_regions():
    svc = (ROOT / "console/src/ramen_console/services.py").read_text()
    notes = (ROOT / "deploy/helm/ramen-satellite/templates/NOTES.txt").read_text()
    assert "Config → Regions" not in svc + notes
    assert "Config → Clusters" in svc and "Config → Clusters → Add satellite" in notes
    for tf in ("gcp", "aws", "azure"):
        d = ROOT / f"deploy/terraform/{tf}-satellite"
        text = "".join(f.read_text() for f in [*d.glob("*.tf"), *d.glob("README.md")])
        assert "Config → Regions" not in text, tf


# --- N6 / N9: chart comments and the deploy workflow -----------------------------------------------------------------
def test_values_comments_match_terraform_and_d86():
    v = (ROOT / "deploy/helm/ramen/values.yaml").read_text()
    assert "output service_cidr" not in v and "172.20.0.0/16" in v
    assert "`redis://:<password>" not in v and "`rediss://:<password>" in v


def test_no_workflow_says_jobs_live_in_memory():
    paths = [*(ROOT / ".github/workflows").glob("*.y*ml"), ROOT / "docs/wiki/deploy-github-actions.md"]
    demo_wf = ROOT.parent / "ramen-demo-mcp/.github/workflows/ramen-deploy.yml"
    if demo_wf.exists():
        paths.append(demo_wf)
    for p in paths:
        assert "console's memory" not in p.read_text(), p


# --- N5: the satellite chart's console roles carry the D86 objects --------------------------------------------------
def sat_template(*sets):
    import subprocess

    import yaml

    flags = [x for s in sets for x in ("--set", s)]
    r = subprocess.run(
        ["helm", "template", "s", str(ROOT / "deploy/helm/ramen-satellite"), "-n", "ramen-system", *flags],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr
    return [d for d in yaml.safe_load_all(r.stdout) if d]


def rules_of(docs, name):
    return next(d for d in docs if d["kind"] == "ClusterRole" and d["metadata"]["name"] == name)["rules"]


@pytest.mark.skipif(__import__("shutil").which("helm") is None, reason="helm not installed")
@pytest.mark.parametrize("provider", ["gcp", "aws", "azure"])
def test_satellite_zone_role_writes_the_ca_configmap_and_azure_its_backend_tls(provider):
    sets = [f"provider={provider}", "region=r1", "project=p", "console.identity=c", "gateway.certificateMap=m"]
    sets += ["aws.certificateArn=arn:x", "azure.publicHostname=h.sslip.io", "azure.nodeResourceGroup=mc"]
    docs = sat_template(*sets)
    zone = rules_of(docs, "ramen-console-zone")
    assert any("configmaps" in r["resources"] and "create" in r["verbs"] for r in zone if r["apiGroups"] == [""])
    btp = [r for r in rules_of(docs, "ramen-console") if "backendtlspolicies" in r["resources"]]
    if provider == "azure":
        assert btp and btp[0]["apiGroups"] == ["gateway.networking.k8s.io"] and "create" in btp[0]["verbs"]
    else:
        assert not btp


# --- §39.5a: satellite workers reach the console at its public URL ---------------------------------------------------
def test_zone_cluster_answers_home_or_the_satellite(demo, monkeypatch):
    from tests.test_v100b_satellites import SAT, gcp_console

    gcp_console(demo, monkeypatch)
    assert demo.put("/api/v1/config/regions", json={"clusters": [SAT]}).status_code == 200
    s, store = demo.app.state.services, demo.app.state.store
    z = run(store.get("zones", "zone-a"))
    run(store.put("zones", "zone-a", {**z, "provider": "gcp", "region": "australia-southeast1-b"}))
    assert run(s.zone_cluster("zone-a")) == SAT
    run(store.put("zones", "zone-a", {**z, "provider": "gcp", "region": "us-central1-a"}))
    assert run(s.zone_cluster("zone-a")) == "home"
    assert run(s.zone_cluster("nope")) == "home"


def test_the_wake_answers_through_an_edge_only_for_a_satellite_zone(demo, monkeypatch):
    from tests.test_v100b_satellites import SAT

    edge = {"x-ramen-admin-key": "adm", "X-Forwarded-For": "203.0.113.9"}
    url = "/api/v1/internal/zones/demo/zone-a/wake"
    with TestClient(demo.app) as pod:
        assert pod.post(url, headers=edge, json={"trigger": "call"}).status_code == 404

        async def far(zone):
            return SAT if zone == "zone-a" else "home"

        monkeypatch.setattr(demo.app.state.services, "zone_cluster", far)
        assert pod.post(url, headers={**edge, "x-ramen-admin-key": "nope"}).status_code == 401
        assert pod.post(url, headers=edge, json={"trigger": "call"}).status_code == 200
        assert pod.post(url.replace("zone-a", "zone-b"), headers=edge).status_code == 404


@pytest.fixture
def grpc_worker():
    from tests.fake_grpc import FakeWorker

    w = FakeWorker(admin_key="adm-key", mcp_keys=("rmk_1",)).start()
    yield w
    w.stop()


def test_a_satellite_zone_gets_the_public_judge_url_and_wakes_through_the_public_url(grpc_worker, monkeypatch):
    from ramen_console.cloud.gcp_k8s import ACTIVATOR
    from tests.test_v100b_satellites import Lookup, gcp_pair

    internal = "https://console.ramen-system.svc.cluster.local:443"
    monkeypatch.setenv("RAMEN_CONSOLE_INTERNAL_URL", internal)
    cloud, fk, sat, zones = gcp_pair(grpc_worker)
    cloud.set_cluster_lookup(Lookup(sat, zones))
    cfg = {
        "RAMEN_MCP_KEYS": "rmk_1",
        "RAMEN_OAUTH_ISSUER": "https://c.example",
        "RAMEN_LLM_PROVIDER": "openai_compatible",
        "RAMEN_LLM_BASE_URL": f"{internal}/llm/v1",
    }

    async def go():
        for z in ("far", "near"):
            spec = {"region": zones[z], "size": "s", "count": 1}
            await cloud.attach_zone("demo", z, spec)
            assert (await cloud.deploy("demo", "prod", z, canary=False, config=cfg, spec=spec))["ok"]
        await cloud.sleep_zone("demo", "far")
        await cloud.sleep_zone("demo", "near")

    run(go())
    sk = fk.satellites[sat["name"]].k8s

    def secret(k8s, ns):
        return k8s.objs[("Secret", ns, "ramen-deploy")]["stringData"]

    def wake_url(k8s, ns):
        env = k8s.objs[("Deployment", ns, ACTIVATOR)]["spec"]["template"]["spec"]["containers"][0]["env"]
        return next(e["value"] for e in env if e["name"] == "RAMEN_CONSOLE_INTERNAL_URL")

    assert secret(sk, "ramen-demo-far")["RAMEN_LLM_BASE_URL"] == "https://c.example/llm/v1"
    assert secret(fk.k8s, "ramen-demo-near")["RAMEN_LLM_BASE_URL"] == f"{internal}/llm/v1"
    assert wake_url(sk, "ramen-demo-far") == "https://c.example"
    assert wake_url(fk.k8s, "ramen-demo-near") == internal
