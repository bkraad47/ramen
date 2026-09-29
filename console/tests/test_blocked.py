"""CONTRACTS §9 tool blocking: env.blocked → PUT .../blocked → deploy writes RAMEN_BLOCKED → UI toggles."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures


@pytest.fixture(autouse=True)
def fake_sync(demo, monkeypatch):
    async def sync(group, repo_url, ref, token):
        return "/tmp/fake"

    monkeypatch.setattr(demo.app.state.cloud, "sync_repo", sync)


def wait_job(client, jid):
    for _ in range(100):
        j = client.get(f"/api/v1/jobs/{jid}").json()
        if j["status"] != "running":
            return j
        time.sleep(0.05)
    raise AssertionError("job still running")


def test_blocked_list_and_deploy_config(demo, tmp_path):
    envs = demo.get("/api/v1/environments?group=demo").json()
    assert envs[0]["blocked"] == []
    r = demo.put(
        "/api/v1/groups/demo/environments/prod/blocked",
        json={"blocked": ["secret_tool", " ramen://demo/readme ", "secret_tool"]},
    )
    assert r.status_code == 200 and r.json()["blocked"] == ["secret_tool", "ramen://demo/readme"]
    assert demo.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": ["a,b"]}).status_code == 422
    assert demo.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": "x, y"}).json()["blocked"] == [
        "x",
        "y",
    ]
    assert demo.put("/api/v1/groups/demo/environments/prod", json={"blocked": ["via-update"]}).json()["blocked"] == [
        "via-update"
    ]
    assert demo.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": ["calc"]}).json()["blocked"] == [
        "calc"
    ]
    assert demo.put("/api/v1/groups/demo/environments/nope/blocked", json={"blocked": []}).status_code == 404
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok", job
    env_file = Path(job["result"]["zone-a"]["env_file"]).read_text()
    assert "RAMEN_BLOCKED=calc\n" in env_file
    demo.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": []})
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert "RAMEN_BLOCKED=\n" in Path(job["result"]["zone-a"]["env_file"]).read_text()
    audit = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "environment.blocked"]
    assert audit and "blocked:calc" in [t for a in audit for t in a["tags"]]


def test_group_page_block_toggles(demo):
    make_user(demo, "v@x", "viewer", ["demo"])
    job = wait_job(demo, demo.post("/api/v1/groups/demo/environments/prod/deploy", json={"canary": False}).json()["id"])
    assert job["status"] == "ok"
    page = demo.get("/groups/demo").text
    assert "<code>calc</code>" in page and ">Disable<" in page
    assert "/environments/prod/zones/zone-a/blocked" in page  # U5: the toggle is per zone
    demo.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": ["calc"]})
    page = demo.get("/groups/demo").text
    assert ">Disabled everywhere<" in page and ">Unblock<" in page
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        page = v.get("/groups/demo").text
        assert ">Disable<" not in page and ">Unblock<" not in page and "calc" in page
        assert v.put("/api/v1/groups/demo/environments/prod/blocked", json={"blocked": []}).status_code == 403
