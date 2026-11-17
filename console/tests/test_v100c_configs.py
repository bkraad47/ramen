"""CONTRACTS §31 console side: the group repo's `mcp/llm-config/` validated at sync, versioned, handed out in the token
answer and the policy document, accepted per user and version on every sign-in path (page, consent, redirect), gating
the token and MCP-key minting; the OTLP ingest at /otlp/v1/* stamping and forwarding to the collector."""

import asyncio
import gzip
import json
from pathlib import Path
from urllib.parse import quote

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry.proto.collector.logs.v1.logs_service_pb2 import ExportLogsServiceRequest
from opentelemetry.proto.collector.metrics.v1.metrics_service_pb2 import ExportMetricsServiceRequest

from ramen_console import configs, otel
from ramen_console.web import otlp_routes
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixtures
from tests.test_oauth_server import authorize, consent, exchange, pkce, refresh, register
from tests.test_v100_otel import CFG, ON, decide

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
ACCEPT = "/api/v1/groups/demo/configs/accept"
PAGE = "/groups/demo/configs"
MANIFEST = """version: 1
description: demo LLM configs
targets:
  - {client: claude-code, file: claude.json, path: ~/.claude/settings.json, format: json, merge: deep}
  - {client: codex, file: codex.toml, path: ~/.codex/config.toml, format: toml, merge: deep}
  - {client: env, file: env.sh, path: ~/.ramen/env.sh, format: env, merge: keys}
  - {client: shell, file: rc.txt, path: ~/.zshrc, format: text, merge: block}
"""
CLAUDE = json.dumps(
    {
        "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "ramen-mcp-bridge policy check"}]}]},
        "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "${RAMEN_OTLP_ENDPOINT}"},
        "model": "opus",
    }
)
CODEX = '[otel]\nexporter = "otlp-http"\nendpoint = "${RAMEN_OTLP_ENDPOINT}"\n'
ENV = "OTEL_EXPORTER_OTLP_ENDPOINT=${RAMEN_OTLP_ENDPOINT}\nRAMEN_GROUP=${RAMEN_GROUP}\n"
RC = "source ~/.ramen/env.sh\n"
GOOD = {"manifest.yaml": MANIFEST, "claude.json": CLAUDE, "codex.toml": CODEX, "env.sh": ENV, "rc.txt": RC}
STAMPED = ("ramen.group", "ramen.user", "openinference.project.name", "service.namespace")


def write_configs(tmp_path, files=GOOD):
    d = tmp_path / "buckets" / "demo" / configs.DIR
    if d.is_dir():
        for p in d.iterdir():
            p.unlink()
    d.mkdir(parents=True, exist_ok=True)
    for name, text in files.items():
        (d / name).write_text(text)
    return configs.version(files)


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


def member(demo, email="m@x", role="viewer"):  # noqa: F811
    make_user(demo, email, role, ["demo"])
    c = TestClient(demo.app)
    r = c.post("/login", data={"email": email, "password": PW}, follow_redirects=False)
    assert r.status_code == 303, r.text
    return c


def with_manifest(**over):
    doc = dict(GOOD)
    doc["manifest.yaml"] = MANIFEST
    for k, v in over.items():
        doc[k] = v
    return doc


def target_manifest(t):
    return f"version: 1\ntargets:\n  - {json.dumps(t)}\n"


def violation(files):
    with pytest.raises(configs.ConfigsError) as e:
        configs.parse(files)
    return str(e.value)


# pure -----------------------------------------------------------------------------------------------------------------


def test_parse_and_version():
    p = configs.parse(GOOD)
    assert len(p["version"]) == 12 and int(p["version"], 16) >= 0
    assert p["manifest"]["version"] == 1 and len(p["manifest"]["targets"]) == 4
    assert set(p["files"]) == {"claude.json", "codex.toml", "env.sh", "rc.txt"} and p["files"]["rc.txt"] == RC
    assert configs.version(GOOD) == p["version"]
    assert configs.version({**GOOD, "rc.txt": RC + "#"}) != p["version"]
    assert configs.version({**GOOD, "manifest.yaml": MANIFEST + "# x\n"}) != p["version"]


def test_manifest_violations_name_the_problem():
    assert "manifest.yaml" in violation({"claude.json": CLAUDE})
    assert "version" in violation(with_manifest(**{"manifest.yaml": MANIFEST.replace("version: 1", "version: 2")}))
    assert "targets" in violation({"manifest.yaml": "version: 1\n"})
    assert "not valid YAML" in violation({"manifest.yaml": "version: [1\n"})
    base = {"client": "x", "file": "rc.txt", "path": "~/.zshrc", "format": "text", "merge": "block"}
    for over, word in (
        ({"format": "ini"}, "format"),
        ({"merge": "deep"}, "merge"),
        ({"path": "/etc/zshrc"}, "under ~"),
        ({"path": "~/../x"}, "under ~"),
        ({"file": "nope.txt"}, "nope.txt"),
        ({"file": "../rc.txt"}, "file"),
        ({"client": ""}, "client"),
    ):
        assert word in violation({"manifest.yaml": target_manifest({**base, **over}), "rc.txt": RC})
    assert "manifest.yaml" in violation({"manifest.yaml": target_manifest({**base, "file": "manifest.yaml"})})


def test_fragment_violations_name_the_key():
    bad = json.loads(CLAUDE)
    assert "apiKeyHelper" in violation(with_manifest(**{"claude.json": json.dumps({**bad, "apiKeyHelper": "x"})}))
    assert "awsAuthRefresh" in violation(
        with_manifest(**{"claude.json": json.dumps({**bad, "env": {"awsAuthRefresh": "x"}})})
    )
    assert "theme" in violation(with_manifest(**{"claude.json": json.dumps({**bad, "theme": "dark"})}))
    hooks = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "curl evil"}]}]}}
    assert "curl evil" in violation(with_manifest(**{"claude.json": json.dumps(hooks)}))
    assert "ANTHROPIC_API_KEY" in violation(
        with_manifest(**{"claude.json": json.dumps({**bad, "env": {"ANTHROPIC_API_KEY": "sk"}})})
    )
    assert "GITHUB_TOKEN" in violation(with_manifest(**{"codex.toml": CODEX + 'github_token = "x"\n'.upper()}))
    assert "OPENAI_API_KEY" in violation(with_manifest(**{"env.sh": ENV + "OPENAI_API_KEY=sk\n"}))
    assert "MY_TOKEN" in violation(with_manifest(**{"rc.txt": "export MY_TOKEN=abc\n"}))
    assert "not valid JSON" in violation(with_manifest(**{"claude.json": "{nope"}))
    assert "not valid TOML" in violation(with_manifest(**{"codex.toml": "[x\n"}))
    assert "KEY=value" in violation(with_manifest(**{"env.sh": "no equals here\n"}))
    assert "32 KiB" in violation(with_manifest(**{"rc.txt": "x" * (configs.MAX_TEXT + 1)}))
    assert "256 KiB" in violation(with_manifest(**{"claude.json": json.dumps({"model": "x" * configs.MAX_DIR})}))
    assert "object" in violation(with_manifest(**{"claude.json": "[1]"}))


def test_client_info_worker_env_and_policy():
    p = configs.parse(GOOD)
    assert configs.worker_env(None) == {} and configs.client_info(None, True) is None and configs.policy(None) is None
    assert configs.worker_env(p) == {"RAMEN_CONFIGS_VERSION": p["version"], "RAMEN_CONFIGS_REQUIRED": "1"}
    info = configs.client_info(p, False)
    assert info == {
        "version": p["version"],
        "required": True,
        "accepted": False,
        "manifest": MANIFEST,
        "files": p["files"],
    }
    assert configs.policy(p) == {"version": p["version"], "required": True}
    broken = {"version": "abc", "manifest": {}, "files": {}, "error": "claude.json: key 'apiKeyHelper' is forbidden"}
    info = configs.client_info(broken, True)
    assert (
        info["accepted"] is False and "manifest" not in info and "files" not in info and "apiKeyHelper" in info["error"]
    )
    user = {"accepted_configs": {"demo": {"version": "abc", "at": "t"}}}
    assert configs.accepted_version(user, "demo") == "abc" and configs.accepted_version(user, "other") is None
    assert configs.is_accepted(user, "demo", "abc") and not configs.is_accepted(user, "demo", "abd")
    assert not configs.is_accepted({}, "demo", "abc")


# deploy ----------------------------------------------------------------------------------------


def test_deploy_validates_the_directory_at_sync_and_tells_the_workers(demo, ws, tmp_path):  # noqa: F811
    job = deploy(demo)
    assert job["status"] == "ok" and "RAMEN_CONFIGS_" not in Path(job["result"]["zone-a"]["env_file"]).read_text()
    v = write_configs(tmp_path)
    job = deploy(demo)
    assert job["status"] == "ok", job
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert f"RAMEN_CONFIGS_VERSION={v}\n" in env_file and "RAMEN_CONFIGS_REQUIRED=1\n" in env_file
    assert any(f"{configs.DIR}: version {v}, 4 files" in line for line in job["log"])
    write_configs(tmp_path, with_manifest(**{"claude.json": json.dumps({"model": "x", "apiKeyHelper": "evil"})}))
    job = deploy(demo)
    assert job["status"] == "error" and "apiKeyHelper" in job["error"] and configs.DIR in job["error"]
    assert job["result"] is None or "zone-a" not in (job["result"] or {})  # failed before any zone


# acceptance ----------------------------------------------------------------------------------------


def test_accept_round_trip_rbac_and_audit(demo, tmp_path):
    assert demo.post(ACCEPT, json={"version": "x"}).status_code == 404  # no directory in the repo
    v = write_configs(tmp_path)
    m = member(demo)
    r = m.post(ACCEPT, json={"version": "stale0000000"})
    assert r.status_code == 409 and v in r.json()["detail"]
    r = m.post(ACCEPT, json={"version": v})
    assert r.status_code == 200, r.text
    assert r.json()["group"] == "demo" and r.json()["version"] == v and r.json()["at"]
    me = next(u for u in demo.get("/api/v1/users").json() if u["email"] == "m@x")
    assert me["accepted_configs"]["demo"]["version"] == v
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "configs.accept"]
    assert a and a[-1]["user"] == "m@x" and {"group:demo", f"version:{v}"} <= set(a[-1]["tags"])
    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(demo.app) as o:
        login(o, "o@x", PW)
        assert o.post(ACCEPT, json={"version": v}).status_code == 403
        assert o.get(PAGE).status_code == 403
    assert demo.post(ACCEPT, json={"version": v}).status_code == 200  # a super admin accepts too (per group)


def test_the_page_shows_every_file_verbatim_and_accepts_then_follows_next(demo, tmp_path):
    v = write_configs(tmp_path)
    m = member(demo)
    page = m.get(PAGE + "?next=/groups/demo").text
    assert f"<code>{v}</code>" in page and "demo LLM configs" in page
    for name, text in GOOD.items():
        assert name in page and f"<pre>{text}</pre>" in page  # verbatim: quotes and newlines untouched
    assert 'name="version" value="' + v + '"' in page and 'name="next" value="/groups/demo"' in page
    assert "Accept" in page and "not accepted" in page
    r = m.post(
        PAGE + "/accept",
        data={"version": v, "next": "/groups/demo", "csrf_token": m.cookies.get("ramen_csrf", "")},
        follow_redirects=False,
    )
    assert r.status_code == 303 and r.headers["location"] == "/groups/demo"
    assert "accepted this version" in m.get(PAGE).text
    r = m.post(PAGE + "/accept", data={"version": "old", "next": "/", "csrf_token": m.cookies.get("ramen_csrf", "")})
    assert r.status_code == 409
    assert demo.get("/groups/other/configs").status_code == 404


def test_password_and_magic_sign_in_redirect_to_the_configs_page_first(demo, tmp_path):
    make_user(demo, "m@x", "viewer", ["demo", "other"])
    with TestClient(demo.app) as c:
        r = c.post("/login", data={"email": "m@x", "password": PW}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/"  # nothing to accept
    v = write_configs(tmp_path)
    with TestClient(demo.app) as c:
        r = c.post("/login", data={"email": "m@x", "password": PW, "next": "/users"}, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"{PAGE}?next={quote('/users', safe='')}"
        assert "ramen_session" in r.cookies  # signed in, just sent to accept first
        c.post(ACCEPT, json={"version": v})
        r = c.post("/login", data={"email": "m@x", "password": PW, "next": "/users"}, follow_redirects=False)
        assert r.headers["location"] == "/users"
    # a magic-link sign-in takes the same turn
    demo.put("/api/v1/config/auth", json={"magic_link": True})
    st = demo.app.state
    user, nonce = asyncio.run(st.accounts.start_token("m@x", "magic"))
    write_configs(tmp_path, with_manifest(**{"rc.txt": RC + "# v2\n"}))  # a new version: accept again
    with TestClient(demo.app) as c:
        r = c.get(f"/auth/magic/{st.tokens.issue('magic', user['id'], nonce)}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == f"{PAGE}?next=%2F"


def test_the_consent_page_requires_acceptance_and_the_token_carries_configs(demo, tmp_path):
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert "configs" not in t
    v = write_configs(tmp_path)
    verifier, challenge = pkce()
    page = authorize(demo, cid, challenge).text
    assert f'name="configs_accept" value="{v}" required' in page and "claude.json" in page and RC.strip() in page
    r = decide(demo, cid, challenge, decision="allow")
    assert r.status_code == 403 and "configs not accepted" in r.text.lower() and "location" not in r.headers
    r = decide(demo, cid, challenge, decision="allow", configs_accept="stale")
    assert r.status_code == 403
    r = decide(demo, cid, challenge, decision="allow", configs_accept=v)
    assert r.status_code == 303, r.text
    code = r.headers["location"].split("code=")[1].split("&")[0]
    t = exchange(demo, cid, code, verifier).json()
    assert t["configs"] == {
        "version": v,
        "required": True,
        "accepted": True,
        "manifest": MANIFEST,  # the YAML text: the bridge parses it
        "files": {k: x for k, x in GOOD.items() if k != "manifest.yaml"},
    }
    audits = demo.get("/api/v1/audit?limit=200").json()
    assert any(a["action"] == "configs.accept" for a in audits)
    assert any("configs:accepted" in a["tags"] for a in audits if a["action"] == "oauth.authorize")
    verifier, challenge = pkce()
    assert "configs_accept" not in authorize(demo, cid, challenge).text  # accepted: the block is gone
    # a new version makes the acceptance stale: a refresh still answers (the bridge polls it while the person accepts
    # in the browser) with `accepted: false`; a first token (code exchange) is refused 403 until re-accepted
    v2 = write_configs(tmp_path, with_manifest(**{"rc.txt": RC + "# v2\n"}))
    r = refresh(demo, cid, t["refresh_token"])
    assert r.status_code == 200, r.text
    assert r.json()["access_token"] and r.json()["configs"]["version"] == v2
    assert r.json()["configs"]["accepted"] is False and r.json()["configs"]["files"]["rc.txt"] == RC + "# v2\n"
    demo.post(ACCEPT, json={"version": v2})
    verifier, challenge = pkce()
    code = consent(demo, cid, challenge)  # consented on v2 …
    v3 = write_configs(tmp_path, with_manifest(**{"rc.txt": RC + "# v3\n"}))  # … exchanged after v3 landed
    r = exchange(demo, cid, code, verifier)
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"] == "configs_not_accepted" and body["data"]["version"] == v3
    assert "not accepted" in body["error_description"].lower() and PAGE in body["data"]["url"]
    denied = [a for a in demo.get("/api/v1/audit?limit=50").json() if a["action"] == "oauth.denied"]
    assert denied and "reason:configs_not_accepted" in denied[-1]["tags"]
    demo.post(ACCEPT, json={"version": v3})
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert t["configs"]["version"] == v3 and t["configs"]["accepted"] is True


def test_mcp_key_minting_needs_acceptance_and_records_the_version(demo, tmp_path):
    assert demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "before"}).status_code == 201
    v = write_configs(tmp_path)
    r = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    assert r.status_code == 403, r.text
    assert r.json()["data"] == {"version": v, "url": f"http://testserver{PAGE}"}
    assert "not accepted" in r.json()["detail"].lower()
    r = demo.post("/api/v1/api-keys", json={"name": "bot", "client_type": "agent", "groups": ["demo"]})
    assert r.status_code == 403 and r.json()["data"]["version"] == v
    demo.post(ACCEPT, json={"version": v})
    r = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"})
    assert r.status_code == 201, r.text
    assert r.json()["accepted_configs"] == v
    r = demo.post("/api/v1/api-keys", json={"name": "bot", "client_type": "agent", "groups": ["demo"]})
    assert r.status_code == 201
    keys = {k["name"]: k for k in demo.get("/api/v1/groups/demo/mcp-keys").json()}
    assert keys["ci"]["accepted_configs"] == v and keys["before"].get("accepted_configs") is None
    assert (
        next(k for k in keys if k.startswith("bot-"))
        and keys[next(k for k in keys if k.startswith("bot-"))]["accepted_configs"] == v
    )
    page = demo.get("/groups/demo").text
    assert f"<code>{v}</code>" in page and "LLM configs" in page
    v2 = write_configs(tmp_path, with_manifest(**{"rc.txt": RC + "# v2\n"}))
    page = demo.get("/groups/demo").text
    assert f"{v} (stale)" in page and f"<code>{v2}</code>" in page


def test_the_group_page_lists_acceptance_per_member_for_admins(demo, tmp_path):
    v = write_configs(tmp_path)
    make_user(demo, "ga@x", "group_admin", ["demo"])
    m = member(demo)
    page = demo.get("/groups/demo").text
    assert "LLM configs" in page and "m@x" in page and "ga@x" in page and page.count("not accepted") >= 2
    m.post(ACCEPT, json={"version": v})
    page = demo.get("/groups/demo").text
    assert page.count("not accepted") == 1  # ga@x still has not
    assert 'href="/groups/demo/configs"' in page
    with TestClient(demo.app) as v_:
        login(v_, "m@x", PW)
        page = v_.get("/groups/demo").text
        assert "ga@x" not in page  # members' acceptance is the admin's view


def test_the_policy_document_names_the_configs_version(demo, tmp_path):
    with TestClient(demo.app) as anon:
        assert "configs" not in anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a").json()
        v = write_configs(tmp_path)
        doc = anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a").json()
        assert doc["configs"] == {"version": v, "required": True} and RC not in json.dumps(doc)


# OTLP ingest ----------------------------------------------------------------------------------------


@pytest.fixture
def collector(monkeypatch):
    seen = []
    state = {"status": 200}

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if isinstance(state["status"], Exception):
            raise state["status"]
        return httpx.Response(state["status"], json={"partialSuccess": {}})

    monkeypatch.setattr(otel, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setattr(otlp_routes, "RATE", 1000)
    otlp_routes.reset()
    seen_state = (seen, state)
    return seen_state


def traces_json(extra_attr=None):
    attrs = [{"key": "service.name", "value": {"stringValue": "claude-code"}}] + ([extra_attr] if extra_attr else [])
    return {
        "resourceSpans": [
            {
                "resource": {"attributes": attrs},
                "scopeSpans": [{"scope": {"name": "x"}, "spans": [{"name": "s", "traceId": "00", "spanId": "00"}]}],
            }
        ]
    }


def attrs_of(items):
    return {a["key"]: a["value"]["stringValue"] for a in items}


def token_for(demo, cid=None):
    cid = cid or register(demo)
    verifier, challenge = pkce()
    return exchange(demo, cid, consent(demo, cid, challenge), verifier).json()["access_token"]


def test_ingest_stamps_json_and_forwards_with_ramen_headers(demo, collector, caplog):
    seen, _ = collector
    tok = token_for(demo)
    r = demo.post("/otlp/v1/traces", json=traces_json(), headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 204 and seen == []  # telemetry off: nothing forwarded
    demo.put(CFG, json=ON)
    body = traces_json({"key": "ramen.user", "value": {"stringValue": "spoofed"}})
    r = demo.post("/otlp/v1/traces", json=body, headers={"Authorization": f"Bearer {tok}"})
    assert r.status_code == 200, r.text
    assert len(seen) == 1 and str(seen[0].url) == "http://jaeger.ramen-system:4318/v1/traces"
    assert seen[0].headers["authorization"] == "Bearer ramen-export-s3cret"
    assert seen[0].headers["content-type"].startswith("application/json")
    sent = json.loads(seen[0].content)
    a = attrs_of(sent["resourceSpans"][0]["resource"]["attributes"])
    assert a["service.name"] == "claude-code" and a["ramen.group"] == "demo" and a["ramen.user"] == "root@ramen.local"
    assert a["ramen.zone"] == "zone-a" and a["openinference.project.name"] == "demo"
    assert a["service.namespace"] == "ramen/demo"
    assert sum(1 for x in sent["resourceSpans"][0]["resource"]["attributes"] if x["key"] == "ramen.user") == 1
    assert tok not in caplog.text and "claude-code" not in caplog.text
    assert demo.post("/otlp/v1/profiles", json={}, headers={"Authorization": f"Bearer {tok}"}).status_code == 404


def test_ingest_accepts_protobuf_gzip_and_every_bearer_kind(demo, collector, tmp_path):
    seen, _ = collector
    demo.put(CFG, json=ON)
    msg = ExportMetricsServiceRequest()
    rm = msg.resource_metrics.add()
    kv = rm.resource.attributes.add()
    kv.key, kv.value.string_value = "service.name", "codex"
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()["key"]
    r = demo.post(
        "/otlp/v1/metrics",
        content=gzip.compress(msg.SerializeToString()),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/x-protobuf",
            "Content-Encoding": "gzip",
        },
    )
    assert r.status_code == 200, r.text
    assert str(seen[-1].url).endswith("/v1/metrics") and seen[-1].headers["content-type"] == "application/x-protobuf"
    assert "content-encoding" not in seen[-1].headers
    out = ExportMetricsServiceRequest()
    out.ParseFromString(seen[-1].content)
    a = {x.key: x.value.string_value for x in out.resource_metrics[0].resource.attributes}
    assert a["service.name"] == "codex" and a["ramen.user"] == "key:ci" and a["ramen.group"] == "demo"
    assert "ramen.zone" not in a and a["service.namespace"] == "ramen/demo"
    # the console session (cookie) with the group named in a header
    logs = ExportLogsServiceRequest()
    logs.resource_logs.add()
    r = demo.post(
        "/otlp/v1/logs",
        content=logs.SerializeToString(),
        headers={"Content-Type": "application/x-protobuf", "ramen-group": "demo"},
    )
    assert r.status_code == 200, r.text
    out = ExportLogsServiceRequest()
    out.ParseFromString(seen[-1].content)
    a = {x.key: x.value.string_value for x in out.resource_logs[0].resource.attributes}
    assert a["ramen.user"] == "root@ramen.local" and a["ramen.group"] == "demo"
    # the session token as a bearer
    sess = demo.cookies.get("ramen_session")
    with TestClient(demo.app) as anon:
        r = anon.post("/otlp/v1/logs", json={"resourceLogs": []}, headers={"Authorization": f"Bearer {sess}"})
        assert r.status_code == 401  # no group named
        r = anon.post(
            "/otlp/v1/logs", json={"resourceLogs": []}, headers={"Authorization": f"Bearer {sess}", "ramen-group": "x"}
        )
        assert r.status_code == 403
        r = anon.post(
            "/otlp/v1/logs",
            json={"resourceLogs": []},
            headers={"Authorization": f"Bearer {sess}", "ramen-group": "demo"},
        )
        assert r.status_code == 200
        assert anon.post("/otlp/v1/logs", json={"resourceLogs": []}).status_code == 401
        assert anon.post("/otlp/v1/logs", json={}, headers={"Authorization": "Bearer rmk_nope"}).status_code == 401
        bad = {"Authorization": "Bearer a.b.c"}
        assert anon.post("/otlp/v1/logs", json={}, headers=bad).status_code == 401
    make_user(demo, "o@x", "viewer", ["other"])
    with TestClient(demo.app) as o:
        login(o, "o@x", PW)
        r = o.post("/otlp/v1/logs", json={"resourceLogs": []}, headers={"ramen-group": "demo"})
        assert r.status_code == 403


def test_ingest_limits_and_upstream_failures(demo, collector, monkeypatch):
    seen, state = collector
    demo.put(CFG, json=ON)
    tok = token_for(demo)
    h = {"Authorization": f"Bearer {tok}"}
    assert (
        demo.post(
            "/otlp/v1/traces", content=b"\xff\xfe garbage", headers={**h, "Content-Type": "application/x-protobuf"}
        ).status_code
        == 400
    )
    assert (
        demo.post("/otlp/v1/traces", content=b"{nope", headers={**h, "Content-Type": "application/json"}).status_code
        == 400
    )
    assert demo.post("/otlp/v1/traces", json=[1], headers=h).status_code == 400
    assert demo.post("/otlp/v1/traces", content=b"x", headers={**h, "Content-Type": "text/plain"}).status_code == 415
    big = b" " * (otlp_routes.MAX_BODY + 1)
    assert (
        demo.post("/otlp/v1/traces", content=big, headers={**h, "Content-Type": "application/json"}).status_code == 413
    )
    gz = gzip.compress(b'{"resourceSpans": [], "pad": "' + b"x" * (otlp_routes.MAX_BODY + 1) + b'"}')
    r = demo.post(
        "/otlp/v1/traces", content=gz, headers={**h, "Content-Type": "application/json", "Content-Encoding": "gzip"}
    )
    assert r.status_code == 413
    r = demo.post(
        "/otlp/v1/traces",
        content=b"not gzip",
        headers={**h, "Content-Type": "application/json", "Content-Encoding": "gzip"},
    )
    assert r.status_code == 400
    assert seen == []
    state["status"] = 503
    r = demo.post("/otlp/v1/traces", json=traces_json(), headers=h)
    assert r.status_code == 502 and r.json()["upstream_status"] == 503
    state["status"] = httpx.ConnectError("down")
    r = demo.post("/otlp/v1/traces", json=traces_json(), headers=h)
    assert r.status_code == 502 and r.json()["upstream_status"] is None and "ConnectError" in r.json()["detail"]
    state["status"] = 200
    monkeypatch.setattr(otlp_routes, "RATE", 2)
    otlp_routes.reset()
    assert demo.post("/otlp/v1/traces", json=traces_json(), headers=h).status_code == 200
    assert demo.post("/otlp/v1/traces", json=traces_json(), headers=h).status_code == 200
    r = demo.post("/otlp/v1/traces", json=traces_json(), headers=h)
    assert r.status_code == 429 and r.headers["retry-after"]
    other = token_for(demo)
    assert (
        demo.post("/otlp/v1/traces", json=traces_json(), headers={"Authorization": f"Bearer {other}"}).status_code
        == 200
    )


def test_ingest_audits_once_per_thousand_requests_per_token(demo, collector, monkeypatch):
    demo.put(CFG, json=ON)
    monkeypatch.setattr(otlp_routes, "AUDIT_EVERY", 3)
    otlp_routes.reset()
    tok = token_for(demo)
    for _ in range(7):
        assert (
            demo.post("/otlp/v1/traces", json=traces_json(), headers={"Authorization": f"Bearer {tok}"}).status_code
            == 200
        )
    rows = [a for a in demo.get("/api/v1/audit?limit=200").json() if a["action"] == "telemetry.ingest"]
    assert len(rows) == 3  # requests 1, 4 and 7
    assert rows[-1]["user"] == "root@ramen.local" and {"group:demo", "signal:traces", "zone:zone-a"} <= set(
        rows[-1]["tags"]
    )
    assert tok not in json.dumps(rows)


def test_the_config_page_shows_the_ingest_url(demo, monkeypatch):
    monkeypatch.setenv("RAMEN_PUBLIC_URL", "https://ramen.example.com")
    page = demo.get("/config").text
    assert "<code>https://ramen.example.com/otlp</code>" in page and "Ingest URL" in page


def test_an_invalid_directory_blocks_acceptance_and_more_violations(demo, tmp_path):
    t = {"client": "x", "file": "f.yaml", "path": "~/.x.yaml", "format": "yaml", "merge": "replace"}
    assert "not valid YAML" in violation({"manifest.yaml": target_manifest(t), "f.yaml": "a: [1\n"})
    assert "mapping" in violation({"manifest.yaml": "version: 1\ntargets: [1]\n"})
    c = {**t, "file": "c.yaml", "path": "~/.claude/settings.json"}
    assert "JSON object" in violation({"manifest.yaml": target_manifest(c), "c.yaml": "- 1\n"})
    bad = with_manifest(**{"claude.json": json.dumps({"model": "x", "apiKeyHelper": "x"})})
    v = write_configs(tmp_path, bad)
    r = demo.post(ACCEPT, json={"version": v})
    assert r.status_code == 422 and "apiKeyHelper" in r.json()["detail"]
    assert "does not validate" in demo.get(PAGE).text
    cid = register(demo)
    verifier, challenge = pkce()
    page = authorize(demo, cid, challenge).text
    assert "does not validate" in page and "configs_accept" not in page
    assert decide(demo, cid, challenge, decision="allow").status_code == 403
    with TestClient(demo.app) as anon:
        doc = anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a").json()
        assert doc["configs"] == {"version": v, "required": True}  # the gate holds while the admin fixes the repo
    assert "RAMEN_CONFIGS_VERSION" in json.dumps(
        configs.worker_env(asyncio.run(configs.load(demo.app.state.services, "demo")))
    )


def test_ingest_refuses_a_tampered_token_and_forgets_old_windows(demo, collector, monkeypatch):
    demo.put(CFG, json=ON)
    tok = token_for(demo)
    h, p, s = tok.split(".")
    bad = {"Authorization": f"Bearer {h}.{p}.{'A' * len(s)}"}
    assert demo.post("/otlp/v1/traces", json=traces_json(), headers=bad).status_code == 401
    monkeypatch.setattr(otlp_routes, "RATE", 1)
    monkeypatch.setattr(otlp_routes, "WINDOW", 0.0)
    otlp_routes.reset()
    for _ in range(3):  # every request opens a new window: the limit never bites
        assert (
            demo.post("/otlp/v1/traces", json=traces_json(), headers={"Authorization": f"Bearer {tok}"}).status_code
            == 200
        )
    assert demo.post("/otlp/v1/traces", json=traces_json(), headers={"Authorization": "Bearer "}).status_code == 401
