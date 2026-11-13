"""CONTRACTS §26.2 (D63): deploy jobs live in the store's `jobs` collection — visible after a restart and from every
replica; a `running` job nobody touched for RAMEN_JOB_STALE_SECS is lost; 50 kept per target; never in a backup."""

import asyncio

from fastapi.testclient import TestClient

from ramen_console.app import create_app
from ramen_console.deploy import Jobs
from ramen_console.util import now
from tests.test_api import app, client, cloud, demo, login, root  # noqa: F401 - fixtures
from tests.test_blocked import wait_job

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"


def run(coro):
    return asyncio.run(coro)


def test_a_deploy_job_is_written_to_the_store_and_survives_a_restart(demo, cloud, monkeypatch):  # noqa: F811
    async def sync(group, repo_url, ref, token):
        return "/tmp/fake"

    monkeypatch.setattr(cloud, "sync_repo", sync)
    job = wait_job(demo, demo.post(DEPLOY, json={"canary": False}).json()["id"])
    store = demo.app.state.store
    doc = run(store.get("jobs", job["id"]))
    assert doc["status"] == job["status"] and doc["finished"] and doc["log"] == job["log"] and doc["kind"] == "deploy"
    assert doc["target"] == "demo/prod" and doc["result"] == job["result"]
    assert "jobs" not in run(demo.app.state.backups.snapshot())  # §26.2: backups do not include jobs
    with TestClient(create_app(store=store, cloud=cloud)) as fresh:  # a restart: the same store, a new process
        login(fresh, "root@ramen.local", "rootpw")
        assert fresh.get(f"/api/v1/jobs/{job['id']}").json()["log"] == job["log"]
        page = fresh.get("/groups/demo").text
        assert f"job {job['id'][:8]}" in page and job["log"][0] in page
        assert fresh.get("/api/v1/jobs/nope").status_code == 404


def test_the_log_is_written_as_it_grows_and_scrubbed(demo):  # noqa: F811
    jobs = demo.app.state.jobs
    job = run(jobs.create("deploy", "demo/prod"))
    assert run(demo.app.state.store.get("jobs", job["id"]))["status"] == "running"  # written at creation
    job["log"].append("syncing https://user:pa55@example.com/repo.git")
    run(jobs.save(job))
    doc = run(demo.app.state.store.get("jobs", job["id"]))
    assert doc["log"] == ["syncing https://***@example.com/repo.git"]
    assert "pa55" not in demo.get(f"/api/v1/jobs/{job['id']}").text


def test_a_running_job_nobody_touched_is_shown_as_lost(demo, monkeypatch):  # noqa: F811
    store = demo.app.state.store
    stale = {"kind": "deploy", "target": "demo/prod", "status": "running", "started": "2020-01-01T00:00:00Z"}
    stale.update(finished=None, result=None, error=None, log=["x"], updated="2020-01-01T00:00:01Z")
    run(store.put("jobs", "old1", stale))
    fresh = {**stale, "updated": now(), "started": now()}
    run(store.put("jobs", "new1", fresh))
    j = demo.get("/api/v1/jobs/old1").json()
    assert j["status"] == "error" and j["error"] == "lost (console restarted)"
    assert demo.get("/api/v1/jobs/new1").json()["status"] == "running"
    monkeypatch.setenv("RAMEN_JOB_STALE_SECS", "0")
    assert demo.get("/api/v1/jobs/new1").json()["status"] == "error"
    page = demo.get("/groups/demo").text
    assert "lost (console restarted)" in page
    assert demo.post(DEPLOY, json={"canary": True}).status_code == 202  # a lost job never blocks the next deploy


def test_the_last_fifty_per_target_are_kept(demo):  # noqa: F811
    jobs: Jobs = demo.app.state.jobs
    for _ in range(55):
        run(jobs.create("deploy", "demo/prod"))
    run(jobs.create("deploy", "demo/dev"))
    store = demo.app.state.store
    assert len(run(store.list("jobs", {"target": "demo/prod"}))) == 50
    assert len(run(store.list("jobs", {"target": "demo/dev"}))) == 1
    recent = run(jobs.recent("demo/"))
    assert len(recent) == 20 and recent[0]["started"] >= recent[-1]["started"]
