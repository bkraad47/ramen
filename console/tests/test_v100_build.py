"""CONTRACTS §24.2: the group's worker image is built on deploy — the setting, the tag, the Dockerfile, the `images`
record, reuse, the precedence over the base image and under a manual pin, and a failed build stopping the deploy."""

import hashlib

import pytest
from fastapi.testclient import TestClient

from ramen_console import imagebuild
from ramen_console.grpcclient import Client
from tests.stub_cloud import BASE_IMAGE, BuildFailed, StubCloud
from tests.test_api import PW, app, client, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_blocked import wait_job

DEPLOY = "/api/v1/groups/demo/environments/prod/deploy"
REQ = b"requests==2.32.3\nhttpx>=0.27\n"


@pytest.fixture
def cloud(tmp_path, workers, monkeypatch):
    c = StubCloud(
        tmp_path / "buckets",
        tmp_path / "logs",
        {"demo/zone-a": [workers["cold"].target], "demo/zone-b": [workers["hot"].target]},
        workers["cold"].target,
        "adm",
        Client(deadline=2),
    )

    async def sync(group, repo_url, ref, token):
        return "/tmp/fake"

    monkeypatch.setattr(c, "sync_repo", sync)
    return c


def requirements(tmp_path, data=REQ):
    p = tmp_path / "buckets" / "demo" / "mcp" / "requirements.txt"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)


def deploy(c):
    return wait_job(c, c.post(DEPLOY, json={"zone": "zone-a"}).json()["id"])


def builds(cloud):
    return [c for c in cloud.calls if c[0] == "build"]


def tag_of(data=REQ, base=BASE_IMAGE):
    return "b-demo-" + hashlib.sha256(base.encode() + b"\n" + data).hexdigest()[:16]


def test_the_tag_and_the_dockerfile_are_exactly_the_contracts():
    assert imagebuild.tag_for("demo", BASE_IMAGE, REQ) == tag_of()
    assert imagebuild.dockerfile(BASE_IMAGE, REQ) == (
        f"FROM {BASE_IMAGE}\n"
        "COPY requirements.txt /opt/ramen/requirements.txt\n"
        "RUN /opt/venv/bin/pip install --no-cache-dir -r /opt/ramen/requirements.txt\n"
        f"ENV RAMEN_DEPS_BAKED={hashlib.sha256(REQ).hexdigest()}\n"
    )


def test_build_image_needs_a_builder_and_a_group_admin(demo, cloud):
    cloud.builder = False
    r = demo.put("/api/v1/groups/demo", json={"build_image": True})
    assert (
        r.status_code == 422 and "ramen-builder" in r.json()["detail"] and "RAMEN_BUILD_REGISTRY" in r.json()["detail"]
    )
    assert demo.put("/api/v1/groups/demo", json={"build_image": False}).status_code == 200  # off is always allowed
    cloud.builder = True
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "v@x", "viewer", ["demo"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.put("/api/v1/groups/demo", json={"build_image": True}).json()["build_image"] is True
    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put("/api/v1/groups/demo", json={"build_image": False}).status_code == 403
    a = [x for x in demo.get("/api/v1/audit?limit=100").json() if x["action"] == "group.update" and x["ok"]]
    assert any("build_image:True" in x["tags"] for x in a)


async def test_the_default_is_on_only_on_a_cloud_with_a_builder(demo, cloud, monkeypatch):
    s = demo.app.state.services
    g = await s.get_group("demo")
    assert await s.builds_images(g) is False  # RAMEN_CLOUD=local
    monkeypatch.setenv("RAMEN_CLOUD", "gcp")
    assert await s.builds_images(g) is True
    cloud.builder = False
    assert await s.builds_images(g) is False
    assert await s.builds_images({**g, "build_image": True}) is True  # what was set wins over the default
    monkeypatch.delattr(type(cloud), "builder_available")
    assert await imagebuild.builder_available(object()) is False

    class Async:
        async def builder_available(self):
            return True

    assert await imagebuild.builder_available(Async()) is True


def test_a_deploy_builds_the_image_once_records_it_and_runs_it(demo, cloud, tmp_path):
    requirements(tmp_path)
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    job = deploy(demo)
    assert job["status"] == "ok", job
    (b,) = builds(cloud)
    assert b[1:3] == ("demo", tag_of()) and b[3] == imagebuild.dockerfile(BASE_IMAGE, REQ)
    assert b[4] == {"requirements.txt": REQ.decode()}
    ref = f"reg.example/ramen/worker:{tag_of()}"
    assert cloud.deploys[-1]["spec"]["image"] == ref
    assert cloud.calls.index(b) == 0 and any(f"image: built {ref}" in ln for ln in job["log"])
    (rec,) = demo.get("/api/v1/groups/demo/images").json()
    assert rec["tag"] == tag_of() and rec["ref"] == ref and rec["source"] == "build" and rec["status"] == "ok"
    assert rec["deps"] == hashlib.sha256(REQ).hexdigest() and rec["base"] == BASE_IMAGE
    assert rec["in_use"] is True and rec["current"] is False and rec["created_by"] == "deploy"
    assert demo.get("/api/v1/groups/demo").json()["built_image"]["ref"] == ref
    job = deploy(demo)  # the record says the image exists: nothing is built
    assert job["status"] == "ok" and len(builds(cloud)) == 1 and cloud.deploys[-1]["spec"]["image"] == ref
    assert any("already built" in ln for ln in job["log"])
    requirements(tmp_path, REQ + b"rich\n")  # other requirements, another image
    assert deploy(demo)["status"] == "ok" and builds(cloud)[-1][2] == tag_of(REQ + b"rich\n")
    assert len(demo.get("/api/v1/groups/demo/images").json()) == 2
    html = demo.get("/groups/demo").text
    assert "Build on deploy" in html and ">built</span>" in html and "(built on deploy)" in html


def test_nothing_is_built_when_off_or_without_requirements(demo, cloud, tmp_path):
    requirements(tmp_path)
    assert deploy(demo)["status"] == "ok" and builds(cloud) == []  # local: the default is off
    assert cloud.deploys[-1]["spec"]["image"] is None
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    requirements(tmp_path, b"  \n")
    assert deploy(demo)["status"] == "ok" and builds(cloud) == [] and cloud.deploys[-1]["spec"]["image"] is None
    requirements(tmp_path)
    assert deploy(demo)["status"] == "ok" and len(builds(cloud)) == 1
    demo.put("/api/v1/groups/demo", json={"build_image": False})  # turned off again: back to the base image
    assert deploy(demo)["status"] == "ok" and cloud.deploys[-1]["spec"]["image"] is None
    assert "built_image" not in demo.get("/api/v1/groups/demo").json()


def test_a_pinned_image_wins_over_a_built_one(demo, cloud, tmp_path):
    requirements(tmp_path)
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    assert deploy(demo)["status"] == "ok"
    r = demo.post("/api/v1/groups/demo/images", json={"tag": "reg.example/custom/worker:demo-7"})
    assert r.status_code == 201 and demo.get("/api/v1/groups/demo").json()["image"]["source"] == "manual"
    requirements(tmp_path, REQ + b"rich\n")
    job = deploy(demo)
    assert job["status"] == "ok" and len(builds(cloud)) == 1  # pinned: no new build
    assert cloud.deploys[-1]["spec"]["image"] == "reg.example/custom/worker:demo-7"
    assert any("is pinned for this group" in ln for ln in job["log"])
    demo.delete("/api/v1/groups/demo/images/current")
    assert deploy(demo)["status"] == "ok" and cloud.deploys[-1]["spec"]["image"].endswith(tag_of(REQ + b"rich\n"))


def test_a_failed_build_stops_the_deploy_before_the_canary_with_the_log_tail(demo, cloud, tmp_path):
    requirements(tmp_path)
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    cloud.build_error = BuildFailed("pip exited 1", log="\n".join(f"line {i}" for i in range(1, 51)))
    job = deploy(demo)
    assert job["status"] == "error" and "image build" in job["error"] and "pip exited 1" in job["error"]
    assert cloud.deploys == []  # no canary, the stable track untouched
    tail = [ln.split("image: | ")[1] for ln in job["log"] if "image: | " in ln]
    assert tail == [f"line {i}" for i in range(11, 51)]
    (rec,) = demo.get("/api/v1/groups/demo/images").json()
    assert rec["status"] == "failed" and rec["source"] == "build" and not rec["ref"] and not rec["in_use"]
    assert demo.put("/api/v1/groups/demo/images/current", json={"id": rec["id"]}).status_code == 404
    assert ">build failed</span>" in demo.get("/groups/demo").text
    cloud.build_error = None
    cloud.build_out = {"ok": False, "error": "timed out after 900 s", "log": "still pulling"}
    job = deploy(demo)
    assert job["status"] == "error" and "timed out after 900 s" in job["error"]
    cloud.build_out = None
    assert deploy(demo)["status"] == "ok" and len(builds(cloud)) == 3  # a failed record never counts as built


def test_requirements_that_cannot_be_built_are_refused(demo, cloud, tmp_path):
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    requirements(tmp_path, b"\xff\xfe bad")
    job = deploy(demo)
    assert job["status"] == "error" and "not UTF-8" in job["error"]
    requirements(tmp_path)
    cloud.image = ""
    job = deploy(demo)
    assert job["status"] == "error" and "no base worker image" in job["error"] and builds(cloud) == []


def test_the_toggle_is_shown_and_disabled_without_a_builder(demo, cloud):
    html = demo.get("/groups/demo").text
    assert "Build on deploy: off → on" in html and "This deployment has no image builder" not in html
    cloud.builder = False
    html = demo.get("/groups/demo").text
    assert "This deployment has no image builder, so it cannot be turned on." in html


def test_a_requirements_file_of_comments_has_nothing_to_bake():
    from ramen_console import imagebuild

    assert imagebuild.packages(b"# nothing yet\n\n   # numpy later\n") == []
    assert imagebuild.packages(b"# pinned\nsix==1.17.0\n\nrequests>=2 # http\n") == [
        "six==1.17.0",
        "requests>=2 # http",
    ]
