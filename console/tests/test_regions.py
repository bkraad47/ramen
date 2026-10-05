"""R1 / C7 (0.7.0): a super admin blocks cloud regions; zones and deploys in them are refused."""

import asyncio
import json
import logging
from pathlib import Path

import pytest

from ramen_console import regions
from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import fake_sync, wait_job  # noqa: F401 - pytest fixtures

URL = "/api/v1/config/regions"


def test_regions_file_lists_both_providers_with_a_dated_note():
    data = json.loads(Path(regions.__file__).with_name("regions.json").read_text())
    assert "2026" in data["note"]
    for provider in ("gcp", "aws"):
        assert len(data[provider]) >= 30 and all(v for v in data[provider].values())
    assert data["gcp"]["us-central1"] == ["a", "b", "c", "f"] and data["aws"]["us-east-1"][:3] == ["a", "b", "c"]
    assert regions.REGIONS["gcp"] == data["gcp"]


def test_zone_names_follow_each_providers_convention():
    assert "us-central1-a" in regions.zones("gcp")["us-central1"]
    assert "eu-west-1a" in regions.zones("aws")["eu-west-1"]
    assert regions.zones("local") == {}
    assert regions.zones("gcp", blocked=["us-central1"]).get("us-central1") is None
    assert "us-west1" in regions.zones("gcp", blocked=["us-central1"])


@pytest.mark.parametrize(
    "provider, value, blocked, hit",
    [
        ("gcp", "us-west1-a", ["us-west1"], "us-west1"),
        ("gcp", "us-west1", ["us-west1"], "us-west1"),
        ("gcp", "us-west12-a", ["us-west1"], None),
        ("aws", "eu-west-1a", ["eu-west-1"], "eu-west-1"),
        ("aws", "eu-west-1", ["eu-west-1"], "eu-west-1"),
        ("aws", "eu-west-10a", ["eu-west-1"], None),
        ("aws", "us-east-1", ["eu-west-1"], None),
        ("local", "local", ["local"], None),
        ("gcp", "", ["us-west1"], None),
    ],
)
def test_block_matches_by_region_prefix(provider, value, blocked, hit):
    assert regions.blocked_region(provider, value, {provider: blocked}) == hit


def test_message_names_the_region():
    assert regions.message("us-west1") == "region us-west1 is blocked by a super admin"


def test_config_api_round_trips_and_validates(demo):
    r = demo.get(URL)
    assert r.status_code == 200
    assert r.json()["blocked"] == {"gcp": [], "aws": []} and "us-central1" in r.json()["regions"]["gcp"]
    r = demo.put(URL, json={"gcp": ["us-west1", "", "us-west1"], "aws": ["eu-west-1"]})
    assert r.status_code == 200, r.text
    assert r.json()["blocked"] == {"gcp": ["us-west1"], "aws": ["eu-west-1"]}
    assert demo.put(URL, json={"blocked": {"gcp": ["us-east1"]}}).json()["blocked"] == {"gcp": ["us-east1"], "aws": []}
    r = demo.put(URL, json={"gcp": ["mars-1"]})
    assert r.status_code == 422 and "mars-1" in r.json()["detail"]
    assert demo.put(URL, json={"gcp": "us-west1,us-west2"}).json()["blocked"]["gcp"] == ["us-west1", "us-west2"]
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "config.regions"]
    assert any("gcp:us-west1,us-west2" in a["tags"] for a in rows)


def test_only_a_super_admin_reads_or_writes_the_policy(demo):
    from fastapi.testclient import TestClient

    make_user(demo, "ga@x", "group_admin", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get(URL).status_code == 403
        assert ga.put(URL, json={"gcp": ["us-west1"]}).status_code == 403


def test_a_zone_in_a_blocked_region_is_refused_with_403(demo):
    demo.put(URL, json={"gcp": ["us-west1"], "aws": ["eu-west-1"]})
    r = demo.post("/api/v1/zones", json={"name": "w", "provider": "gcp", "region": "us-west1-a"})
    assert r.status_code == 403 and r.json()["detail"] == "Region us-west1 is blocked by a super admin"
    assert demo.post("/api/v1/zones", json={"name": "e", "provider": "aws", "region": "eu-west-1a"}).status_code == 403
    assert (
        demo.post("/api/v1/zones", json={"name": "c", "provider": "gcp", "region": "us-central1-a"}).status_code == 201
    )
    assert demo.post("/api/v1/zones", json={"name": "l", "provider": "local", "region": "us-west1"}).status_code == 201
    assert demo.get("/api/v1/zones/w").status_code == 404


def test_a_deploy_refuses_a_zone_whose_region_was_blocked_later(demo, caplog):
    assert (
        demo.post("/api/v1/zones", json={"name": "west", "provider": "gcp", "region": "us-west1-b"}).status_code == 201
    )
    demo.put("/api/v1/groups/demo/environments/prod", json={"zones": ["zone-a", "west"]})
    demo.put(URL, json={"gcp": ["us-west1"]})
    with caplog.at_level(logging.WARNING, logger="ramen.deploy"):
        job = wait_job(
            demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"]
        )
    assert job["status"] == "error", job
    msg = "region us-west1 is blocked by a super admin"
    assert job["result"]["west"] == {"ok": False, "error": msg, "workers": []}
    assert job["result"]["zone-a"]["ok"] is True  # the other zone still deployed
    assert msg in job["error"] and any(msg in line for line in job["log"])
    assert any(
        r.levelno == logging.WARNING and msg in r.getMessage() and "demo/prod" in r.getMessage() for r in caplog.records
    )


def test_config_page_has_a_blocked_regions_card_with_one_dropdown_per_provider(demo):
    demo.put(URL, json={"gcp": ["us-west1"]})
    page = demo.get("/config").text
    assert "Blocked regions" in page
    assert '<details class="multi" id="blocked-gcp">' in page and '<details class="multi" id="blocked-aws">' in page
    assert 'name="gcp" value="us-west1" checked' in page and 'name="aws" value="eu-west-1">' in page
    assert 'hx-put="/api/v1/config/regions"' in page


def test_zones_page_offers_the_providers_zones_minus_blocked_regions(demo):
    demo.put(URL, json={"gcp": ["us-west1"], "aws": ["eu-west-1"]})
    page = demo.get("/zones").text
    assert '<optgroup label="us-central1">' in page and "<option>us-central1-a</option>" in page
    assert "us-west1-a" not in page and '<optgroup label="us-west1">' not in page
    assert "<option>eu-west-2a</option>" in page and "eu-west-1a" not in page
    assert 'name="region" placeholder="local"' in page  # local stays free text
    assert page.count('hx-post="/api/v1/zones"') == 3  # one JS-free form per provider
    assert "Blocked by a super admin: gcp us-west1 · aws eu-west-1" in page
    make_user(demo, "v@x", "viewer", ["demo"])
    from fastapi.testclient import TestClient

    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert 'hx-post="/api/v1/zones"' not in v.get("/zones").text


def test_services_check_region_reads_the_store(demo):
    svc = demo.app.state.services

    async def run():
        await svc.store.put("config", "regions", {"blocked": {"gcp": ["us-west1"], "aws": []}})
        assert await svc.blocked_regions() == {"gcp": ["us-west1"], "aws": []}
        assert await svc.region_block("gcp", "us-west1-a") == "us-west1"
        assert await svc.region_block("aws", "us-west1-a") is None
        await svc.store.put("config", "regions", {"blocked": {"gcp": ["x"]}})  # a partial doc still answers both
        assert await svc.blocked_regions() == {"gcp": ["x"], "aws": []}

    asyncio.run(run())
