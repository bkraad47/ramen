"""CONTRACTS §13.3 (V5.3 / F9.3): worker images recorded, pinned per group, recalled, rendered into manifests."""

from fastapi.testclient import TestClient

from tests.test_api import PW, app, client, cloud, demo, login, make_user, root  # noqa: F401 - pytest fixtures

AR = "us-central1-docker.pkg.dev/p/ramen/worker"
DIGEST = "sha256:" + "ab" * 32


def record(root, tag, **body):
    return root.post("/api/v1/groups/demo/images", json={"tag": tag, **body})


def test_record_pin_and_recall(demo):
    assert demo.get("/api/v1/groups/demo/images").json() == []

    r = record(demo, f"{AR}:demo-1", note="first build")
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["ref"] == f"{AR}:demo-1" and first["current"] is True

    r = record(demo, f"{AR}:demo-2", digest=DIGEST)
    assert r.status_code == 201, r.text
    second = r.json()
    assert second["ref"] == f"{AR}:demo-2@{DIGEST}"  # a recorded digest pins the reference

    history = demo.get("/api/v1/groups/demo/images").json()
    assert [h["id"] for h in history] == [second["id"], first["id"]]  # newest first
    assert [h["current"] for h in history] == [True, False]
    assert demo.get("/api/v1/groups/demo").json()["image"]["ref"] == second["ref"]

    r = demo.put("/api/v1/groups/demo/images/current", json={"id": first["id"]})
    assert r.status_code == 200, r.text
    assert demo.get("/api/v1/groups/demo").json()["image"]["ref"] == first["ref"]
    assert [h["current"] for h in demo.get("/api/v1/groups/demo/images").json()] == [False, True]

    assert demo.delete("/api/v1/groups/demo/images/current").status_code == 200
    assert demo.get("/api/v1/groups/demo").json().get("image") is None
    assert all(h["current"] is False for h in demo.get("/api/v1/groups/demo/images").json())


async def test_the_pinned_image_reaches_the_zone_spec(demo):
    """The spec is what every adapter renders into the worker manifests, so the pin has to arrive there."""
    svc = demo.app.state.services
    assert (await svc.zone_spec("demo", "zone-a"))["image"] is None
    record(demo, f"{AR}:demo-7")
    assert (await svc.zone_spec("demo", "zone-a"))["image"] == f"{AR}:demo-7"
    assert (await svc.zone_spec("other", "zone-a"))["image"] is None  # the pin is per group


def test_bad_references_are_refused(demo):
    for tag in ("", "Demo:Latest", "no-tag-at-all", "has space:1", f"{AR}:"):
        assert record(demo, tag).status_code == 422, tag
    assert record(demo, f"{AR}:demo-1", digest="sha256:nope").status_code == 422
    assert record(demo, AR, digest=DIGEST).status_code == 201  # digest alone is a complete reference


def test_recall_of_an_unknown_or_foreign_record_is_a_404(demo):
    assert demo.put("/api/v1/groups/demo/images/current", json={"id": "nope"}).status_code == 404
    other = demo.post("/api/v1/groups/other/images", json={"tag": f"{AR}:other-1"})
    assert other.status_code == 201
    assert demo.put("/api/v1/groups/demo/images/current", json={"id": other.json()["id"]}).status_code == 404
    assert demo.get("/api/v1/groups/unknown/images").status_code == 404


def test_only_super_admins_record_and_recall_but_group_viewers_can_look(demo):
    r = record(demo, f"{AR}:demo-1")
    make_user(demo, "ga@x", "group_admin", ["demo"])
    make_user(demo, "out@x", "viewer", ["other"])
    with TestClient(demo.app) as ga:
        login(ga, "ga@x", PW)
        assert ga.get("/api/v1/groups/demo/images").status_code == 200
        assert ga.post("/api/v1/groups/demo/images", json={"tag": f"{AR}:x-1"}).status_code == 403
        assert ga.put("/api/v1/groups/demo/images/current", json={"id": r.json()["id"]}).status_code == 403
        assert ga.delete("/api/v1/groups/demo/images/current").status_code == 403
    with TestClient(demo.app) as out:
        login(out, "out@x", PW)
        assert out.get("/api/v1/groups/demo/images").status_code == 403


def test_the_group_page_shows_the_image_and_its_history(demo):
    record(demo, f"{AR}:demo-1", note="first build")
    record(demo, f"{AR}:demo-2")
    page = demo.get("/groups/demo").text
    assert "Worker image" in page and "demo-1" in page and "demo-2" in page
    assert "Recall" in page and "first build" in page


def test_recording_an_image_is_audited(demo):
    record(demo, f"{AR}:demo-1")
    rows = [a for a in demo.get("/api/v1/audit").json() if a["action"] == "image.record"]
    assert rows and rows[0]["ok"] is True and "group:demo" in rows[0]["tags"]
