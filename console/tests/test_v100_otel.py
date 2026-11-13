"""CONTRACTS §24.3: OpenTelemetry — the settings (write-only headers), what workers and agents are told, consent on
the OAuth page and in the token, the deploy span, and the console's own worker calls passing the consent gate."""

import asyncio
import base64
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console import otel
from ramen_console.grpcclient import Client
from ramen_console.storage.encrypted import SENSITIVE
from tests.stub_cloud import StubCloud
from tests.test_api import PW, app, client, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import wait_job
from tests.test_deploy_gates import manifest, write_cases
from tests.test_oauth_server import CLIENT, authorize, consent, exchange, node_verify, pkce, refresh, register

CFG = "/api/v1/config/otel"
DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
ON = {
    "enabled": True,
    "endpoint": "http://jaeger.ramen-system:4318/",
    "headers": {"Authorization": "Bearer ramen-export-s3cret"},
    "client_endpoint": "https://otel.example.com:4318",
    "client_headers": {"x-team": "agents-t0ken"},
}


@pytest.fixture
def cloud(tmp_path, workers, monkeypatch):
    c = StubCloud(
        tmp_path / "buckets",
        tmp_path / "logs",
        {"demo/zone-a": [workers["cold"].target], "demo/zone-b": [workers["hot"].target]},
        workers["cold"].target,
        "adm",
        Client(deadline=2),
    )

    async def sync(group, repo_url, ref, token):
        return "/tmp/fake"

    monkeypatch.setattr(c, "sync_repo", sync)
    return c


@pytest.fixture
def collector(monkeypatch):
    seen = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(200, json={})

    monkeypatch.setattr(otel, "TRANSPORT", httpx.MockTransport(handler))
    return seen


def deploy(c):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])


def claims(token):
    return json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "=="))


# settings ---------------------------------------------------------------------------------------------------------


def test_the_settings_round_trip_and_header_values_never_come_back(demo):
    assert demo.get(CFG).json() == {
        "enabled": False,
        "endpoint": "",
        "client_endpoint": "",
        "headers_set": False,
        "header_names": [],
        "client_headers_set": False,
        "client_header_names": [],
    }
    assert demo.put(CFG, json={"enabled": True}).status_code == 422  # no endpoint yet
    assert demo.put(CFG, json={"endpoint": "ftp://x"}).status_code == 422
    assert demo.put(CFG, json={"endpoint": "http://x/?a=1"}).status_code == 422
    assert demo.put(CFG, json={"headers": {"bad name": "x"}}).status_code == 422
    assert demo.put(CFG, json={"headers": "not json"}).status_code == 422
    assert demo.put(CFG, json={"headers": "[1]"}).status_code == 422
    r = demo.put(CFG, json=ON)
    assert r.status_code == 200, r.text
    want = {
        "enabled": True,
        "endpoint": "http://jaeger.ramen-system:4318",
        "client_endpoint": "https://otel.example.com:4318",
        "headers_set": True,
        "header_names": ["Authorization"],
        "client_headers_set": True,
        "client_header_names": ["x-team"],
    }
    assert r.json() == want and demo.get(CFG).json() == want
    for text in (r.text, demo.get(CFG).text, demo.get("/config").text, demo.get("/api/v1/audit?limit=50").text):
        assert "s3cret" not in text and "t0ken" not in text
    # the form posts strings: an empty header field means unchanged, JSON text sets, {} clears
    r = demo.put(CFG, json={"headers": "", "client_headers": '{"x-team": "b"}', "endpoint": "http://c:4318"})
    assert r.json()["headers_set"] and r.json()["client_header_names"] == ["x-team"]
    assert demo.put(CFG, json={"client_headers": {}}).json()["client_headers_set"] is False
    assert demo.put(CFG, json={"enabled": False}).json()["enabled"] is False


def test_headers_are_encrypted_fields_and_stripped_from_backups(demo):
    demo.put(CFG, json=ON)
    assert {"otel_headers", "otel_client_headers"} <= SENSITIVE["config"]
    doc = asyncio.run(demo.app.state.store.get("config", "otel"))
    assert json.loads(doc["otel_headers"]) == ON["headers"] and "headers" not in doc
    snap = asyncio.run(demo.app.state.backups.snapshot())
    (saved,) = [d for d in snap["config"] if d["id"] == "otel"]
    assert saved["enabled"] is True and "otel_headers" not in saved and "otel_client_headers" not in saved
    assert "s3cret" not in json.dumps(snap) and "t0ken" not in json.dumps(snap)


def test_only_a_super_admin_reads_or_sets_telemetry(demo):
    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get(CFG).status_code == 403 and ga.put(CFG, json=ON).status_code == 403
    page = demo.get("/config").text
    assert "Telemetry (OpenTelemetry)" in page and 'name="client_headers"' in page and "Telemetry: off → on" in page


def test_require_consent_is_a_group_admins_and_needs_telemetry_on(demo):
    r = demo.put("/api/v1/groups/demo", json={"require_telemetry_consent": True})
    assert r.status_code == 422 and "telemetry is enabled" in r.json()["detail"]
    assert "Telemetry consent" not in demo.get("/groups/demo").text
    demo.put(CFG, json=ON)
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        r = ga.put("/api/v1/groups/demo", json={"require_telemetry_consent": True})
        assert r.status_code == 200 and r.json()["require_telemetry_consent"] is True
        assert "Require consent: on → off" in ga.get("/groups/demo").text
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put("/api/v1/groups/demo", json={"require_telemetry_consent": False}).status_code == 403
        assert "Require consent" not in v.get("/groups/demo").text


# what workers get -------------------------------------------------------------------------------------------------


def test_workers_get_the_export_and_client_settings_on_deploy(demo, cloud, collector):
    assert deploy(demo)["status"] == "ok"
    cfg = cloud.deploys[-1]["config"]
    assert not any(k.startswith(("RAMEN_OTEL", "RAMEN_TELEMETRY")) for k in cfg)  # off: nothing
    demo.put(CFG, json=ON)
    assert deploy(demo)["status"] == "ok"
    cfg = cloud.deploys[-1]["config"]
    assert cfg["RAMEN_OTEL_ENDPOINT"] == "http://jaeger.ramen-system:4318"
    assert json.loads(cfg["RAMEN_OTEL_HEADERS"]) == ON["headers"]
    assert json.loads(cfg["RAMEN_OTEL_CLIENT"]) == {
        "otlp_endpoint": "https://otel.example.com:4318",
        "headers": {"x-team": "agents-t0ken"},
        "resource_attributes": {"ramen.group": "demo", "ramen.zone": "zone-a"},
    }
    assert "s3cret" not in cfg["RAMEN_OTEL_CLIENT"]  # never Ramen's own headers
    assert "RAMEN_TELEMETRY_CONSENT" not in cfg and "RAMEN_TELEMETRY_NOTICE" not in cfg
    demo.put("/api/v1/groups/demo", json={"require_telemetry_consent": True})
    assert deploy(demo)["status"] == "ok"
    cfg = cloud.deploys[-1]["config"]
    assert cfg["RAMEN_TELEMETRY_CONSENT"] == "1" and cfg["RAMEN_TELEMETRY_NOTICE"] == "otel.example.com:4318"


def test_the_client_endpoint_defaults_to_the_endpoint_with_no_headers():
    cfg = {"enabled": True, "endpoint": "http://j:4318", "client_endpoint": "", "headers": {"a": "b"}}
    cfg["client_headers"] = {}
    assert otel.client_info(cfg, "g", "z") == {
        "otlp_endpoint": "http://j:4318",
        "headers": {},
        "resource_attributes": {"ramen.group": "g", "ramen.zone": "z"},
    }
    env = otel.worker_env(cfg, {"require_telemetry_consent": True}, "g", "z")
    assert env["RAMEN_TELEMETRY_NOTICE"] == "j:4318" and env["RAMEN_OTEL_HEADERS"] == '{"a":"b"}'
    assert otel.client_info({**cfg, "enabled": False}, "g", "z") is None and otel.host("") == ""
    assert otel._headers("not json") == {} and otel._headers("[1]") == {}


# the token and consent --------------------------------------------------------------------------------------------


def test_token_answers_carry_telemetry_when_enabled_and_no_tel_claim_without_consent(demo):
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert "telemetry" not in t and "tel" not in claims(t["access_token"])
    demo.put(CFG, json=ON)
    want = {
        "otlp_endpoint": "https://otel.example.com:4318",
        "headers": {"x-team": "agents-t0ken"},
        "resource_attributes": {"ramen.group": "demo", "ramen.zone": "zone-a"},
    }
    t = refresh(demo, cid, t["refresh_token"]).json()
    assert t["telemetry"] == want and "tel" not in claims(t["access_token"])
    verifier, challenge = pkce()
    page = authorize(demo, cid, challenge).text
    assert "telemetry_consent" not in page  # the group does not require consent
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert t["telemetry"] == want and "tel" not in claims(t["access_token"])


def decide(user, cid, challenge, **extra):
    return user.post(
        "/oauth/authorize",
        data={
            "response_type": "code",
            "client_id": cid,
            "redirect_uri": CLIENT["redirect_uris"][0],
            "scope": "mcp:demo:zone-a",
            "state": "xyz",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": "mcp:demo:zone-a",
            "csrf_token": user.cookies.get("ramen_csrf", ""),
            **extra,
        },
        follow_redirects=False,
    )


def test_the_consent_page_requires_the_telemetry_box_and_the_token_then_carries_tel(demo):
    demo.put(CFG, json=ON)
    demo.put("/api/v1/groups/demo", json={"require_telemetry_consent": True})
    cid = register(demo)
    verifier, challenge = pkce()
    page = authorize(demo, cid, challenge).text
    assert 'type="checkbox" name="telemetry_consent" value="1" required' in page
    assert "records telemetry about your calls" in page and "<code>otel.example.com:4318</code>" in page
    assert 'value="deny" class="ghost" formnovalidate' in page
    r = decide(demo, cid, challenge, decision="allow")  # allow without the box: impossible
    assert r.status_code == 400 and "telemetry consent" in r.text and "location" not in r.headers
    r = decide(demo, cid, challenge, decision="deny")  # declining ends the flow as before
    assert r.status_code == 303 and "error=access_denied" in r.headers["location"]
    r = decide(demo, cid, challenge, decision="allow", telemetry_consent="1")
    assert r.status_code == 303
    code = r.headers["location"].split("code=")[1].split("&")[0]
    t = exchange(demo, cid, code, verifier).json()
    secret = asyncio.run(demo.app.state.services.session_secret("demo"))
    assert node_verify(t["access_token"], secret)["tel"] is True and t["telemetry"]["otlp_endpoint"]
    t2 = refresh(demo, cid, t["refresh_token"]).json()
    assert claims(t2["access_token"])["tel"] is True  # never asked again
    audits = demo.get("/api/v1/audit?limit=100").json()
    assert any("telemetry:accepted" in a["tags"] for a in audits if a["action"] == "oauth.authorize")
    assert any("reason:consent_required" in a["tags"] and not a["ok"] for a in audits)


def test_a_grant_without_acceptance_cannot_refresh_once_the_group_requires_consent(demo):
    demo.put(CFG, json=ON)
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    verifier2, challenge2 = pkce()
    code2 = consent(demo, cid, challenge2)  # a code issued before the group turned consent on
    demo.put("/api/v1/groups/demo", json={"require_telemetry_consent": True})
    r = refresh(demo, cid, t["refresh_token"])
    assert r.status_code == 400 and r.json()["error"] == "invalid_grant" and "telemetry consent" in r.text
    assert exchange(demo, cid, code2, verifier2).status_code == 400
    denied = [a for a in demo.get("/api/v1/audit?limit=100").json() if a["action"] == "oauth.denied"]
    assert len([a for a in denied if "reason:consent_required" in a["tags"]]) == 2
    demo.put(CFG, json={"enabled": False})  # telemetry off: nobody is asked, whatever the group says
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert "tel" not in claims(t["access_token"]) and "telemetry" not in t


# the deploy span --------------------------------------------------------------------------------------------------


def attrs(span):
    out = {}
    for a in span["attributes"]:
        v = a["value"]
        out[a["key"]] = (
            [x["stringValue"] for x in v["arrayValue"]["values"]] if "arrayValue" in v else next(iter(v.values()))
        )
    return out


def test_one_span_per_deploy_job_goes_to_the_collector(demo, cloud, collector, workers, tmp_path):
    for w in workers.values():
        w.mcp_keys = None
        w.load_result = manifest()
    assert deploy(demo)["status"] == "ok" and collector == []  # telemetry off
    demo.put(CFG, json=ON)
    demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    write_cases(tmp_path, "cases:\n  - tool: calc\n    args: {a: 1}\n    expect: {text_contains: '1'}\n")
    job = deploy(demo)
    assert job["status"] == "ok", (job["error"], job["log"])
    (req,) = collector
    assert str(req.url) == "http://jaeger.ramen-system:4318/v1/traces"
    assert req.headers["authorization"] == "Bearer ramen-export-s3cret" and "x-team" not in req.headers
    body = json.loads(req.content)
    (rs,) = body["resourceSpans"]
    res = {a["key"]: a["value"]["stringValue"] for a in rs["resource"]["attributes"]}
    assert res["service.name"] == "ramen-console" and res["service.version"]
    (span,) = rs["scopeSpans"][0]["spans"]
    assert span["name"] == "ramen.deploy" and len(span["traceId"]) == 32 and len(span["spanId"]) == 16
    assert int(span["endTimeUnixNano"]) >= int(span["startTimeUnixNano"]) > 0 and span["status"] == {"code": 1}
    assert attrs(span) == {
        "ramen.group": "demo",
        "ramen.env": "prod",
        "ramen.zones": ["zone-a"],
        "ramen.outcome": "ok",
        "ramen.golden.passed": "1",
        "ramen.golden.failed": "0",
        "ramen.compat": "none",
        "ramen.image": "base",
    }
    # the console's own calls present the worker admin key, so a consent-gated group still runs its golden cases
    calls = [c for c in workers["cold"].calls if c[0] == "Mcp/Call" and c[2].get("method") == "tools/call"]
    assert calls and calls[-1][1]["x-ramen-admin-key"] == "adm" and calls[-1][1]["authorization"].startswith("Bearer ")
    for w in workers.values():
        w.tool_results["calc"] = {"content": [{"type": "text", "text": "nope"}]}
    job = deploy(demo)
    assert job["status"] == "error"
    span = json.loads(collector[-1].content)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    a = attrs(span)
    assert a["ramen.outcome"] == "error" and a["ramen.golden.failed"] == "1" and span["status"]["code"] == 2


def test_a_dead_or_refusing_collector_is_a_warning_never_a_failed_deploy(demo, cloud, monkeypatch, caplog):
    demo.put(CFG, json=ON)

    def boom(req):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(otel, "TRANSPORT", httpx.MockTransport(boom))
    with caplog.at_level(logging.WARNING, logger="ramen.otel"):
        assert deploy(demo)["status"] == "ok"
    assert any("could not export the deploy span" in r.message for r in caplog.records)
    monkeypatch.setattr(otel, "TRANSPORT", httpx.MockTransport(lambda req: httpx.Response(401)))
    with caplog.at_level(logging.WARNING, logger="ramen.otel"):
        assert deploy(demo)["status"] == "ok"
    assert any("refused the deploy span" in r.message for r in caplog.records)


def test_span_attributes_report_compat_and_bad_times():
    job = {"status": "ok", "started": None, "finished": "junk", "result": {"z": {"compat": {"additive": ["x"]}}}}
    span = otel.deploy_span(job, "g", "e", ["z"], "img:1")["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert attrs(span)["ramen.compat"] == "additive" and attrs(span)["ramen.image"] == "img:1"
    assert span["startTimeUnixNano"] == "0" and span["endTimeUnixNano"] == "0"
    job["result"]["z"]["compat"]["breaking"] = ["y"]
    span = otel.deploy_span(job, "g", "e", ["z"], None)["resourceSpans"][0]["scopeSpans"][0]["spans"][0]
    assert attrs(span)["ramen.compat"] == "breaking"
    assert otel._attr("k", True) == {"key": "k", "value": {"boolValue": True}}


# the admin key on the console's own calls -------------------------------------------------------------------------


def test_the_grpc_client_sends_the_admin_key_only_when_it_has_one(workers):
    w = workers["cold"]
    msg = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
    asyncio.run(Client(deadline=2).call(w.target, "rmk_1", "demo", "zone-a", msg))
    assert "x-ramen-admin-key" not in w.calls[-1][1]
    asyncio.run(Client(deadline=2, admin_key="adm").call(w.target, "rmk_1", "demo", "zone-a", msg))
    assert w.calls[-1][1]["x-ramen-admin-key"] == "adm"
    asyncio.run(Client(deadline=2).call(w.target, "rmk_1", "demo", "zone-a", msg, admin_key="other"))
    assert w.calls[-1][1]["x-ramen-admin-key"] == "other"
