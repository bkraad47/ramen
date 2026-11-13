"""CONTRACTS §26.3 (D62), console parts: `telemetry_version`, where it is shown and handed out, and the push of the
worker config + Admin/Reload to every deployed zone when the settings change."""

import json

import pytest

from ramen_console import otel
from tests.test_api import PW, app, client, login, make_user, root  # noqa: F401 - fixtures
from tests.test_blocked import wait_job
from tests.test_oauth_server import consent, exchange, pkce, refresh, register
from tests.test_v100_otel import CFG, DEPLOY, ON, cloud, collector, decide, demo  # noqa: F401 - fixtures


def test_the_version_follows_what_clients_are_told_only():
    cfg = {"enabled": True, "endpoint": "http://j:4318", "client_endpoint": "", "headers": {}, "client_headers": {}}
    v = otel.telemetry_version(cfg)
    assert len(v) == 12 and int(v, 16) >= 0
    assert otel.telemetry_version({**cfg, "headers": {"a": "b"}}) == v  # Ramen's own headers: not the client's
    assert otel.telemetry_version({**cfg, "client_endpoint": "http://x"}) != v
    assert otel.telemetry_version({**cfg, "client_headers": {"x": "y"}}) != v
    assert otel.telemetry_version({**cfg, "enabled": False}) != v
    assert otel.client_info(cfg, "g", "z", required=True)["version"] == v
    assert otel.client_info(cfg, "g", "z", required=True)["required"] is True


def test_the_public_config_and_the_page_show_the_version_and_the_jaeger_ui(demo, monkeypatch):  # noqa: F811
    demo.put(CFG, json=ON)
    cfg = demo.get(CFG).json()
    assert cfg["telemetry_version"] == otel.telemetry_version(
        {"enabled": True, "endpoint": ON["endpoint"].rstrip("/"), "client_endpoint": ON["client_endpoint"]}
        | {"client_headers": ON["client_headers"]}
    )
    page = demo.get("/config").text
    assert f"<code>{cfg['telemetry_version']}</code>" in page and "Jaeger UI" not in page
    monkeypatch.setenv("RAMEN_OTEL_UI_URL", "http://jaeger.example:16686")
    page = demo.get("/config").text
    assert 'href="http://jaeger.example:16686"' in page


def test_workers_and_tokens_get_the_version_and_the_required_flag(demo, cloud, collector):  # noqa: F811
    demo.put(CFG, json=ON)
    version = demo.get(CFG).json()["telemetry_version"]
    assert wait_job(demo, demo.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])["status"] == "ok"
    cfg = cloud.deploys[-1]["config"]
    assert cfg["RAMEN_TELEMETRY_VERSION"] == version
    client_ = json.loads(cfg["RAMEN_OTEL_CLIENT"])
    assert client_["version"] == version and client_["required"] is False
    cid = register(demo)
    verifier, challenge = pkce()
    t = exchange(demo, cid, consent(demo, cid, challenge), verifier).json()
    assert t["telemetry"]["version"] == version and t["telemetry"]["required"] is False
    demo.put("/api/v1/groups/demo", json={"require_telemetry_consent": True})
    verifier, challenge = pkce()
    r = decide(demo, cid, challenge, decision="allow", telemetry_consent="1")
    code = r.headers["location"].split("code=")[1].split("&")[0]
    t = exchange(demo, cid, code, verifier).json()
    assert t["telemetry"]["required"] is True and t["telemetry"]["version"] == version
    assert refresh(demo, cid, t["refresh_token"]).json()["telemetry"]["required"] is True
    assert wait_job(demo, demo.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])["status"] == "ok"
    assert json.loads(cloud.deploys[-1]["config"]["RAMEN_OTEL_CLIENT"])["required"] is True


def test_saving_the_settings_pushes_the_config_to_every_deployed_zone(demo, cloud, collector, workers):  # noqa: F811
    r = demo.put(CFG, json=ON)
    assert r.status_code == 200 and r.json()["pushed"] == {}  # nothing deployed yet
    job = wait_job(demo, demo.post(DEPLOY, json={"canary": False}).json()["id"])
    assert job["status"] == "ok"
    seen = len(workers["hot"].calls)
    r = demo.put(CFG, json={"client_endpoint": "https://otel2.example.com"})
    assert r.json()["pushed"] == {"demo/zone-a": "ok", "demo/zone-b": "ok"}
    version = r.json()["telemetry_version"]
    pushed = {p["zone"]: p for p in cloud.pushes}
    assert set(pushed) == {"zone-a", "zone-b"} and pushed["zone-a"]["env"] == "prod"
    cfg = pushed["zone-a"]["config"]
    assert cfg["RAMEN_TELEMETRY_VERSION"] == version and cfg["RAMEN_SESSION_SECRET"]  # the deploy's cfg, whole
    assert json.loads(cfg["RAMEN_OTEL_CLIENT"])["otlp_endpoint"] == "https://otel2.example.com"
    env_file = job["result"]["zone-a"]["env_file"]
    assert f"RAMEN_TELEMETRY_VERSION={version}" in open(env_file).read()  # the local adapter rewrote it
    assert "Admin/Reload" in workers["hot"].paths()[seen:]  # zone-b's worker was told to re-read its config
    a = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "telemetry.push"]
    assert a and a[0]["ok"] and {"demo/zone-a:ok", "demo/zone-b:ok"} <= set(a[0]["tags"])


def test_a_zone_that_cannot_be_reached_is_reported_not_raised(demo, cloud, collector, monkeypatch):  # noqa: F811
    demo.put(CFG, json=ON)
    assert wait_job(demo, demo.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])["status"] == "ok"

    async def boom(group, env, zone, config):
        raise RuntimeError("worker unreachable")

    monkeypatch.setattr(cloud, "push_config", boom)
    r = demo.put(CFG, json={"client_endpoint": "https://otel3.example.com"})
    assert r.status_code == 200 and r.json()["pushed"] == {"demo/zone-a": "RuntimeError: worker unreachable"}
    a = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "telemetry.push"]
    assert a and a[0]["ok"] is False


@pytest.mark.parametrize("by", ["group_admin", "viewer"])
def test_only_a_super_admin_pushes(app, demo, by):  # noqa: F811
    from fastapi.testclient import TestClient

    make_user(demo, "u@x", by, ["demo"])
    with TestClient(app) as u:
        login(u, "u@x", PW)
        assert u.put(CFG, json=ON).status_code == 403
