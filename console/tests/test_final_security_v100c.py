"""1.0.0c final security pass (2026-11-17): a group's settings push rolls only that group's workers (live: a group
admin of another group rolled `demo` mid-deploy and left its workers on a stale evals key); provider URLs never aim at
the cloud metadata service."""

import pytest

from ramen_console import deploy as dep
from ramen_console import llm_models
from ramen_console import llm_providers as lp
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - fixtures


class _Cloud:
    def __init__(self):
        self.pushed: list[tuple[str, str]] = []

    async def push_config(self, group, env, zone, config):
        self.pushed.append((group, zone))
        return {"ok": True}


class _Svc:
    store = None

    def __init__(self):
        self.cloud = _Cloud()

    async def environments(self):
        deployed = {"at": "2026-11-16T00:00:00+00:00", "stable": {"z1": "k"}}
        return [
            {"group": "a", "name": "prod", "zones": ["z1"], "last_deploy": deployed},
            {"group": "b", "name": "prod", "zones": ["z1"], "last_deploy": deployed},
        ]

    async def get_group(self, group):
        return {"name": group}


@pytest.fixture
def svc(monkeypatch):
    async def none(*a, **kw):
        return {}

    monkeypatch.setattr(dep.otel, "get_config", none)
    monkeypatch.setattr(dep.roles, "load", none)
    monkeypatch.setattr(dep, "issuer", none)
    monkeypatch.setattr(dep, "zone_config", none)
    return _Svc()


async def test_a_group_push_reaches_only_that_groups_zones(svc):
    assert await dep.push_config(svc, {}, "a") == {"a/z1": "ok"}
    assert svc.cloud.pushed == [("a", "z1")]
    svc.cloud.pushed.clear()
    assert await dep.push_config(svc, {}) == {"a/z1": "ok", "b/z1": "ok"}  # the org's own settings (otel) reach all
    assert svc.cloud.pushed == [("a", "z1"), ("b", "z1")]


def test_the_group_settings_routes_push_their_own_group_only(demo, monkeypatch):  # noqa: F811
    seen = []

    async def push(s, warnings=None, group=None):
        seen.append(group)
        return {}

    monkeypatch.setattr(dep, "push_config", push)
    for path, body in (
        ("evals", {"required": True}),
        ("skillspector", {"required": True}),
        ("guardrails", {"required": True}),
    ):
        r = demo.put(f"/api/v1/groups/demo/{path}", json=body)
        assert r.status_code == 200, (path, r.text[:200])
    assert seen == ["demo", "demo", "demo"]


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/computeMetadata/v1",
        "http://169.254.170.2/v2/credentials",
        "http://[fe80::1]/x",
        "http://[fd00:ec2::254]/latest",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://METADATA/computeMetadata/v1",
        "http://metadata.goog/x",
    ],
)
def test_a_provider_url_at_the_metadata_service_is_refused(url):
    with pytest.raises(ValueError, match="metadata"):
        lp.merge({}, {"openai_compatible": {"base_url": url}})
    with pytest.raises(ValueError, match="metadata"):
        lp.merge({}, {"foundry": {"endpoint": url}})


def test_ordinary_provider_urls_still_save():
    for url in (
        "https://api.example.com/v1",
        "http://10.0.0.5:8000/v1",
        "http://127.0.0.1:9999/v1",
        "http://vllm.llm.svc.cluster.local/v1",
    ):
        assert lp.merge({}, {"openai_compatible": {"base_url": url}})["openai_compatible"]["base_url"] == url


async def test_the_model_listing_refuses_a_name_that_resolves_to_the_metadata_service(monkeypatch):
    async def resolve(host):
        return ["169.254.169.254"]

    monkeypatch.setattr(llm_models, "addresses", resolve)
    cfg = {"openai_compatible": {"base_url": "http://innocent.example.com/v1"}}
    out = await llm_models.list_models(cfg, "openai_compatible", "group:g")
    assert out["source"] == "unavailable" and "metadata" in out["error"]
