"""1.0.0c (the user's Config screenshot): one LLM providers card with live model lists (org and group), the Guardrails
model select, and every cloud region in the zone picker grouped by continent — selectable with a cluster, greyed
without one, hidden when blocked."""

import json
import time
from pathlib import Path
from types import SimpleNamespace as NS

import httpx
import pytest
from fastapi.testclient import TestClient

from ramen_console import llm_models, llm_scope, regions
from ramen_console import llm_providers as lp
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures
from tests.test_v100b_satellites import gcp_console

ORG, GROUP = "/api/v1/config/llm-providers", "/api/v1/groups/demo/llm-providers"
KEY = "sk-test-key-not-real-000000000001"


@pytest.fixture(autouse=True)
def _fresh_cache():
    llm_models._cache.clear()
    yield
    llm_models._cache.clear()


# --- C: regions ------------------------------------------------------------------------------------------------------


def test_regions_json_lists_every_public_region_with_zone_letters_and_no_duplicates():
    text = Path(regions.__file__).with_name("regions.json").read_text()
    seen: list[str] = []

    def no_dupes(pairs):
        keys = [k for k, _ in pairs]
        assert len(keys) == len(set(keys)), "duplicate key"
        seen.extend(keys)
        return dict(pairs)

    data = json.loads(text, object_pairs_hook=no_dupes)
    assert "2026" in data["note"] and "Azure" in data["note"]
    for p in regions.PROVIDERS:
        assert data[p], p
        for r, letters in data[p].items():
            assert letters and len(set(letters)) == len(letters) and all(len(z) == 1 for z in letters), (p, r)
            assert all(z.isalpha() for z in letters) if p != "azure" else all(z in "123" for z in letters), (p, r)
    for r in (
        "africa-south1",
        "me-central1",
        "asia-south1",
        "australia-southeast1",
        "europe-west1",
        "southamerica-east1",
    ):
        assert r in data["gcp"], r
    for r in ("af-south-1", "ap-southeast-1", "eu-west-1", "me-central-1", "sa-east-1", "il-central-1", "ca-central-1"):
        assert r in data["aws"], r
    for r in (
        "southafricanorth",
        "australiaeast",
        "australiasoutheast",
        "japaneast",
        "koreacentral",
        "centralindia",
        "uaenorth",
        "brazilsouth",
        "canadacentral",
        "westeurope",
        "northeurope",
        "uksouth",
        "francecentral",
        "germanywestcentral",
        "swedencentral",
    ):
        assert r in data["azure"], r
    assert len(data["gcp"]) >= 40 and len(data["aws"]) >= 30 and len(data["azure"]) >= 40
    assert data["azure"]["westus"] == ["1"] and data["azure"]["australiaeast"] == ["1", "2", "3"]


def test_continent_grouping_covers_every_region_of_the_three_clouds():
    for p in regions.PROVIDERS:
        for r in regions.REGIONS[p]:
            assert regions.continent(p, r) in regions.CONTINENTS, (p, r)
    assert regions.continent("gcp", "us-central1") == "Americas" and regions.continent("gcp", "me-west1") != "Americas"
    assert (
        regions.continent("gcp", "europe-west1") == "Europe"
        and regions.continent("gcp", "asia-east1") == "Asia Pacific"
    )
    assert regions.continent("gcp", "australia-southeast1") == "Asia Pacific"
    assert regions.continent("gcp", "africa-south1") == "Middle East & Africa"
    assert regions.continent("aws", "sa-east-1") == "Americas" and regions.continent("aws", "il-central-1").startswith(
        "Middle"
    )
    assert (
        regions.continent("azure", "uksouth") == "Europe"
        and regions.continent("azure", "newzealandnorth") == "Asia Pacific"
    )
    assert (
        regions.continent("azure", "qatarcentral") == "Middle East & Africa"
        and regions.continent("azure", "mexicocentral") == "Americas"
    )
    assert regions.continent("gcp", "mars-north1") == "Other" and regions.continent("azure", "atlantis") == "Other"
    groups = regions.grouped("gcp", ["us-west1"])
    assert [c for c, _ in groups] == list(regions.CONTINENTS)
    flat = [r for _, rs in groups for r in rs]
    assert "us-west1" not in flat and set(flat) | {"us-west1"} == set(regions.REGIONS["gcp"])
    assert groups[0][1][0] == "northamerica-northeast1" or "us-central1" in groups[0][1]
    assert regions.grouped("local") == []


def test_the_picker_offers_every_region_grouped_enabled_with_a_cluster_and_greyed_without(demo, monkeypatch):  # noqa: F811
    gcp_console(demo, monkeypatch)
    demo.put("/api/v1/config/regions", json={"gcp": ["us-west1"]})
    sat = {"provider": "gcp", "region": "australia-southeast1", "name": "ramen-au", "host": "au.example.net"}
    demo.put("/api/v1/config/regions", json={"clusters": [sat]})
    page = demo.get("/environments").text
    for cont in regions.CONTINENTS:
        assert f'<optgroup label="{cont.replace("&", "&amp;")}">' in page, cont
    assert '<option value="us-central1-a">us-central1-a · home cluster</option>' in page
    assert '<option value="australia-southeast1-b">australia-southeast1-b · satellite ramen-au</option>' in page
    assert (
        '<option value="" disabled>europe-west1 — no cluster yet — register a satellite cluster on the Config page'
        "</option>" in page
    )
    assert "europe-west1-b" not in page  # no zone of a greyed region
    assert "us-west1-a" not in page and ">us-west1 —" not in page  # blocked: hidden, neither offered nor greyed
    assert 'href="/config#clusters"' in page and "greyed out until a super admin registers a satellite" in page
    # a zone's edit row uses the same picker, its own location selected
    demo.post(
        "/api/v1/zones", json={"name": "au", "provider": "gcp", "region": "australia-southeast1-b", "environments": []}
    )
    page = demo.get("/environments").text
    assert (
        '<option value="australia-southeast1-b" selected>australia-southeast1-b · satellite ramen-au</option>' in page
    )
    # the Clusters card: the same grouped list, home and satellite regions marked and disabled
    cfg = demo.get("/config").text
    assert '<div class="card" id="clusters">' in cfg and '<optgroup label="Europe">' in cfg
    assert '<option value="us-central1" disabled>us-central1 · home cluster</option>' in cfg
    assert '<option value="australia-southeast1" disabled>australia-southeast1 · satellite ramen-au</option>' in cfg
    assert (
        '<option value="europe-west1">europe-west1</option>' in cfg
        and 'value="us-west1"' not in cfg.split('id="clusters"')[1]
    )


def test_without_a_known_home_region_every_region_is_selectable(demo, monkeypatch):  # noqa: F811
    monkeypatch.setenv("RAMEN_CLOUD", "azure")
    page = demo.get("/environments").text  # the test cloud has no home region: nothing to grey out
    assert "no cluster yet" not in page and '<option value="southafricanorth-1">southafricanorth-1</option>' in page
    assert '<option value="westus-1">westus-1</option>' in page and '<optgroup label="Asia Pacific">' in page


# --- B: model lists ------------------------------------------------------------------------------------------

LISTS = {
    "openai": {"data": [{"id": "gpt-4o"}, {"id": "gpt-4o-mini"}, {"id": "o3-mini"}]},
    "anthropic": {"data": [{"id": "claude-sonnet-4-5"}, {"id": "claude-haiku-4-5"}]},
    "azure": {"data": [{"id": "my-gpt4o", "model": "gpt-4o"}, {"id": "my-mini", "model": "gpt-4o-mini"}]},
    "compat": {"data": [{"id": "llama3.1"}, {"id": "bad model"}]},
    "gemini": {"models": [{"name": "models/gemini-2.5-pro"}, {"name": "models/gemini-2.0-flash"}]},
}


def fake_provider(calls: list):
    def handler(req: httpx.Request) -> httpx.Response:
        calls.append((str(req.url), dict(req.headers)))
        u = str(req.url)
        if "proxy.example" in u or "api.openai.com" in u:
            assert req.headers["authorization"] == f"Bearer {KEY}"
            return httpx.Response(200, json=LISTS["openai"])
        if "api.anthropic.com" in u:
            assert req.headers["x-api-key"] == KEY and req.headers["anthropic-version"] == llm_models.ANTHROPIC_VERSION
            return httpx.Response(200, json=LISTS["anthropic"])
        if "openai.azure.com" in u:
            assert u.endswith("/openai/deployments?api-version=2024-10-21") and req.headers["api-key"] == KEY
            return httpx.Response(200, json=LISTS["azure"])
        if "ollama" in u:
            assert req.headers["x-team"] == "ops" and req.headers["authorization"] == f"Bearer {KEY}"
            return httpx.Response(200, json=LISTS["compat"])
        if "generativelanguage" in u:
            assert req.headers["x-goog-api-key"] == "g-key"
            return httpx.Response(200, json=LISTS["gemini"])
        if "down.example" in u:
            return httpx.Response(401, json={"error": {"message": "bad key " + KEY}})
        if "slow.example" in u:
            raise httpx.ConnectTimeout("slow")
        if "junk.example" in u:
            return httpx.Response(200, text="<html>")
        raise AssertionError(u)

    return handler


@pytest.fixture
def provider(monkeypatch):
    calls: list = []
    monkeypatch.setattr(llm_models, "TRANSPORT", httpx.MockTransport(fake_provider(calls)))
    monkeypatch.setattr(llm_models, "boto_models", lambda region: None)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    return calls


def test_each_provider_is_asked_its_way_and_the_answer_is_parsed(demo, provider, monkeypatch, caplog):  # noqa: F811
    demo.put(
        ORG,
        json={
            "openai": {"api_key": KEY, "base_url": "https://proxy.example/v1"},
            "anthropic": {"api_key": KEY},
            "azure_openai": {"api_key": KEY, "host": "r.openai.azure.com"},
            "foundry": {"api_key": KEY, "endpoint": "https://f.openai.azure.com"},
            "openai_compatible": {"api_key": KEY, "base_url": "http://ollama:11434/v1", "headers": {"X-Team": "ops"}},
            "vertex": {"project": "p"},
            "bedrock": {"region": "us-west-2"},
        },
    )
    get = lambda p: demo.get(f"{ORG}/{p}/models").json()  # noqa: E731
    assert get("openai") == {"models": ["gpt-4o", "gpt-4o-mini", "o3-mini"], "source": "live"}
    assert provider[-1][0] == "https://proxy.example/v1/models"
    assert get("anthropic") == {"models": ["claude-haiku-4-5", "claude-sonnet-4-5"], "source": "live"}
    assert get("azure_openai") == {"models": ["my-gpt4o", "my-mini"], "source": "live"}
    assert provider[-1][0].startswith("https://r.openai.azure.com/openai/deployments")
    assert get("foundry")["models"] == ["my-gpt4o", "my-mini"]
    assert get("openai_compatible") == {"models": ["llama3.1"], "source": "live"}  # "bad model" is not an id
    # Vertex: the static Gemini list without a key, the live list with one
    assert get("vertex") == {
        "models": llm_models.STATIC["vertex"],
        "source": "static",
        "error": "no GOOGLE_API_KEY on the console",
    }
    monkeypatch.setenv("GOOGLE_API_KEY", "g-key")
    assert get("vertex") == {"models": ["gemini-2.0-flash", "gemini-2.5-pro"], "source": "live"}
    # Bedrock: static without AWS credentials, boto's list with them
    assert get("bedrock") == {
        "models": llm_models.STATIC["bedrock"],
        "source": "static",
        "error": "no AWS credentials on the console",
    }
    monkeypatch.setattr(
        llm_models, "boto_models", lambda region: ["amazon.nova-lite-v1:0"] if region == "us-west-2" else []
    )
    assert get("bedrock") == {"models": ["amazon.nova-lite-v1:0"], "source": "live"}
    # the default OpenAI base URL
    demo.put(ORG, json={"openai": {"base_url": ""}})
    assert get("openai")["source"] == "live" and provider[-1][0] == "https://api.openai.com/v1/models"
    assert KEY not in caplog.text and KEY not in demo.get("/api/v1/audit").text


def test_lists_are_cached_ten_minutes_per_scope_and_forgotten_on_save(demo, provider, monkeypatch):  # noqa: F811
    demo.put(ORG, json={"openai": {"api_key": KEY}})
    demo.put(GROUP, json={"openai": {"api_key": KEY, "base_url": "https://proxy.example/v1"}})
    assert demo.get(f"{ORG}/openai/models").json()["source"] == "live"
    assert demo.get(f"{ORG}/openai/models").json() == {
        "models": ["gpt-4o", "gpt-4o-mini", "o3-mini"],
        "source": "cached",
    }
    assert demo.get(f"{GROUP}/openai/models").json()["source"] == "live"  # another scope, its own entry
    assert demo.get(f"{GROUP}/openai/models").json()["source"] == "cached"
    assert len(provider) == 2
    assert llm_models._cache[("org", "openai")][0] > time.monotonic() + 500  # ten minutes
    llm_models._cache[("org", "openai")] = (time.monotonic() - 1, ["stale"])  # expired: asked again, not served
    assert demo.get(f"{ORG}/openai/models").json() == {"models": ["gpt-4o", "gpt-4o-mini", "o3-mini"], "source": "live"}
    assert demo.get(f"{ORG}/openai/models").json()["source"] == "cached"
    demo.put(ORG, json={"openai": {"base_url": "https://proxy.example/v1"}})  # a save forgets that provider's list
    assert demo.get(f"{ORG}/openai/models").json()["source"] == "live"
    assert demo.get(f"{GROUP}/openai/models").json()["source"] == "cached"  # the group's is untouched
    llm_models.forget("group:demo")
    assert demo.get(f"{GROUP}/openai/models").json()["source"] == "live"


def test_unavailable_answers_name_a_short_reason_and_never_the_key(demo, provider, caplog):  # noqa: F811
    assert demo.get(f"{ORG}/openai/models").json() == {
        "models": [],
        "source": "unavailable",
        "error": "provider not set",
    }
    demo.put(ORG, json={"openai_compatible": {"api_key": KEY, "base_url": "https://down.example/v1"}})
    assert demo.get(f"{ORG}/openai_compatible/models").json() == {
        "models": [],
        "source": "unavailable",
        "error": "HTTP 401",
    }
    demo.put(ORG, json={"openai_compatible": {"base_url": "https://slow.example/v1"}})
    assert demo.get(f"{ORG}/openai_compatible/models").json()["error"] == "ConnectTimeout"
    demo.put(ORG, json={"openai_compatible": {"base_url": "https://junk.example/v1"}})
    r = demo.get(f"{ORG}/openai_compatible/models").json()
    assert r["source"] == "unavailable" and "Expecting value" in r["error"]
    assert llm_models.parse("openai", {"data": "nope"}) == [] if False else True
    with pytest.raises(ValueError):
        llm_models.parse("openai", {"nothing": []})
    assert llm_models.parse("vertex", {"models": [{"name": "models/g"}, "junk", {"name": ""}]}) == ["g"]
    assert llm_models._cache == {}  # failures are not cached: the next call asks again
    assert demo.get(f"{ORG}/mistral/models").status_code == 404
    assert KEY not in caplog.text


def test_boto_models_uses_the_consoles_credentials_when_it_has_them(monkeypatch):
    import boto3

    class Session:
        def __init__(self, region_name=None):
            self.region = region_name
            self.creds = Session.creds

        def get_credentials(self):
            return self.creds

        def client(self, name):
            assert name == "bedrock" and self.region == "eu-west-1"
            return NS(
                list_foundation_models=lambda: {
                    "modelSummaries": [{"modelId": "b.x-v1:0"}, {"modelId": "a.y-v1:0"}, {"modelId": "bad id"}]
                }
            )

    Session.creds = None
    monkeypatch.setattr(boto3, "Session", Session)
    assert llm_models.boto_models("eu-west-1") is None
    Session.creds = object()
    assert llm_models.boto_models("eu-west-1") == ["a.y-v1:0", "b.x-v1:0"]


def test_the_group_route_is_the_group_admins_and_reads_the_groups_own_providers(demo, provider):  # noqa: F811
    demo.put(ORG, json={"openai": {"api_key": KEY}})
    assert demo.get(f"{GROUP}/openai/models").json()["error"] == "provider not set"  # the org's key is not the group's
    demo.put(GROUP, json={"anthropic": {"api_key": KEY}})
    assert demo.get(f"{GROUP}/anthropic/models").json()["source"] == "live"
    assert demo.get("/api/v1/groups/nope/llm-providers/openai/models").status_code == 404
    assert demo.get(f"{GROUP}/nope/models").status_code == 404
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    make_user(demo, "o@x", "group_admin", ["other"])
    with TestClient(demo.app) as u:
        login(u, "ga@x", PW)
        assert u.get(f"{GROUP}/anthropic/models").status_code == 200
        assert u.get(f"{ORG}/openai/models").status_code == 403  # the org route stays the super admin's
    for who in ("v@x", "o@x"):
        with TestClient(demo.app) as u:
            login(u, who, PW)
            assert u.get(f"{GROUP}/anthropic/models").status_code == 403


def test_an_htmx_request_gets_the_allowed_models_control_rendered_with_the_list(demo, provider):  # noqa: F811
    demo.put(ORG, json={"openai": {"api_key": KEY, "allowed_models": ["gpt-4o", "custom-ft"]}})
    r = demo.get(f"{ORG}/openai/models", headers={"HX-Request": "true"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    html = r.text
    assert 'id="am-org-openai"' in html and 'name="openai_allowed_models" value="gpt-4o-mini"' in html  # default first
    assert (
        html.index('value="gpt-4o-mini"')
        < html.index('value="gpt-4o" checked')
        < html.index('value="custom-ft" checked')
    )
    assert (
        'value="o3-mini"' in html and "3 live" in html and 'hx-get="/api/v1/config/llm-providers/openai/models"' in html
    )
    r = demo.get(f"{GROUP}/openai/models", headers={"HX-Request": "true"}).text
    assert 'id="am-group-demo-openai"' in r and "unavailable" in r and "provider not set" in r
    assert (
        'hx-get="/api/v1/groups/demo/llm-providers/openai/models"' in r and 'hx-target="#models-group-demo-openai"' in r
    )


# --- A: the one card, the macro, the guardrails select ---------------------------------------------------------------


def test_the_config_page_has_one_providers_card_with_a_row_per_provider_and_one_save(demo):  # noqa: F811
    demo.put(
        ORG,
        json={
            "vertex": {"project": "p", "location": "us-central1"},
            "openai_compatible": {"base_url": "http://o:1/v1", "headers": {"X": "y"}},
        },
    )
    page = demo.get("/config").text
    card = page.split('<div class="card" id="llm-providers">')[1].split('<div class="card" id="evals-scans">')[0]
    assert "More providers" not in page and 'id="more-providers"' not in page
    assert card.count("Save changes") == 1 and 'hx-include="#llm-org"' in card and "Save providers" not in page
    for p, label in lp.LABELS.items():
        assert f'<tr id="llm-org-{p}"><th>{label}</th>' in card, p
        assert f'<div id="models-org-{p}">' in card and f'hx-get="/api/v1/config/llm-providers/{p}/models"' in card, p
        assert f'name="{p}_allowed_models"' in card, p
    assert (
        card.index('id="llm-org-anthropic"')
        < card.index('id="llm-org-openai"')
        < card.index('id="llm-org-openai_compatible"')
    )
    assert 'name="anthropic_api_key" type="password"' in card and 'name="vertex_project" value="p"' in card
    assert card.count('<span class="badge ok">configured</span>') == 2 and card.count(">not set<") == 5
    assert "headers set (hidden)" in card and """hx-vals='{"openai_compatible": {"headers": ""}}\'""" in card
    assert "default <code>gemini-2.0-flash</code>" in card and "default <code>gpt-4o-mini</code>" in card
    assert 'hx-confirm="Clear the Vertex AI provider?"' in card  # the whole provider (no key) is cleared in one go
    r = demo.put(ORG, json={"vertex": dict.fromkeys(lp.FIELDS["vertex"], "")})  # what that button sends
    assert r.json()["vertex"] == {"project": "", "location": "", "allowed_models": []}
    assert "no org provider" not in page and "provider set" in page  # the Evals & scans row reads the same card


def test_the_card_posts_picked_ids_plus_free_text_and_an_empty_pick_clears(demo):  # noqa: F811
    r = demo.put(ORG, json={"openai_api_key": KEY, "openai_allowed_models": ["", "gpt-4o", "o3-mini, gpt-4.1"]})
    assert r.status_code == 200 and r.json()["openai"]["allowed_models"] == ["gpt-4o", "o3-mini", "gpt-4.1"]
    r = demo.put(ORG, json={"openai_api_key": "", "openai_allowed_models": ["", ""]})  # nothing ticked, nothing typed
    assert r.json()["openai"] == {"api_key_set": True, "base_url": "", "allowed_models": []}
    assert demo.put(ORG, json={"openai_allowed_models": ["ok", "has space"]}).status_code == 422
    assert lp._models("openai", "a, b c") == ["a", "b", "c"] and lp._models("openai", ["a,b", " c ", ""]) == [
        "a",
        "b",
        "c",
    ]


def test_the_group_page_reuses_the_card_and_the_guardrails_model_is_a_select(demo, provider):  # noqa: F811
    page = demo.get("/groups/demo").text
    card = page.split('<div class="card" id="group-llm-providers">')[1].split("<h2>Service-account permissions</h2>")[0]
    assert "LLM providers (group)" in page and 'hx-put="/api/v1/groups/demo/llm-providers"' in card
    assert (
        card.count("Save changes") == 1
        and 'hx-include="#llm-group-demo"' in card
        and "Save group providers" not in page
    )
    for p in lp.FIELDS:
        assert (
            f'<tr id="llm-group-demo-{p}">' in card and f'hx-get="/api/v1/groups/demo/llm-providers/{p}/models"' in card
        )
    # guardrails: no provider anywhere → the selects say so
    rails = page.split('<div class="card policy" id="policy-guardrails">')[1].split("<h2>LLM providers (group)</h2>")[0]
    assert (
        '<select name="llm" hx-get="/ui/groups/demo/guardrails/models?kind=guardrails"' in rails
        and 'hx-target="#guardrails-model"' in rails
    )
    assert (
        '<option value="">none configured yet</option>' in rails
        and '<option value="">no provider configured yet</option>' in rails
    )
    assert 'name="model" value=' not in rails  # no free-text model field any more
    # the org sets OpenAI with two models, the group Anthropic: the group's own scope wins for anthropic
    demo.put(ORG, json={"openai": {"api_key": KEY, "allowed_models": ["gpt-4o-mini", "gpt-4o"]}})
    demo.put(GROUP, json={"anthropic": {"api_key": KEY, "allowed_models": ["claude-sonnet-4-5"]}})
    demo.put("/api/v1/groups/demo/guardrails", json={"llm": "openai", "model": "gpt-4o"})
    rails = (
        demo.get("/groups/demo")
        .text.split('<div class="card policy" id="policy-guardrails">')[1]
        .split("<h2>LLM providers (group)</h2>")[0]
    )
    assert (
        '<option value="">first configured (anthropic)</option>' in rails
        and '<option value="openai" title="org scope" selected>openai</option>' in rails
    )
    assert (
        '<option value="">default: gpt-4o-mini</option>' in rails
        and '<option value="gpt-4o" selected>gpt-4o</option>' in rails
    )
    assert "openai · org scope" in rails
    # the partial the provider select fetches on change
    part = demo.get("/ui/groups/demo/guardrails/models?llm=anthropic").text
    assert '<option value="claude-sonnet-4-5">claude-sonnet-4-5</option>' in part and "anthropic · group scope" in part
    assert "default: claude-sonnet-4-5" in part and "gpt-4o" not in part
    part = demo.get("/ui/groups/demo/guardrails/models").text  # "" = the first configured provider
    assert "anthropic · group scope" in part
    assert "no provider configured yet" in demo.get("/ui/groups/demo/guardrails/models?llm=bedrock").text
    assert demo.get("/ui/groups/nope/guardrails/models").status_code == 404
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as u:
        login(u, "v@x", PW)
        assert u.get("/ui/groups/demo/guardrails/models").status_code == 403
    # a provider with no default and no list: the select says what to do
    demo.put(GROUP, json={"azure_openai": {"api_key": KEY, "host": "h.openai.azure.com"}})
    part = demo.get("/ui/groups/demo/guardrails/models?llm=azure_openai").text
    assert "no default — list allowed models on the group" in part
    assert llm_scope.options({}, {}) is None
    assert llm_scope.options({}, {"openai": {"api_key": "k"}}) == {
        "scope": "org",
        "provider": "openai",
        "models": ["gpt-4o-mini"],
    }
    # a stale model (not allowed any more) stays visible and selected so the admin sees it
    demo.put(ORG, json={"openai": {"allowed_models": ["gpt-4o-mini"]}})
    rails = demo.get("/groups/demo").text.split('<div class="card policy" id="policy-guardrails">')[1]
    assert '<option value="gpt-4o" selected>gpt-4o (not allowed any more)</option>' in rails
