"""CONTRACTS §36.2 console side: `allowed_models` on the org providers, a group's own providers (`PUT/GET
/api/v1/groups/{g}/llm-providers`, vaulted under `config/groups/<g>/llm_providers`), the group → org → error resolution
(`llm_scope.resolve`) and its use by the scans and the evals."""

import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from ramen_console import llm_providers as lp
from ramen_console import llm_scope
from ramen_console.storage.encrypted import nested_field
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - fixtures
from tests.test_deploy_gates import ws  # noqa: F401 - fixtures
from tests.test_v100c_evals import DEPLOY, write_evals
from tests.test_v100c_evals import runner as evals_runner  # noqa: F401 - fixture
from tests.test_v100c_skillspector import runner as scan_runner  # noqa: F401 - fixture
from tests.test_v100c_skillspector import write_skill

ORG = "/api/v1/config/llm-providers"
GROUP = "/api/v1/groups/demo/llm-providers"
KEY = "group-key-not-real-0001"
ORG_KEY = "org-key-not-real-0002"


def deploy(c, **body):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a", **body}).json()["id"])


# allowed models + defaults ------------------------------------------------------------------------------------------


def test_allowed_models_merge_public_form_and_the_defaults_table():
    cfg = lp.merge({}, {"openai": {"api_key": "k", "allowed_models": ["gpt-4o-mini", "gpt-4o", "gpt-4o-mini"]}})
    assert cfg["openai"]["allowed_models"] == ["gpt-4o-mini", "gpt-4o"]  # de-duplicated, order kept
    assert lp.public(cfg)["openai"]["allowed_models"] == ["gpt-4o-mini", "gpt-4o"]
    cfg = lp.merge(cfg, {"openai": {"allowed_models": "o3-mini, gpt-4.1\n gpt-4o"}})  # the form's text
    assert cfg["openai"]["allowed_models"] == ["o3-mini", "gpt-4.1", "gpt-4o"]
    assert "allowed_models" not in lp.merge(cfg, {"openai": {"allowed_models": []}})["openai"]  # [] clears
    assert "allowed_models" not in lp.merge(cfg, {"openai": {"allowed_models": ""}})["openai"]
    assert lp.env(cfg) == {"OPENAI_API_KEY": "k"}  # never an env var
    with pytest.raises(ValueError):
        lp.merge({}, {"openai": {"allowed_models": ["has space"]}})
    with pytest.raises(ValueError):
        lp.merge({}, {"openai": {"allowed_models": [1]}})
    assert lp.from_form({"anthropic_allowed_models": "a, b", "anthropic_api_key": ""}) == {
        "anthropic": {"allowed_models": "a, b"}
    }
    # §36.2 defaults: one table
    assert lp.default_model("openai") == "gpt-4o-mini" and lp.default_model("anthropic") == "claude-haiku-4-5"
    assert lp.default_model("vertex") == "gemini-2.0-flash"
    assert lp.default_model("bedrock") == "anthropic.claude-3-haiku-20240307-v1:0"
    assert lp.default_model("foundry", {"deployment": "my-dep"}) == "my-dep" and lp.default_model("foundry") is None
    assert lp.default_model("azure_openai") is None and lp.default_model("openai_compatible") is None
    assert lp.default_model("openai_compatible", {"allowed_models": ["llama3"]}) == "llama3"
    assert lp.allowed_models({}, "openai") == ["gpt-4o-mini"]  # empty = the default only
    assert lp.allowed_models({"openai": {"allowed_models": ["x"]}}, "openai") == ["x"]
    assert lp.allowed_models({}, "openai_compatible") == []
    # configured = the credential is there, not just a list of models
    assert lp.configured({"openai": {"allowed_models": ["x"]}}) == []
    assert lp.configured({"openai_compatible": {"api_key": "k"}}) == [] and lp.is_configured(
        {"vertex": {"project": "p"}}, "vertex"
    )
    assert lp.configured({"foundry": {"api_key": "k", "endpoint": "https://r.openai.azure.com"}}) == ["foundry"]


def test_the_org_card_round_trips_allowed_models(demo):
    r = demo.put(ORG, json={"openai": {"api_key": ORG_KEY, "allowed_models": ["gpt-4o-mini", "gpt-4o"]}})
    assert r.status_code == 200 and r.json()["openai"]["allowed_models"] == ["gpt-4o-mini", "gpt-4o"]
    page = demo.get("/config").text
    assert 'name="openai_allowed_models"' in page and "gpt-4o-mini, gpt-4o" in page and "Guardrails (NeMo" in page
    assert "provider set" in page
    r = demo.put(ORG, json={"openai_allowed_models": "", "openai_api_key": ""})  # the form: empty list clears
    assert r.json()["openai"] == {"api_key_set": True, "base_url": "", "allowed_models": []}
    assert demo.put(ORG, json={"openai": {"allowed_models": ["bad model"]}}).status_code == 422


# group providers ----------------------------------------------------------------------------------------------------


def test_group_providers_round_trip_vault_audit_and_rbac(demo, caplog):
    assert demo.get(GROUP).json() == lp.public({})
    r = demo.put(GROUP, json={"anthropic": {"api_key": KEY, "allowed_models": ["claude-haiku-4-5"]}})
    assert r.status_code == 200, r.text
    assert r.json()["anthropic"] == {"api_key_set": True, "allowed_models": ["claude-haiku-4-5"]}
    assert demo.get(ORG).json()["anthropic"]["api_key_set"] is False  # the org doc is another doc
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "group.llm_providers"]
    assert len(a) == 1 and sorted(a[0]["tags"]) == ["group:demo", "provider:anthropic"]
    # the same NESTED rule: encrypted at rest, decrypted on load, scrubbed from logs
    assert nested_field("config", lp.group_doc("demo")) == "api_key" and nested_field("config", "nope") is None
    store = demo.app.state.store
    raw = asyncio.run(store.inner.get("config", lp.group_doc("demo")))
    assert raw["anthropic"]["api_key"].startswith("enc:") and KEY not in json.dumps(raw)
    assert asyncio.run(lp.load(store, "demo"))["anthropic"]["api_key"] == KEY
    page = demo.get("/groups/demo").text
    assert "LLM providers (group)" in page and 'hx-put="/api/v1/groups/demo/llm-providers"' in page
    assert 'name="anthropic_api_key" type="password"' in page and "configured" in page
    seen = demo.get(GROUP).text + page + demo.get("/api/v1/audit").text + caplog.text
    assert KEY not in seen
    assert demo.put(GROUP, json={"gemini": {"api_key": "x"}}).status_code == 422
    assert demo.put("/api/v1/groups/nope/llm-providers", json={}).status_code == 404
    assert demo.get("/api/v1/groups/nope/llm-providers").status_code == 404
    r = demo.put(GROUP, json={"anthropic_api_key": "", "anthropic_allowed_models": "claude-sonnet-4-5"})  # form
    assert r.json()["anthropic"] == {"api_key_set": True, "allowed_models": ["claude-sonnet-4-5"]}
    assert demo.put(GROUP, json={"anthropic": {"api_key": ""}}).json()["anthropic"]["api_key_set"] is False
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "o@x", "group_admin", ["other"])
    with TestClient(demo.app) as u:
        login(u, "ga@x", PW)
        assert u.get(GROUP).status_code == 200 and u.put(GROUP, json={"openai": {"api_key": "k"}}).status_code == 200
    for who in ("v@x", "o@x"):
        with TestClient(demo.app) as u:
            login(u, who, PW)
            assert u.get(GROUP).status_code == 403 and u.put(GROUP, json={}).status_code == 403
    demo.delete("/api/v1/groups/demo")
    assert asyncio.run(store.get("config", lp.group_doc("demo"))) is None  # gone with the group


# resolution ---------------------------------------------------------------------------------------------------------

G = {"openai": {"api_key": "gk", "allowed_models": ["gpt-4o"]}}
ORG_CFG = {
    "openai": {"api_key": "ok"},
    "anthropic": {"api_key": "ak", "allowed_models": ["claude-haiku-4-5", "claude-sonnet-4-5"]},
}


def test_resolution_matrix_group_then_org_then_error():
    r = llm_scope.resolve({}, G, ORG_CFG, "guardrails")
    assert (r["scope"], r["provider"], r["model"]) == ("group", "openai", "gpt-4o") and r["env"] == {
        "OPENAI_API_KEY": "gk"
    }
    r = llm_scope.resolve({}, G, ORG_CFG, "guardrails", "anthropic")  # the group lacks it → the org
    assert (r["scope"], r["provider"], r["model"]) == ("org", "anthropic", "claude-haiku-4-5")
    assert r["env"] == {"OPENAI_API_KEY": "ok", "ANTHROPIC_API_KEY": "ak"}  # the whole org scope for a Job
    r = llm_scope.resolve({}, {}, ORG_CFG, "evals")  # no group providers → the org, its first configured provider
    assert (r["scope"], r["provider"]) == ("org", "anthropic")
    with pytest.raises(llm_scope.NoProvider, match="no LLM provider: set one on the group or ask a super admin"):
        llm_scope.resolve({}, {}, {}, "guardrails")
    with pytest.raises(llm_scope.NoProvider, match="'bedrock'"):
        llm_scope.resolve({}, G, ORG_CFG, "skillspector", "bedrock")
    assert llm_scope.pick_scope({}, {}) is None and llm_scope.configured(G, ORG_CFG) == ["anthropic", "openai"]
    # the model must be allowed by the CHOSEN scope: the org allows gpt-4o-mini, the group only gpt-4o
    with pytest.raises(llm_scope.BadModel, match="one of gpt-4o"):
        llm_scope.resolve({}, G, ORG_CFG, "guardrails", "openai", "gpt-4o-mini")
    assert (
        llm_scope.resolve({}, G, ORG_CFG, "guardrails", "anthropic", "claude-sonnet-4-5")["model"]
        == "claude-sonnet-4-5"
    )
    with pytest.raises(llm_scope.BadModel, match="claude-haiku-4-5, claude-sonnet-4-5"):
        llm_scope.resolve({}, G, ORG_CFG, "guardrails", "anthropic", "claude-opus-4")
    # no default: guardrails need a model, a scan does not
    compat = {"openai_compatible": {"base_url": "http://ollama:11434/v1"}}
    with pytest.raises(llm_scope.BadModel, match="no default model"):
        llm_scope.resolve({}, compat, {}, "guardrails")
    assert llm_scope.resolve({}, compat, {}, "skillspector")["model"] is None
    with pytest.raises(llm_scope.BadModel, match="none yet"):
        llm_scope.resolve({}, compat, {}, "skillspector", "openai_compatible", "llama3")


def test_worker_env_and_client_llm_name_the_proxy_never_a_key():
    """§38.1: whatever the provider, workers and the bridge get the console's judge proxy and the resolved model."""
    for cfg, provider, model in (
        ({"openai": {"api_key": "k", "base_url": "http://p/v1"}}, "openai", "gpt-4o-mini"),
        ({"anthropic": {"api_key": "a"}}, "anthropic", "claude-haiku-4-5"),
        (
            {"azure_openai": {"api_key": "z", "host": "r.openai.azure.com", "allowed_models": ["dep1"]}},
            "azure_openai",
            "dep1",
        ),
        ({"vertex": {"project": "p"}}, "vertex", "gemini-2.0-flash"),
        ({"bedrock": {"region": "us-west-2"}}, "bedrock", "anthropic.claude-3-haiku-20240307-v1:0"),
    ):
        res = llm_scope.resolve({}, cfg, {}, "guardrails", provider)
        assert llm_scope.worker_env(res, "http://c/llm/v1", "rjt_t") == {
            "RAMEN_LLM_PROVIDER": "openai_compatible",
            "RAMEN_LLM_BASE_URL": "http://c/llm/v1",
            "RAMEN_LLM_API_KEY": "rjt_t",
            "RAMEN_LLM_MODEL": model,
        }
        assert llm_scope.client_llm(res, "https://c/llm/v1") == {
            "provider": "openai_compatible",
            "base_url": "https://c/llm/v1",
            "model": model,
        }


# scans and evals use the scope --------------------------------------------------------------------------------------


def test_the_scan_uses_the_group_provider_before_the_org_one(demo, scan_runner, tmp_path):  # noqa: F811
    write_skill(tmp_path)
    demo.put(ORG, json={"openai_compatible": {"base_url": "http://org:11434/v1", "api_key": "org-k"}})
    demo.put(GROUP, json={"openai_compatible": {"base_url": "http://grp:11434/v1", "api_key": KEY}})
    assert demo.put("/api/v1/groups/demo/skillspector", json={"llm": "openai_compatible"}).status_code == 200
    job = deploy(demo)
    assert job["status"] == "ok", job
    env = scan_runner.specs[0][2]["secret"]["stringData"]
    assert env["SKILLSPECTOR_COMPAT_BASE_URL"] == "http://grp:11434/v1" and env["SKILLSPECTOR_COMPAT_API_KEY"] == KEY
    assert "org-k" not in json.dumps(scan_runner.specs[0][2]) and KEY not in json.dumps(job)
    # a provider only the GROUP configures may be named (the org-only lookup is gone)
    demo.put(ORG, json={"openai_compatible": {"base_url": "", "api_key": ""}})
    assert demo.put("/api/v1/groups/demo/skillspector", json={"llm": "openai_compatible"}).status_code == 200
    assert demo.put("/api/v1/groups/demo/skillspector", json={"llm": "anthropic"}).status_code == 422
    # ... and one configured nowhere any more is the run's error, never a silent static scan
    demo.put(GROUP, json={"openai_compatible": {"base_url": "", "api_key": ""}})
    job = deploy(demo)
    assert job["status"] == "ok" and "no LLM provider" in job["result"]["zone-a"]["skillspector"]["error"]
    assert len(scan_runner.specs) == 1 and any("scan error: skillspector: no LLM provider" in ln for ln in job["log"])
    assert demo.put("/api/v1/groups/demo/skillspector", json={"gate": True}).status_code == 422  # llm is stale now
    g = demo.app.state.store
    doc = asyncio.run(g.get("groups", "demo"))
    doc["skillspector"]["gate"] = True
    asyncio.run(g.put("groups", "demo", doc))
    job = deploy(demo)
    assert job["status"] == "error" and "no LLM provider" in job["error"]


def test_the_evals_get_the_resolved_scope_env_or_none(demo, evals_runner, tmp_path):  # noqa: F811
    write_evals(tmp_path)
    job = deploy(demo)  # no provider anywhere: the evals still run, LLM-free
    assert job["status"] == "ok", job
    env = evals_runner.specs[-1][2]["secret"]["stringData"]
    assert not any(k.endswith("_API_KEY") for k in env) and env["RAMEN_GROUP"] == "demo"
    demo.put(ORG, json={"anthropic": {"api_key": "org-k"}})
    demo.put(GROUP, json={"openai": {"api_key": KEY}})
    deploy(demo)
    env = evals_runner.specs[-1][2]["secret"]["stringData"]
    assert env["OPENAI_API_KEY"] == KEY and "ANTHROPIC_API_KEY" not in env  # the group scope, whole
    demo.put(GROUP, json={"openai": {"api_key": ""}})
    deploy(demo)
    env = evals_runner.specs[-1][2]["secret"]["stringData"]
    assert env["ANTHROPIC_API_KEY"] == "org-k" and "OPENAI_API_KEY" not in env  # then the org's
    # §33.4a's allowlist is checked against the chosen scope: the group names openai, the file wants anthropic
    demo.put(GROUP, json={"openai": {"api_key": KEY}})
    write_evals(tmp_path, "providers: [anthropic:messages:claude-haiku-4-5]\ntests: []\n")
    job = deploy(demo)
    assert job["status"] == "error" and "no anthropic key is configured" in job["error"]
