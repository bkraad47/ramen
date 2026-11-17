"""CONTRACTS §36.2 / §36.3 console side: `mcp/guardrails.yaml` + `mcp/guardrails/` as a client policy (pack, version,
token answer with `client_llm`, `/.well-known/ramen-policy`, the console's own policy meta), the workers'
RAMEN_GUARDRAILS_*, the group's `guardrails` settings, and the deploy's RAMEN_LLM_* written only when a rail needs a
model (no provider anywhere = the deploy fails at the sync step)."""

import asyncio
import base64
import io
import json
import tarfile
from pathlib import Path

from fastapi.testclient import TestClient

from ramen_console import apps, guardrails
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixtures
from tests.test_oauth_server import consent, exchange, pkce, register
from tests.test_v100c_evals import DEPLOY

SETTINGS = "/api/v1/groups/demo/guardrails"
ORG = "/api/v1/config/llm-providers"
GROUP = "/api/v1/groups/demo/llm-providers"
KEY = "rails-key-not-real-0001"
STATIC = "engine: nemo\nconfig: mcp/guardrails\nfail: closed\ntools:\n  word_count: {pre: true, post: true}\n"
LLM = (
    "engine: nemo\nconfig: mcp/guardrails\ntools:\n  iris_outliers: {post: [self_check_output]}\n"
    "  iris_report:\n    pre: [self_check_input, jailbreak_detection, {topical: {topics: [iris]}}]\n"
    "    post: {hallucination: {}}\n"
)
FLOWS = "define flow block outlier dump\n  bot refuse\n"


def write_rails(tmp_path, text=STATIC, files=(("config.yml", "models: []\n"), ("rails/flows.co", FLOWS))):
    root = tmp_path / "buckets" / "demo"
    (root / guardrails.PATH).parent.mkdir(parents=True, exist_ok=True)
    (root / guardrails.PATH).write_text(text)
    for rel, body in files:
        p = root / guardrails.DIR / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


def env_file(job):
    return Path(job["result"]["zone-a"]["env_file"]).read_text()


def token(demo, cid):
    verifier, challenge = pkce()
    return exchange(demo, cid, consent(demo, cid, challenge), verifier).json()


def loaded(demo):
    return asyncio.run(guardrails.load(demo.app.state.services, "demo"))


# pure functions ---------------------------------------------------------------------------------------------------


def test_llm_rails_are_found_in_lists_and_mappings():
    assert guardrails.llm_rails(STATIC) == {} and guardrails.engine(STATIC) == "nemo"
    assert guardrails.llm_rails(LLM) == {
        "iris_outliers": ["self_check_output"],
        "iris_report": ["hallucination", "jailbreak_detection", "self_check_input", "topical"],
    }
    assert guardrails.llm_rails("pre: [self_check_input]\ntools: {a: {pre: true}}\n") == {
        "__default__": ["self_check_input"]
    }
    assert guardrails.llm_rails("tools:\n  a: {pre: [my_own_rail]}\n") == {}  # a custom flow needs no model
    assert guardrails.llm_rails(": not yaml [") == {} and guardrails.llm_rails("- a\n- b\n") == {}
    assert guardrails.engine(": [") == "" and guardrails.engine("[1]") == ""
    assert guardrails.needs_llm(None) is False and guardrails.needs_llm({"llm_rails": {"a": ["x"]}}) is True


def test_pack_is_a_stable_base64_gzip_tar_bounded_at_64_kib():
    files = {"config.yml": b"models: []\n", "rails/flows.co": FLOWS.encode()}
    packed = guardrails.pack(STATIC, files)
    assert packed == guardrails.pack(STATIC, dict(reversed(list(files.items()))))  # order and time do not matter
    with tarfile.open(fileobj=io.BytesIO(base64.b64decode(packed)), mode="r:gz") as tar:
        names = tar.getnames()
        assert names == ["guardrails.yaml", "guardrails/config.yml", "guardrails/rails/flows.co"]
        assert tar.extractfile("guardrails.yaml").read().decode() == STATIC
        assert all(m.mtime == 0 and m.uid == 0 for m in tar.getmembers())
    assert guardrails.pack(STATIC, {}) != packed
    import secrets

    import pytest

    with pytest.raises(ValueError, match="64 KiB"):
        guardrails.pack(STATIC, {"big.co": secrets.token_bytes(70 * 1024)})  # incompressible


def test_bytecode_and_pycache_are_not_packed_and_do_not_move_the_version(demo, ws, tmp_path):  # noqa: F811
    assert guardrails.packable("rails/flows.co") and guardrails.packable("actions.py")
    for rel in ("__pycache__/actions.cpython-312.pyc", "actions.pyc", "sub/__pycache__/x.pyo", "x.pyo", "../a", ""):
        assert not guardrails.packable(rel), rel
    write_rails(tmp_path)
    before = loaded(demo)
    v = guardrails.version(before, {})
    pyc = tmp_path / "buckets" / "demo" / guardrails.DIR / "__pycache__" / "actions.cpython-312.pyc"
    pyc.parent.mkdir(parents=True)
    pyc.write_bytes(b"\x00bytecode")  # the local worker's first call leaves this in the bucket copy
    (tmp_path / "buckets" / "demo" / guardrails.DIR / "actions.pyc").write_bytes(b"\x01")
    after = loaded(demo)
    assert after["files"] == before["files"] and after["pack"] == before["pack"]
    assert guardrails.version(after, {}) == v and "__pycache__" not in json.dumps(sorted(after["files"]))


def test_settings_version_worker_env_client_info_and_policy():
    assert guardrails.settings({}) == {"llm": "", "model": "", "required": False}
    assert guardrails.settings({"guardrails": {"llm": "openai", "required": 1}}) == {
        "llm": "openai",
        "model": "",
        "required": True,
    }
    ld = {"text": STATIC, "files": {}, "pack": guardrails.pack(STATIC, {}), "llm_rails": {}, "engine": "nemo"}
    v = guardrails.version(ld, {})
    assert len(v) == 12 and int(v, 16) >= 0 and v == guardrails.version(dict(ld), {})
    assert guardrails.version(ld, {"guardrails": {"required": True}}) != v
    assert guardrails.version(ld, {"guardrails": {"llm": "openai"}}) != v
    assert guardrails.version(ld, {"guardrails": {"model": "gpt-4o"}}) != v
    assert guardrails.version({**ld, "pack": guardrails.pack(STATIC + "#", {})}, {}) != v
    too_big = {**ld, "pack": None, "error": "too big"}  # the version still follows the files
    assert guardrails.version(too_big, {}) != guardrails.version({**too_big, "files": {"a": b"1"}}, {})
    assert guardrails.worker_env(None, {}) == {}
    assert guardrails.worker_env(ld, {"guardrails": {"required": True}}) == {
        "RAMEN_GUARDRAILS_VERSION": guardrails.version(ld, {"guardrails": {"required": True}}),
        "RAMEN_GUARDRAILS_REQUIRED": "1",
    }
    assert guardrails.client_info(None, {}) is None and guardrails.policy(None, {}) is None
    assert guardrails.client_info(ld, {}) == {"version": v, "required": False, "config": ld["pack"]}
    info = guardrails.client_info(too_big, {}, {"provider": "openai_compatible", "model": "m", "base_url": "u"})
    assert "config" not in info and info["error"] == "too big" and info["client_llm"]["base_url"] == "u"
    assert guardrails.policy(ld, {}) == {"version": v, "required": False}
    # the credential leaves only when required, or when nemo runs LLM rails
    llm_ld = {**ld, "llm_rails": {"a": ["self_check_output"]}}
    assert guardrails.wants_client_llm(None, {}) is False and guardrails.wants_client_llm(ld, {}) is False
    assert guardrails.wants_client_llm(ld, {"guardrails": {"required": True}}) is True
    assert guardrails.wants_client_llm(llm_ld, {}) is True
    assert guardrails.wants_client_llm({**llm_ld, "engine": "policy"}, {}) is False


# deploy -----------------------------------------------------------------------------------------------------------


def test_no_file_means_no_guardrails_keys(demo, ws):  # noqa: F811
    job = deploy(demo)
    assert job["status"] == "ok" and "RAMEN_GUARDRAILS_" not in env_file(job) and "RAMEN_LLM_" not in env_file(job)
    cid = register(demo)
    assert "guardrails" not in token(demo, cid)
    with TestClient(demo.app) as anon:
        assert "guardrails" not in anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a").json()


def test_static_rails_write_the_version_but_no_llm_env(demo, ws, tmp_path):  # noqa: F811
    write_rails(tmp_path)
    job = deploy(demo)
    assert job["status"] == "ok", job
    v = guardrails.version(loaded(demo), {})
    text = env_file(job)
    assert f"RAMEN_GUARDRAILS_VERSION={v}\n" in text and "RAMEN_GUARDRAILS_REQUIRED=0\n" in text
    assert "RAMEN_LLM_" not in text and not any("LLM rails" in ln for ln in job["log"])


def test_llm_rails_need_a_provider_group_first_then_org_else_the_deploy_fails_at_sync(demo, ws, tmp_path, monkeypatch):  # noqa: F811,E501
    monkeypatch.setenv("RAMEN_CONSOLE_INTERNAL_URL", "http://console.ramen-system.svc.cluster.local:80")
    write_rails(tmp_path, LLM)
    job = deploy(demo)
    assert job["status"] == "error", job
    assert job["error"].endswith(
        "mcp/guardrails.yaml: guardrails: no LLM provider: set one on the group or ask a super admin"
    )
    assert not any("zone zone-a: deploying" in ln for ln in job["log"])  # failed at the sync step, before any zone
    demo.put(ORG, json={"openai": {"api_key": "org-key-not-real", "allowed_models": ["gpt-4o-mini", "gpt-4o"]}})
    job = deploy(demo)
    assert job["status"] == "ok", job
    text = env_file(job)  # §38.1: the console's judge proxy, never the provider key
    assert "RAMEN_LLM_PROVIDER=openai_compatible\n" in text and "RAMEN_LLM_MODEL=gpt-4o-mini\n" in text
    assert "RAMEN_LLM_API_KEY=rjt_" in text and "org-key-not-real" not in text
    assert "RAMEN_LLM_BASE_URL=http://console.ramen-system.svc.cluster.local:80/llm/v1\n" in text
    assert any(
        "LLM rails (iris_outliers: self_check_output, iris_report: " in ln and "(org scope)" in ln for ln in job["log"]
    )
    assert "org-key-not-real" not in json.dumps(job)  # scrubbed from the job record
    # the group's own provider wins, with its own model
    demo.put(GROUP, json={"anthropic": {"api_key": KEY, "allowed_models": ["claude-sonnet-4-5"]}})
    r = demo.put(SETTINGS, json={"llm": "anthropic", "model": "claude-sonnet-4-5"})
    assert r.status_code == 200, r.text
    job = deploy(demo)
    text = env_file(job)
    assert "RAMEN_LLM_PROVIDER=openai_compatible\n" in text and KEY not in text
    assert "RAMEN_LLM_MODEL=claude-sonnet-4-5\n" in text and "RAMEN_LLM_ENDPOINT" not in text
    assert KEY not in json.dumps(job)
    judge = next(ln for ln in text.splitlines() if ln.startswith("RAMEN_LLM_API_KEY="))
    # the pushed config keeps the credential (a reload must not lose the rails' model)
    r = demo.put(SETTINGS, json={"required": True})
    assert r.json()["pushed"] == {"demo/zone-a": "ok"}
    text = env_file(job)
    assert f"{judge}\n" in text and "RAMEN_GUARDRAILS_REQUIRED=1\n" in text
    # the provider disappears: the deploy refuses, a push warns and keeps the version
    demo.put(GROUP, json={"anthropic": {"api_key": ""}})
    job = deploy(demo)
    assert job["status"] == "error" and "'anthropic' is set on neither the group nor the org" in job["error"]


# settings, token, policy ------------------------------------------------------------------------------------------


def test_group_settings_round_trip_422_rbac_audit_and_push(demo, ws, tmp_path):  # noqa: F811
    write_rails(tmp_path)
    assert demo.get("/api/v1/groups/demo").json().get("guardrails") is None
    job = deploy(demo)
    v0 = guardrails.version(loaded(demo), {})
    assert f"RAMEN_GUARDRAILS_VERSION={v0}\n" in env_file(job)
    r = demo.put(SETTINGS, json={"required": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"llm": "", "model": "", "required": True, "pushed": {"demo/zone-a": "ok"}}
    g = demo.get("/api/v1/groups/demo").json()
    assert g["guardrails"] == {"llm": "", "model": "", "required": True}
    assert f"RAMEN_GUARDRAILS_VERSION={guardrails.version(loaded(demo), g)}\n" in env_file(job)
    assert "RAMEN_GUARDRAILS_REQUIRED=1\n" in env_file(job)
    assert demo.put(SETTINGS, json={"required": True}).json()["pushed"] == {}  # unchanged: no push
    assert demo.put(SETTINGS, json={"llm": "openai"}).status_code == 422  # configured nowhere
    demo.put(ORG, json={"openai": {"api_key": "k-not-real", "allowed_models": ["gpt-4o-mini"]}})
    r = demo.put(SETTINGS, json={"llm": "openai", "model": "gpt-5"})
    assert r.status_code == 422 and "one of gpt-4o-mini" in r.json()["detail"]
    r = demo.put(SETTINGS, json={"llm": "openai", "model": "gpt-4o-mini"})
    assert r.status_code == 200 and r.json()["pushed"] == {"demo/zone-a": "ok"}  # llm/model are in the version
    assert demo.put(SETTINGS, json={"model": ""}).json()["pushed"] == {"demo/zone-a": "ok"}
    assert demo.put(SETTINGS, json={"required": "maybe"}).status_code == 422
    assert demo.put("/api/v1/groups/nope/guardrails", json={"required": True}).status_code == 404
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "group.guardrails"]
    assert a and {"group:demo", "required:True"} <= set(a[0]["tags"]) and "k-not-real" not in json.dumps(a)
    page = demo.get("/groups/demo").text
    assert "Guardrails" in page and 'hx-put="/api/v1/groups/demo/guardrails"' in page and "-32029" in page
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as u:
        login(u, "ga@x", PW)
        assert u.put(SETTINGS, json={"required": False}).status_code == 200
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.put(SETTINGS, json={"required": False}).status_code == 403
        assert 'hx-put="/api/v1/groups/demo/guardrails"' not in u.get("/groups/demo").text  # admins only


def test_the_token_answer_the_policy_document_and_the_console_meta(demo, ws, tmp_path):  # noqa: F811
    write_rails(tmp_path)
    cid = register(demo)
    t = token(demo, cid)
    ld = loaded(demo)
    assert t["guardrails"] == {"version": guardrails.version(ld, {}), "required": False, "config": ld["pack"]}
    assert "client_llm" not in t["guardrails"]  # static rails, not required: no credential leaves
    with TestClient(demo.app) as anon:
        r = anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a")
        assert r.json()["guardrails"] == {"version": guardrails.version(ld, {}), "required": False}
        assert "config" not in json.dumps(r.json()["guardrails"])
    # required → client_llm when a provider resolves; absent (never a failed sign-in) when none does
    demo.put(SETTINGS, json={"required": True})
    t = token(demo, cid)
    assert t["guardrails"]["required"] is True and "client_llm" not in t["guardrails"]
    demo.put(GROUP, json={"openai": {"api_key": KEY, "base_url": "https://proxy.example/v1"}})
    t = token(demo, cid)
    assert t["guardrails"]["client_llm"] == {  # §38.1: the console's proxy, the person's own token as the key
        "provider": "openai_compatible",
        "model": "gpt-4o-mini",
        "base_url": "http://testserver/llm/v1",
    }
    assert KEY not in json.dumps(t)
    g = demo.get("/api/v1/groups/demo").json()
    assert t["guardrails"]["version"] == guardrails.version(ld, g)
    with TestClient(demo.app) as anon:
        r = anon.get("/.well-known/ramen-policy?group=demo&zone=zone-a")
        assert r.json()["guardrails"] == {"version": guardrails.version(ld, g), "required": True} and KEY not in r.text
    # nemo with LLM rails, not required: the credential goes out too (the bridge runs the rails locally)
    demo.put(SETTINGS, json={"required": False})
    write_rails(tmp_path, LLM)
    t = token(demo, cid)
    assert t["guardrails"]["required"] is False and t["guardrails"]["client_llm"]["provider"] == "openai_compatible"
    # the console's own call carries the version (§36.3 `_meta["io.ramen/guardrails"]`)
    meta = asyncio.run(apps.policy_meta(demo.app.state.services, "demo", g))
    assert meta["io.ramen/guardrails"] == {"version": guardrails.version(loaded(demo), g)}
    # too large a pack: the version stays, `error` replaces `config`
    import secrets

    write_rails(tmp_path, STATIC, (("big.co", secrets.token_hex(guardrails.MAX_PACK)),))
    t = token(demo, cid)
    assert "config" not in t["guardrails"] and "64 KiB" in t["guardrails"]["error"]
    job = deploy(demo)
    assert job["status"] == "ok" and any("warning" in ln and "64 KiB" in ln for ln in job["log"])
