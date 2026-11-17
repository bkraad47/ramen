"""CONTRACTS §29.1: Config → LLM providers (Anthropic, OpenAI, Azure OpenAI) — keys are console secrets shown as
set/not set, PUT merges, "" clears, audited with `provider:` tags only, fed to evals as env, never logged."""

import json

import pytest
from fastapi.testclient import TestClient

from ramen_console import llm_providers as lp
from ramen_console.storage.encrypted import NESTED
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures

CFG = "/api/v1/config/llm-providers"
KEY = "anthropic-test-key-not-real-0001"
BARE = {  # §30.1: every provider is answered, the new ones as plain fields / set flags
    "anthropic": {"api_key_set": False},
    "openai": {"api_key_set": False, "base_url": ""},
    "azure_openai": {"api_key_set": False, "host": ""},
    "vertex": {"project": "", "location": ""},
    "bedrock": {"region": ""},
    "foundry": {"api_key_set": False, "endpoint": "", "deployment": ""},
    "openai_compatible": {"api_key_set": False, "base_url": "", "headers_set": False},
}
EMPTY = {p: {**v, "allowed_models": []} for p, v in BARE.items()}  # §36.2: plus the allowed models, [] = default only


def expect(**over):
    return {**EMPTY, **{p: {"allowed_models": [], **v} for p, v in over.items()}}


def test_merge_clean_and_env():
    cfg = lp.merge({}, {"anthropic": {"api_key": KEY}, "openai": {"api_key": "sk-o", "base_url": "http://x/v1/"}})
    assert cfg == {"anthropic": {"api_key": KEY}, "openai": {"api_key": "sk-o", "base_url": "http://x/v1"}}
    cfg = lp.merge(cfg, {"openai": {"base_url": ""}, "azure_openai": {"api_key": "az", "host": "h.openai.azure.com"}})
    assert cfg["openai"] == {"api_key": "sk-o"} and cfg["azure_openai"] == {
        "api_key": "az",
        "host": "h.openai.azure.com",
    }
    assert lp.merge(cfg, {"anthropic": {"api_key": None}})["anthropic"] == {"api_key": KEY}  # None = keep
    assert "anthropic" not in lp.merge(cfg, {"anthropic": {"api_key": ""}})  # "" clears; an empty provider goes
    assert lp.env(cfg) == {
        "ANTHROPIC_API_KEY": KEY,
        "OPENAI_API_KEY": "sk-o",
        "AZURE_API_KEY": "az",
        "AZURE_API_HOST": "h.openai.azure.com",
    }
    assert lp.env({}) == {}
    assert lp.public(cfg) == expect(
        anthropic={"api_key_set": True},
        openai={"api_key_set": True, "base_url": ""},
        azure_openai={"api_key_set": True, "host": "h.openai.azure.com"},
    )
    with pytest.raises(ValueError):
        lp.merge({}, {"gemini": {"api_key": "x"}})
    with pytest.raises(ValueError):
        lp.merge({}, {"openai": {"base_url": "ftp://x"}})
    with pytest.raises(ValueError):
        lp.merge({}, {"azure_openai": {"host": "bad host"}})
    assert NESTED[("config", lp.DOC)] == "api_key"  # §24.5: vaulted on the clouds, Fernet locally


def test_round_trip_shows_set_or_not_set_and_never_the_key(demo, caplog):
    assert demo.get(CFG).json() == EMPTY
    r = demo.put(CFG, json={"anthropic": {"api_key": KEY}, "openai": {"base_url": "https://proxy.example/v1"}})
    assert r.status_code == 200, r.text
    assert r.json()["anthropic"] == {"api_key_set": True, "allowed_models": []}
    assert r.json()["openai"] == {"api_key_set": False, "base_url": "https://proxy.example/v1", "allowed_models": []}
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "config.llm_providers"]
    assert len(a) == 1 and sorted(a[0]["tags"]) == ["provider:anthropic", "provider:openai"]
    r = demo.put(CFG, json={"openai": {"api_key": "sk-openai-0000000000"}})  # merges: anthropic stays
    assert r.json()["anthropic"]["api_key_set"] and r.json()["openai"]["api_key_set"]
    page = demo.get("/config").text
    assert "LLM providers" in page and 'name="anthropic_api_key" type="password"' in page
    # §38.1: Vertex/Bedrock rails run as the console's identity (terraform console_vertex / console_bedrock)
    assert "builder identity" not in page and "console_vertex" in page and "console_bedrock" in page
    assert page.count("set (hidden)") >= 2
    r = demo.put(CFG, json={"anthropic": {"api_key": ""}})  # "" clears
    assert r.json()["anthropic"] == {"api_key_set": False, "allowed_models": []}
    assert demo.put(CFG, json={"openai": {"base_url": "not a url"}}).status_code == 422
    assert demo.put(CFG, json={"mistral": {"api_key": "x"}}).status_code == 422
    seen = demo.get(CFG).text + demo.get("/config").text + demo.get("/api/v1/audit").text + caplog.text
    assert KEY not in seen and "sk-openai-0000000000" not in seen
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "config.llm_providers"]
    assert len(a) == 3 and all(t.startswith("provider:") for x in a for t in x["tags"])


def test_the_form_posts_flat_fields_and_an_empty_field_keeps_the_key(demo):
    demo.put(CFG, json={"anthropic": {"api_key": KEY}})
    r = demo.put(CFG, json={"anthropic_api_key": "", "azure_openai_host": "h.openai.azure.com", "openai_base_url": ""})
    assert r.status_code == 200, r.text
    assert r.json()["anthropic"]["api_key_set"] is True and r.json()["azure_openai"]["host"] == "h.openai.azure.com"
    page = demo.get("/config").text
    assert "LLM providers" in page and "h.openai.azure.com" in page
    assert '"api_key": ""' in page  # the Clear button of a set key


def test_part_two_providers_merge_validate_and_export_only_what_is_set():
    cfg = lp.merge(
        {},
        {
            "vertex": {"project": "my-proj", "location": "us-central1"},
            "bedrock": {"region": "us-west-2"},
            "foundry": {"endpoint": "https://r.openai.azure.com/", "api_key": "fk", "deployment": "gpt-4o"},
            "openai_compatible": {
                "base_url": "http://ollama:11434/v1",
                "api_key": "compat-key-001",
                "headers": {"X-Org": "t"},
            },
        },
    )
    assert cfg["foundry"]["endpoint"] == "https://r.openai.azure.com"
    assert cfg["openai_compatible"]["headers"] == '{"X-Org": "t"}'
    assert lp.env(cfg) == {
        "GOOGLE_CLOUD_PROJECT": "my-proj",
        "GOOGLE_CLOUD_LOCATION": "us-central1",
        "AWS_REGION": "us-west-2",
        "AZURE_OPENAI_ENDPOINT": "https://r.openai.azure.com",
        "AZURE_OPENAI_API_KEY": "fk",
        "AZURE_OPENAI_DEPLOYMENT": "gpt-4o",
        "SKILLSPECTOR_COMPAT_BASE_URL": "http://ollama:11434/v1",
        "SKILLSPECTOR_COMPAT_API_KEY": "compat-key-001",
        "SKILLSPECTOR_COMPAT_HEADERS": '{"X-Org": "t"}',
    }
    assert lp.configured(cfg) == ["bedrock", "foundry", "openai_compatible", "vertex"]
    pub = lp.public(cfg)
    assert pub["vertex"] == {"project": "my-proj", "location": "us-central1", "allowed_models": []}
    assert pub["bedrock"] == {"region": "us-west-2", "allowed_models": []}
    assert pub["foundry"] == {
        "api_key_set": True,
        "endpoint": "https://r.openai.azure.com",
        "deployment": "gpt-4o",
        "allowed_models": [],
    }
    assert pub["openai_compatible"] == {
        "api_key_set": True,
        "base_url": "http://ollama:11434/v1",
        "headers_set": True,
        "allowed_models": [],
    }
    assert "compat-key-001" not in json.dumps(pub) and "X-Org" not in json.dumps(pub)
    # headers as the form's JSON text; "" or {} clears them; a provider with nothing left disappears
    cfg = lp.merge(cfg, {"openai_compatible": {"headers": '{"A": "b", "C-D": "e"}'}})
    assert cfg["openai_compatible"]["headers"] == '{"A": "b", "C-D": "e"}'
    assert "headers" not in lp.merge(cfg, {"openai_compatible": {"headers": {}}})["openai_compatible"]
    assert "bedrock" not in lp.merge(cfg, {"bedrock": {"region": ""}})
    assert lp.env(lp.merge({}, {"vertex": {"project": "p"}})) == {"GOOGLE_CLOUD_PROJECT": "p"}  # only what is set
    for bad in (
        {"vertex": {"project": "has space"}},
        {"bedrock": {"region": "us west"}},
        {"foundry": {"endpoint": "r.openai.azure.com"}},
        {"openai_compatible": {"headers": "not json"}},
        {"openai_compatible": {"headers": '{"A": 1}'}},
        {"openai_compatible": {"headers": '{"bad header": "x"}'}},
        {"openai_compatible": {"headers": "[1]"}},
        {"vertex": {"api_key": "x"}},
    ):
        with pytest.raises(ValueError):
            lp.merge({}, bad)
    form = lp.from_form(
        {"vertex_project": "p", "openai_compatible_headers": "", "openai_compatible_api_key": "", "bedrock_region": ""}
    )
    assert form == {"vertex": {"project": "p"}, "bedrock": {"region": ""}}  # empty key/headers = unchanged


def test_part_two_round_trip_and_the_more_providers_card(demo, caplog):
    r = demo.put(
        CFG,
        json={
            "bedrock": {"region": "us-west-2"},
            "openai_compatible": {
                "base_url": "http://ollama:11434/v1",
                "api_key": "ck-0000000000",
                "headers": {"X-Org": "t"},
            },
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["bedrock"] == {"region": "us-west-2", "allowed_models": []}
    assert r.json()["openai_compatible"] == {
        "api_key_set": True,
        "base_url": "http://ollama:11434/v1",
        "headers_set": True,
        "allowed_models": [],
    }
    a = [x for x in demo.get("/api/v1/audit").json() if x["action"] == "config.llm_providers"]
    assert sorted(a[-1]["tags"]) == ["provider:bedrock", "provider:openai_compatible"]
    page = demo.get("/config").text
    assert "Azure AI Foundry" in page and "us-west-2" in page and "headers set (hidden)" in page  # one card (1.0.0c)
    assert 'name="foundry_api_key" type="password"' in page and 'name="vertex_project"' in page
    r = demo.put(
        CFG, json={"foundry_endpoint": "https://r.openai.azure.com", "foundry_api_key": "", "vertex_project": ""}
    )
    assert r.status_code == 200 and r.json()["foundry"] == {
        "api_key_set": False,
        "endpoint": "https://r.openai.azure.com",
        "deployment": "",
        "allowed_models": [],
    }
    assert (
        demo.put(CFG, json={"openai_compatible": {"headers": ""}}).json()["openai_compatible"]["headers_set"] is False
    )
    assert demo.put(CFG, json={"vertex": {"project": "no spaces allowed here!"}}).status_code == 422
    seen = demo.get(CFG).text + demo.get("/config").text + demo.get("/api/v1/audit").text + caplog.text
    assert "ck-0000000000" not in seen and "X-Org" not in seen


@pytest.mark.parametrize("by", ["group_admin", "viewer"])
def test_only_a_super_admin_reads_or_writes(app, demo, by):  # noqa: F811
    make_user(demo, "u@x", by, ["demo"])
    with TestClient(app) as u:
        login(u, "u@x", PW)
        assert u.get(CFG).status_code == 403 and u.put(CFG, json={}).status_code == 403


def test_the_stored_doc_is_encrypted_at_rest(demo):
    demo.put(CFG, json={"anthropic": {"api_key": KEY}})
    store = demo.app.state.store
    import asyncio

    raw = asyncio.run(store.inner.get("config", lp.DOC)) if hasattr(store, "inner") else None
    if raw is not None:
        assert KEY not in json.dumps(raw)
    assert asyncio.run(lp.load(store))["anthropic"]["api_key"] == KEY
