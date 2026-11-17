"""CONTRACTS §38.1 (D85, F1): the console's LLM judge proxy — judge tokens per zone and deploy, the bearer matrix,
allowed models, the per-group rate limit, the token cap, the mapping to every provider, the audit cadence; workers and
the token answer point at the proxy and carry no provider key; `RAMEN_*` in `mcp/env.yaml` and env reads in inline
evals assertions are refused."""

import asyncio
import base64
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console import deploy as deploy_mod
from ramen_console import evals, llm_proxy, llm_scope, otel
from ramen_console.app import create_app
from ramen_console.storage import make_store
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixtures
from tests.test_v100c_configs import token_for
from tests.test_v100c_evals import DEPLOY
from tests.test_v100c_guardrails_policy import LLM, write_rails

ORG = "/api/v1/config/llm-providers"
GROUP = "/api/v1/groups/demo/llm-providers"
ORG_KEY = "sk-org-key-not-real-0003"
INTERNAL = "http://console.ramen-system.svc.cluster.local:80"
CHAT = "/llm/v1/chat/completions"
MSG = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "Is this safe? Answer: Yes or No"}]}
OK = {
    "id": "c1",
    "object": "chat.completion",
    "choices": [{"index": 0, "message": {"role": "assistant", "content": "No"}}],
}


@pytest.fixture
def upstream(monkeypatch):
    seen, state = [], {"status": 200, "body": OK}

    def handler(req: httpx.Request):
        seen.append(req)
        if isinstance(state["status"], Exception):
            raise state["status"]
        return httpx.Response(state["status"], json=state["body"])

    monkeypatch.setattr(llm_proxy, "TRANSPORT", httpx.MockTransport(handler))
    monkeypatch.setenv(llm_proxy.INTERNAL_ENV, INTERNAL)
    llm_proxy.LIMIT.reset()
    return seen, state


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


def env_of(job) -> dict[str, str]:
    from pathlib import Path

    text = Path(job["result"]["zone-a"]["env_file"]).read_text()
    return dict(ln.split("=", 1) for ln in text.splitlines() if "=" in ln)


def rails_deployed(demo, tmp_path):
    write_rails(tmp_path, LLM)
    demo.put(ORG, json={"openai": {"api_key": ORG_KEY, "allowed_models": ["gpt-4o-mini", "gpt-4o"]}})
    job = deploy(demo)
    assert job["status"] == "ok", job
    return job


def bearer(tok, **extra):
    return {"Authorization": f"Bearer {tok}", **extra}


def secret(demo, group="demo"):
    return asyncio.run(demo.app.state.services.session_secret(group))


# judge tokens -------------------------------------------------------------------------------------------------------


def test_judge_tokens_verify_only_with_their_group_secret():
    t = llm_proxy.judge_token("s1", "demo", "zone-a", "job1")
    assert t.startswith("rjt_") and llm_proxy.judge_claims(t) == ("demo", "zone-a", "job1")
    assert llm_proxy.judge_ok(t, "s1") and not llm_proxy.judge_ok(t, "s2")
    forged = "rjt_" + base64.urlsafe_b64encode(b"other/zone-a/job1").decode().rstrip("=") + "." + t.split(".")[1]
    assert llm_proxy.judge_claims(forged)[0] == "other" and not llm_proxy.judge_ok(forged, "s1")
    for bad in ("rmk_x", "rjt_nodot", "rjt_!!!.x", "rjt_" + base64.urlsafe_b64encode(b"a/b").decode() + ".x"):
        assert llm_proxy.judge_claims(bad) is None
    assert llm_proxy.judge_token("s1", "demo", "zone-a", "job2") != t


def test_live_jobs_and_the_record_keep_the_stable_tracks_token():
    assert llm_proxy.live_jobs({}, "z") == []
    assert llm_proxy.live_jobs({"last_deploy": {"job": "j0"}}, "z") == ["j0"]  # recorded before §38.1
    rec = llm_proxy.judge_record({"job": "j0"}, {"a": {"ok": True}, "b": {"ok": False}}, "j1")
    assert rec == {"a": ["j1", "j0"], "b": ["j0"]}
    rec = llm_proxy.judge_record({"job": "j1", "judge": rec}, {"a": {"ok": True}}, "j2")
    assert rec == {"a": ["j2", "j1"], "b": ["j0"]}  # a zone this deploy did not touch keeps its own
    assert llm_proxy.judge_record({"judge": rec}, {"a": {"ok": True}}, "j2")["a"] == ["j2", "j1"]
    assert llm_proxy.live_jobs({"last_deploy": {"job": "j2", "judge": rec}}, "b") == ["j0"]
    assert llm_proxy.judge_record({}, {"a": {"ok": False}}, "j1") == {"a": []}


def test_proxy_url_prefers_the_in_cluster_console(monkeypatch):
    monkeypatch.delenv(llm_proxy.INTERNAL_ENV, raising=False)
    assert llm_proxy.proxy_url("") == "" and llm_proxy.proxy_url("https://c.example/") == "https://c.example/llm/v1"
    monkeypatch.setenv(llm_proxy.INTERNAL_ENV, INTERNAL + "/")
    assert llm_proxy.proxy_url("https://c.example") == INTERNAL + "/llm/v1"
    monkeypatch.setenv(llm_proxy.PER_MIN_ENV, "x")
    assert llm_proxy.per_min() == 120


# deploy: workers point at the proxy ---------------------------------------------------------------------------------


def test_the_deploy_hands_workers_the_proxy_and_a_judge_token_never_the_key(demo, ws, tmp_path, upstream):  # noqa: F811
    job = rails_deployed(demo, tmp_path)
    env = env_of(job)
    assert env["RAMEN_LLM_PROVIDER"] == "openai_compatible" and env["RAMEN_LLM_BASE_URL"] == INTERNAL + "/llm/v1"
    assert env["RAMEN_LLM_MODEL"] == "gpt-4o-mini" and env["RAMEN_LLM_API_KEY"].startswith("rjt_")
    assert ORG_KEY not in json.dumps(env) and ORG_KEY not in json.dumps(job)
    assert env["RAMEN_LLM_API_KEY"] not in json.dumps(job)  # the judge token is scrubbed from the job record
    assert llm_proxy.judge_claims(env["RAMEN_LLM_API_KEY"]) == ("demo", "zone-a", job["id"])
    assert any("through the console's LLM proxy" in ln for ln in job["log"])
    ld = demo.get("/api/v1/environments?group=demo").json()[0]["last_deploy"]
    assert ld["judge"]["zone-a"][0] == job["id"]
    # a settings push keeps the token the zone already runs
    assert demo.put("/api/v1/groups/demo/guardrails", json={"required": True}).json()["pushed"] == {"demo/zone-a": "ok"}
    assert env_of(job)["RAMEN_LLM_API_KEY"] == env["RAMEN_LLM_API_KEY"]
    # tokens rotate per deploy: the previous one drains, the one before that is refused
    first = env["RAMEN_LLM_API_KEY"]
    assert demo.post(CHAT, json=MSG, headers=bearer(first)).status_code == 200
    deploy(demo)
    assert demo.post(CHAT, json=MSG, headers=bearer(first)).status_code == 200
    second = env_of(deploy(demo))["RAMEN_LLM_API_KEY"]
    assert second != first and demo.post(CHAT, json=MSG, headers=bearer(second)).status_code == 200
    r = demo.post(CHAT, json=MSG, headers=bearer(first))
    assert r.status_code == 401 and "no longer current" in r.json()["detail"]


def test_without_a_console_url_llm_rails_fail_at_sync(demo, ws, tmp_path, monkeypatch):  # noqa: F811
    monkeypatch.delenv(llm_proxy.INTERNAL_ENV, raising=False)
    monkeypatch.delenv("RAMEN_PUBLIC_URL", raising=False)
    write_rails(tmp_path, LLM)
    demo.put(ORG, json={"openai": {"api_key": ORG_KEY}})
    job = deploy(demo)
    assert job["status"] == "error" and "no console URL" in job["error"]
    # a push without a URL warns and keeps no credential
    services = demo.app.state.services
    g = asyncio.run(services.get_group("demo"))
    env = asyncio.run(services.get_env("demo", "prod"))
    lines = []
    otel_cfg = asyncio.run(otel.get_config(services.store))
    cfg = asyncio.run(deploy_mod.zone_config(services, g, env, "zone-a", otel_cfg, {}, "", lines.append))
    assert "RAMEN_LLM_API_KEY" not in cfg and any("no console URL" in ln for ln in lines)


def test_ramen_names_in_the_repo_env_file_fail_the_deploy(demo, ws, tmp_path):  # noqa: F811
    root = tmp_path / "buckets" / "demo" / "mcp"
    root.mkdir(parents=True, exist_ok=True)
    (root / "env.yaml").write_text("# comment\nDB_URL: '{{$demo.DB}}'\n\nTOKEN_TTL: 5\n")
    assert deploy(demo)["status"] == "ok"
    (root / "env.yaml").write_text("DB_URL: x\nRAMEN_LLM_BASE_URL: https://evil.example/v1\n")
    job = deploy(demo)
    assert job["status"] == "error" and "mcp/env.yaml:2: RAMEN_LLM_BASE_URL is a Ramen name" in job["error"]
    assert not any("zone zone-a: deploying" in ln for ln in job["log"])
    (root / "env.yaml").unlink()
    (root / ".env").write_text("export ramen_session_secret=x\n")
    job = deploy(demo)
    assert job["status"] == "error" and "mcp/.env:1: ramen_session_secret" in job["error"]


# the bearer matrix --------------------------------------------------------------------------------------------------


def test_bearer_matrix(demo, ws, tmp_path, upstream):  # noqa: F811
    seen, _ = upstream
    judge = env_of(rails_deployed(demo, tmp_path))["RAMEN_LLM_API_KEY"]
    r = demo.post(CHAT, json=MSG, headers=bearer(judge))
    assert r.status_code == 200 and r.json() == OK
    assert str(seen[-1].url) == "https://api.openai.com/v1/chat/completions"
    assert seen[-1].headers["authorization"] == f"Bearer {ORG_KEY}"
    assert json.loads(seen[-1].content)["max_tokens"] == llm_proxy.MAX_TOKENS
    with TestClient(demo.app) as anon:
        assert anon.post(CHAT, json=MSG, headers=bearer(judge, **{"ramen-group": "demo"})).status_code == 200
        assert anon.post(CHAT, json=MSG, headers=bearer(judge, **{"ramen-group": "other"})).status_code == 403
        assert anon.post(f"{CHAT}?group=other", json=MSG, headers=bearer(judge)).status_code == 403
        # a token for another group signed with this group's secret, another group's zone, a token never minted
        payload, mac = judge[4:].split(".")
        forged = "rjt_" + base64.urlsafe_b64encode(b"other/zone-a/x").decode().rstrip("=") + "." + mac
        assert anon.post(CHAT, json=MSG, headers=bearer(forged)).status_code == 401
        ghost = llm_proxy.judge_token(secret(demo), "ghost", "zone-a", "x")
        assert anon.post(CHAT, json=MSG, headers=bearer(ghost)).status_code == 401
        other = llm_proxy.judge_token(secret(demo, "other"), "other", "zone-a", "x")
        assert anon.post(CHAT, json=MSG, headers=bearer(other)).status_code == 401  # never deployed
        stale = llm_proxy.judge_token(secret(demo), "demo", "zone-b", "x")
        assert anon.post(CHAT, json=MSG, headers=bearer(stale)).status_code == 401
        assert anon.post(CHAT, json=MSG, headers=bearer("rjt_garbage")).status_code == 401
        # no bearer at all
        assert anon.post(CHAT, json=MSG).status_code == 401
        assert anon.get("/llm/v1/models").status_code == 401
    # a member's OAuth token, an MCP key of the group, a member's console session naming the group
    assert demo.post(CHAT, json=MSG, headers=bearer(token_for(demo))).status_code == 200
    key = demo.post("/api/v1/groups/demo/mcp-keys", json={"name": "ci"}).json()["key"]
    with TestClient(demo.app) as anon:
        assert anon.post(CHAT, json=MSG, headers=bearer(key)).status_code == 200
    make_user(demo, "m@x", "mcp_user", ["demo"])
    make_user(demo, "o@x", "mcp_user", ["other"])
    with TestClient(demo.app) as m:
        login(m, "m@x", PW)
        assert m.post(CHAT, json=MSG, headers={"ramen-group": "demo"}).status_code == 200
    with TestClient(demo.app) as o:
        login(o, "o@x", PW)
        r = o.post(CHAT, json=MSG, headers={"ramen-group": "demo"})
        assert r.status_code == 403  # an outsider
    assert all(ORG_KEY not in str(s.url) for s in seen)


def test_the_in_cluster_judge_call_needs_tls_too(monkeypatch, tmp_path, cloud):  # noqa: F811
    """D86 §39.3: the workers reach /llm/v1 over TLS; the plain-http exemption is gone."""
    for k, v in {
        "RAMEN_STORE": "memory",
        "RAMEN_ADMIN_EMAIL": "root@ramen.local",
        "RAMEN_ADMIN_PASSWORD": "rootpw",
        "RAMEN_BACKUP_ROOT": str(tmp_path / "b"),
        "RAMEN_BUCKET_ROOT": str(tmp_path / "k"),
        "RAMEN_COOKIE_SECURE": "1",
    }.items():
        monkeypatch.setenv(k, v)
    with TestClient(create_app(store=make_store(), cloud=cloud), base_url="http://console.ramen-system.svc") as c:
        r = c.post(CHAT, json=MSG, headers=bearer("rjt_x"))
        assert r.status_code == 403 and "HTTPS" in r.text
    with TestClient(create_app(store=make_store(), cloud=cloud), base_url="https://console.ramen-system.svc") as c:
        r = c.post(CHAT, json=MSG, headers=bearer("rjt_x"))
        assert r.status_code == 401 and "HTTPS" not in r.text  # reached the route over TLS


# models, limits, errors, audit ------------------------------------------------------------------------------------


def test_models_are_enforced_and_listed(demo, ws, tmp_path, upstream):  # noqa: F811
    seen, _ = upstream
    judge = env_of(rails_deployed(demo, tmp_path))["RAMEN_LLM_API_KEY"]
    r = demo.post(CHAT, json={**MSG, "model": "gpt-5"}, headers=bearer(judge))
    assert r.status_code == 403 and "not allowed" in r.json()["detail"] and seen == []
    r = demo.post(CHAT, json={**MSG, "model": "gpt-4o"}, headers=bearer(judge))
    assert r.status_code == 200 and json.loads(seen[-1].content)["model"] == "gpt-4o"
    body = {k: v for k, v in MSG.items() if k != "model"}
    assert demo.post(CHAT, json=body, headers=bearer(judge)).status_code == 200
    assert json.loads(seen[-1].content)["model"] == "gpt-4o-mini"  # the scope's default
    r = demo.get("/llm/v1/models", headers=bearer(judge))
    assert r.status_code == 200 and [m["id"] for m in r.json()["data"]] == ["gpt-4o-mini", "gpt-4o"]
    assert r.json()["data"][0]["owned_by"] == "openai (org scope)"
    demo.put(ORG, json={"openai": {"api_key": ""}})  # no provider left anywhere
    assert demo.get("/llm/v1/models", headers=bearer(judge)).status_code == 403
    r = demo.post(CHAT, json=MSG, headers=bearer(judge))
    assert r.status_code == 403 and "no LLM provider" in r.json()["detail"]


def test_bodies_are_checked_and_capped(demo, ws, tmp_path, upstream):  # noqa: F811
    seen, _ = upstream
    h = bearer(env_of(rails_deployed(demo, tmp_path))["RAMEN_LLM_API_KEY"])
    for bad in (
        [1],
        {**MSG, "stream": True},
        {"model": "gpt-4o-mini"},
        {**MSG, "messages": ["x"]},
        {**MSG, "messages": [{"role": "robot"}]},
        {**MSG, "model": 5},
        {**MSG, "max_tokens": 0},
        {**MSG, "max_tokens": True},
        {**MSG, "n": 9},
    ):
        assert demo.post(CHAT, json=bad, headers=h).status_code == 400, bad
    assert demo.post(CHAT, content=b"{nope", headers={**h, "Content-Type": "application/json"}).status_code == 400
    big = b" " * (llm_proxy.MAX_BODY + 1)
    assert demo.post(CHAT, content=big, headers={**h, "Content-Type": "application/json"}).status_code == 413
    assert demo.post(CHAT, content=b"{}", headers={**h, "Content-Length": "x"}).status_code == 400
    r = demo.post(CHAT, json={**MSG, "max_tokens": 100000, "tools": [{"type": "function"}], "n": 2}, headers=h)
    sent = json.loads(seen[-1].content)
    assert r.status_code == 200 and sent["max_tokens"] == 4096 and "tools" not in sent and sent["n"] == 2
    demo.post(CHAT, json={**MSG, "max_completion_tokens": 8000}, headers=h)
    sent = json.loads(seen[-1].content)
    assert sent["max_completion_tokens"] == 4096 and "max_tokens" not in sent


def test_rate_limit_upstream_errors_and_audit(demo, ws, tmp_path, upstream, monkeypatch):  # noqa: F811
    seen, state = upstream
    h = bearer(env_of(rails_deployed(demo, tmp_path))["RAMEN_LLM_API_KEY"])
    state["status"] = 500
    r = demo.post(CHAT, json=MSG, headers=h)
    assert r.status_code == 502 and r.json() == {"error": "provider answered HTTP 500", "upstream_status": 500}
    state["status"] = httpx.ConnectError("down")
    r = demo.post(CHAT, json=MSG, headers=h)
    assert r.status_code == 502 and r.json()["upstream_status"] is None and "ConnectError" in r.json()["error"]
    state.update(status=200, body=[1])
    assert demo.post(CHAT, json=MSG, headers=h).json()["upstream_status"] == 200
    state["body"] = OK
    monkeypatch.setenv(llm_proxy.PER_MIN_ENV, "2")
    monkeypatch.setattr(llm_proxy, "AUDIT_EVERY", 3)
    llm_proxy.LIMIT.reset()
    assert demo.post(CHAT, json=MSG, headers=h).status_code == 200
    assert demo.post(CHAT, json=MSG, headers=bearer(token_for(demo))).status_code == 200  # the same group's budget
    r = demo.post(CHAT, json=MSG, headers=h)
    assert r.status_code == 429 and r.headers["retry-after"] == "60"
    monkeypatch.setenv(llm_proxy.PER_MIN_ENV, "100")
    for _ in range(3):
        assert demo.post(CHAT, json=MSG, headers=h).status_code == 200
    rows = [a for a in demo.get("/api/v1/audit?limit=500").json() if a["action"] == "llm.proxy"]
    assert len(rows) == 3  # the first call ever, then calls 1 and 4 after the reset (the 429 counted too)
    assert {"group:demo", "model:gpt-4o-mini", "scope:org", "zone:zone-a"} <= set(rows[0]["tags"])
    assert rows[0]["user"] == "judge:zone-a" and ORG_KEY not in json.dumps(rows) and "Answer" not in json.dumps(rows)


# the token answer ---------------------------------------------------------------------------------------------------


def test_the_token_answer_points_the_bridge_at_the_proxy_without_a_key(demo, ws, tmp_path, upstream):  # noqa: F811
    from tests.test_oauth_server import consent, exchange, pkce, register

    rails_deployed(demo, tmp_path)
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert t["guardrails"]["client_llm"] == {
        "provider": "openai_compatible",
        "base_url": "http://testserver/llm/v1",
        "model": "gpt-4o-mini",
    }
    assert ORG_KEY not in json.dumps(t)
    # the bridge's call: its own access token as the key
    assert demo.post(CHAT, json=MSG, headers=bearer(t["access_token"])).status_code == 200


# provider mapping ---------------------------------------------------------------------------------------------------


def res_of(cfg, provider, model=None):
    return llm_scope.resolve({}, cfg, {}, "guardrails", provider, model)


def call(res, payload=None, answer=None, status=200):
    seen = []

    def handler(req):
        seen.append(req)
        return httpx.Response(status, json=answer if answer is not None else OK)

    llm_proxy.TRANSPORT = httpx.MockTransport(handler)
    try:
        payload, _ = llm_proxy.clean(payload or {**MSG, "temperature": 0})
        return asyncio.run(llm_proxy.forward(res, payload)), seen
    finally:
        llm_proxy.TRANSPORT = None


def test_openai_compatible_azure_and_foundry_mapping(monkeypatch):
    out, seen = call(res_of({"openai": {"api_key": "k", "base_url": "https://p.example/v1"}}, "openai"))
    assert out == OK and str(seen[0].url) == "https://p.example/v1/chat/completions"
    compat = {"base_url": "http://vllm:8000/v1", "headers": '{"X-Org": "o"}', "allowed_models": ["llama3"]}
    _, seen = call(res_of({"openai_compatible": compat}, "openai_compatible"))
    assert str(seen[0].url) == "http://vllm:8000/v1/chat/completions" and seen[0].headers["x-org"] == "o"
    assert "authorization" not in seen[0].headers and json.loads(seen[0].content)["model"] == "llama3"
    _, seen = call(res_of({"openai_compatible": {**compat, "api_key": "c"}}, "openai_compatible"))
    assert seen[0].headers["authorization"] == "Bearer c"
    az = {"api_key": "z", "host": "r.openai.azure.com", "allowed_models": ["dep 1"]}
    _, seen = call(res_of({"azure_openai": {**az, "allowed_models": ["dep1"]}}, "azure_openai"))
    assert str(seen[0].url) == (
        "https://r.openai.azure.com/openai/deployments/dep1/chat/completions?api-version=2024-10-21"
    )
    assert seen[0].headers["api-key"] == "z" and "authorization" not in seen[0].headers
    monkeypatch.setenv("RAMEN_LLM_API_VERSION", "2025-01-01")
    _, seen = call(
        res_of({"foundry": {"api_key": "f", "endpoint": "https://f.example", "deployment": "gpt-4o"}}, "foundry")
    )
    assert str(seen[0].url) == "https://f.example/openai/deployments/gpt-4o/chat/completions?api-version=2025-01-01"


def test_anthropic_is_translated_both_ways():
    payload = {
        "model": "claude-haiku-4-5",
        "temperature": 0,
        "stop": "\n\n",
        "messages": [
            {"role": "system", "content": "You judge."},
            {"role": "developer", "content": [{"type": "text", "text": "Strictly."}]},
            {"role": "user", "content": "first"},
            {"role": "tool", "content": "tool says"},
            {"role": "assistant", "content": None},
            {"role": "user", "content": {"x": 1}},
        ],
    }
    answer = {
        "id": "msg_1",
        "model": "claude-haiku-4-5",
        "content": [
            {"type": "thinking", "thinking": ""},
            {"type": "text", "text": "Answer: "},
            {"type": "text", "text": "No"},
        ],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 2},
    }
    out, seen = call(res_of({"anthropic": {"api_key": "a"}}, "anthropic"), payload, answer)
    assert str(seen[0].url) == llm_proxy.ANTHROPIC_URL
    assert seen[0].headers["x-api-key"] == "a" and seen[0].headers["anthropic-version"] == "2023-06-01"
    sent = json.loads(seen[0].content)
    assert sent == {
        "model": "claude-haiku-4-5",
        "max_tokens": 4096,
        "system": "You judge.\n\nStrictly.",
        "messages": [
            {"role": "user", "content": "first\n\ntool says"},
            {"role": "assistant", "content": ""},
            {"role": "user", "content": '{"x": 1}'},
        ],
        "temperature": 0,
        "stop_sequences": ["\n\n"],
    }
    assert out["object"] == "chat.completion" and out["choices"][0]["message"] == {
        "role": "assistant",
        "content": "Answer: No",
    }
    assert out["choices"][0]["finish_reason"] == "stop" and out["usage"]["total_tokens"] == 12
    # only a system message; top_p without temperature; a list of stops; an odd answer
    only = {"model": "claude-haiku-4-5", "top_p": 0.5, "stop": ["x"], "messages": [{"role": "system", "content": "s"}]}
    out, seen = call(res_of({"anthropic": {"api_key": "a"}}, "anthropic"), only, {"stop_reason": "max_tokens"})
    sent = json.loads(seen[0].content)
    assert sent["messages"] == [{"role": "user", "content": "s"}] and "system" not in sent and sent["top_p"] == 0.5
    assert sent["stop_sequences"] == ["x"] and out["choices"][0]["finish_reason"] == "length"
    assert out["choices"][0]["message"]["content"] == "" and out["model"] == "claude-haiku-4-5"


def test_anthropic_models_without_sampling_are_asked_again():
    seen = []

    def handler(req):
        seen.append(json.loads(req.content))
        if "temperature" in seen[-1]:
            return httpx.Response(400, json={"error": {"message": "temperature is not supported for this model"}})
        return httpx.Response(200, json={"content": [{"type": "text", "text": "Yes"}], "stop_reason": "refusal"})

    llm_proxy.TRANSPORT = httpx.MockTransport(handler)
    try:
        res = res_of({"anthropic": {"api_key": "a", "allowed_models": ["claude-opus-5-5"]}}, "anthropic")
        out = asyncio.run(llm_proxy.forward(res, {**MSG, "temperature": 0, "max_tokens": 10}))
    finally:
        llm_proxy.TRANSPORT = None
    assert len(seen) == 2 and "temperature" not in seen[1] and out["choices"][0]["finish_reason"] == "content_filter"


def test_vertex_and_bedrock_use_the_consoles_identity(monkeypatch):
    monkeypatch.setattr(llm_proxy, "vertex_token", lambda: "adc-token")
    monkeypatch.setattr(llm_proxy, "bedrock_token", lambda region: f"bedrock-api-key-{region}")
    _, seen = call(res_of({"vertex": {"project": "p", "location": "europe-west4"}}, "vertex"))
    assert str(seen[0].url) == (
        "https://europe-west4-aiplatform.googleapis.com/v1/projects/p/locations/europe-west4/endpoints/openapi"
        "/chat/completions"
    )
    assert seen[0].headers["authorization"] == "Bearer adc-token"
    assert json.loads(seen[0].content)["model"] == "google/gemini-2.0-flash"
    _, seen = call(
        res_of({"vertex": {"project": "p", "location": "global", "allowed_models": ["meta/llama"]}}, "vertex")
    )
    assert (
        str(seen[0].url).startswith("https://aiplatform.googleapis.com/")
        and json.loads(seen[0].content)["model"] == "meta/llama"
    )
    _, seen = call(res_of({"bedrock": {"region": "us-west-2"}}, "bedrock"))
    assert str(seen[0].url) == "https://bedrock-runtime.us-west-2.amazonaws.com/openai/v1/chat/completions"
    assert seen[0].headers["authorization"] == "Bearer bedrock-api-key-us-west-2"

    def broken():
        raise OSError("metadata server down")

    monkeypatch.setattr(llm_proxy, "vertex_token", broken)
    with pytest.raises(llm_proxy.Upstream, match="no cloud credentials on the console: OSError"):
        call(res_of({"vertex": {"project": "p"}}, "vertex"))


def test_the_metadata_service_is_refused_at_call_time(monkeypatch):
    res = res_of(
        {"openai_compatible": {"base_url": "http://ok.example/v1", "allowed_models": ["m"]}}, "openai_compatible"
    )
    res["entry"]["base_url"] = "http://169.254.169.254/v1"  # as if stored before the save-time check
    with pytest.raises(llm_proxy.Upstream, match="metadata"):
        call(res)

    async def resolves(host):
        return ["169.254.169.254"]

    monkeypatch.setattr(llm_proxy.llm_models, "addresses", resolves)
    res["entry"]["base_url"] = "http://rebind.example/v1"
    with pytest.raises(llm_proxy.Upstream, match="metadata"):
        asyncio.run(llm_proxy.forward(res, llm_proxy.clean(MSG)[0]))  # no fake transport: DNS is asked


def test_the_cloud_token_helpers(monkeypatch):
    import botocore.credentials
    import botocore.session

    real = llm_proxy.aws_credentials
    monkeypatch.setattr(botocore.session, "get_session", lambda: type("S", (), {"get_credentials": lambda self: "c"})())
    assert real() == "c"
    monkeypatch.setattr(llm_proxy, "aws_credentials", lambda: botocore.credentials.Credentials("AKIDEXAMPLE", "secret"))
    tok = llm_proxy.bedrock_token("us-east-1")
    decoded = base64.b64decode(tok.removeprefix("bedrock-api-key-")).decode()
    assert (
        decoded.startswith("bedrock.us-east-1.amazonaws.com/?Action=CallWithBearerToken")
        and "X-Amz-Signature" in decoded
    )
    assert decoded.endswith("&Version=1")
    monkeypatch.setattr(llm_proxy, "aws_credentials", lambda: None)
    with pytest.raises(RuntimeError, match="no AWS credentials"):
        llm_proxy.bedrock_token("us-east-1")

    class Creds:
        valid, token = False, None

        def refresh(self, request):
            self.valid, self.token = True, "fresh"

    import google.auth

    monkeypatch.setattr(google.auth, "default", lambda scopes: (Creds(), "p"))
    llm_proxy._ADC.clear()
    assert llm_proxy.vertex_token() == "fresh" and llm_proxy.vertex_token() == "fresh"
    llm_proxy._ADC.clear()


# evals allowlist ----------------------------------------------------------------------------------------------------


def test_inline_assertions_may_not_read_the_environment():
    ok = "tests:\n  - assert:\n      - type: javascript\n        value: output.length > 3\n"
    assert evals.validate(ok, []) == []
    for code in (
        "process.env.OPENAI_API_KEY",
        "process['env']",
        "import os; os.environ['K']",
        "os.getenv('K')",
        "from os import environ",
        "Deno.env.get('K')",
    ):
        doc = {
            "defaultTest": {"assert": [{"type": "python", "value": code}]},
            "tests": [{"assert": [{"type": "assert-set", "assert": [{"type": "not-javascript", "value": code}]}]}],
        }
        errs = evals.validate(json.dumps(doc), [])
        assert errs == [
            "defaultTest.assert[0]: inline assertion code reads the environment: refused",
            "tests[0].assert[0].assert[0]: inline assertion code reads the environment: refused",
        ], code
    assert evals.validate(json.dumps({"tests": [{"assert": [{"type": "contains", "value": "process.env"}]}]}), []) == []
    assert evals.env_reads([{"type": "python", "value": "getenv"}]) == ["[0]"]
    assert evals.env_reads({"type": "python", "value": "getenv"}) == ["assert"]
