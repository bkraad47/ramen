"""§38.2 GCP residual closed (sec-gaps, 2026-11-17): the console's Artifact Registry rights are conditioned on the
resource tag `ramen-repo=group`. After creating `ramen-groups-<group>` the console binds that tag (Resource Manager
TagBindings, regional endpoint), waits out IAM-condition propagation (403s) and only then sets the writer binding."""

import pytest

from ramen_console.cloud import gcp_api
from ramen_console.cloud.gcp import GcpCloud
from tests.fakes_gcp import FakeApiError, FakeClients

REPO = "projects/p1/locations/us-central1/repositories/ramen-groups-demo"
FULL = "//artifactregistry.googleapis.com/" + REPO
TAG = "p1/ramen-repo/group"


def reg(fk, **kw):
    kw.setdefault("tag_value", TAG)
    return gcp_api.Registry(
        fk.artifactregistry, "p1", "us-central1", attempts=3, delay=0, tags=fk.tag_bindings("us-central1"), **kw
    )


def conditioned(fk):
    """IAM as terraform sets it: the console's repository rights only on repositories tagged `group` (list/create
    stay unconditioned)."""
    fk.ar_state["conditioned"] = TAG


def test_ensure_creates_tags_then_binds_and_waits_out_propagation():
    fk = FakeClients()
    conditioned(fk)
    fk.ar_state["propagation"] = 2  # the first two conditioned calls after the tag still answer 403
    r = reg(fk)
    assert r.ensure("ramen-groups-demo", {"ramen-group": "demo"}, "d") is True
    assert fk.tag_state["bindings"] == {FULL: TAG}
    assert fk.tag_state["locations"] == ["us-central1"]  # regional endpoint (a repository is a regional resource)
    assert r.bind("ramen-groups-demo", "roles/artifactregistry.writer", "principal://x") is True
    assert fk.ar_state["policies"][REPO]["bindings"][0]["members"] == ["principal://x"]
    methods = [c[1] for c in fk.artifactregistry.calls]
    assert methods.index("create") < methods.index("setIamPolicy")
    # idempotent: the repository exists and carries the tag (409 from TagBindings) — nothing new
    assert r.ensure("ramen-groups-demo", {}, "d") is False and r.bind("ramen-groups-demo", "roles/x", "a") is True


def test_a_legacy_untagged_repository_is_tagged_on_the_next_build_and_on_delete():
    fk = FakeClients()
    r = reg(fk)
    r.ensure("ramen-groups-old", {}, "d")
    fk.tag_state["bindings"].clear()  # made before terraform had the tag key
    conditioned(fk)
    with pytest.raises(FakeApiError):  # untagged: the conditioned role does not reach it
        r.get("ramen-groups-old")
    assert r.ensure("ramen-groups-old", {}, "d") is False
    assert r.get("ramen-groups-old") is not None
    fk.tag_state["bindings"].clear()
    assert r.delete("ramen-groups-old") is True and not fk.ar_state["repos"]
    assert r.delete("ramen-groups-old") is False  # list says it is gone: no tag call, no 403 wait
    assert len([c for c in fk.tag_calls() if c[2]["body"]["parent"].endswith("/ramen-groups-old")]) == 3


def test_the_platform_repository_cannot_be_moved_to_group():
    # one value per key: a repository tagged `platform` refuses a second value of ramen-repo, so the console's
    # createTagBinding cannot bring the platform repository under its conditioned role
    fk = FakeClients()
    r = reg(fk)
    r.ensure("ramen", {}, "d")
    fk.tag_state["bindings"][FULL.replace("ramen-groups-demo", "ramen")] = "p1/ramen-repo/platform"  # terraform
    conditioned(fk)
    with pytest.raises(FakeApiError) as e:
        r.tag("ramen")
    assert e.value.status == 400
    with pytest.raises(gcp_api.ApiError):  # the conditioned role never matches `platform`
        r.bind("ramen", "roles/artifactregistry.writer", "serviceAccount:console")


def test_tag_off_unavailable_and_errors():
    fk = FakeClients()
    assert reg(fk, tag_value=None).tag("x") == "off"
    assert gcp_api.Registry(fk.artifactregistry, "p1", "us-central1").tag("x") == "off"  # no tags client
    r = reg(fk)
    fk.tag_state["fail"] = [403]  # pre-upgrade terraform: no Tag User → the old unconditioned rights still apply
    assert r.tag("x") == "unavailable"
    fk.tag_state["fail"] = [404, 404, 404]  # tag value missing (or the new repository not visible yet, retried)
    assert r.tag("x", retry=True) == "unavailable"
    fk.tag_state["fail"] = [404]
    assert r.tag("x", retry=True) == "bound"
    fk.tag_state["fail"] = [500]
    with pytest.raises(FakeApiError):
        r.tag("y")
    assert reg(fk, tag_value="tagValues/42").tag("z") == "bound"
    assert fk.tag_calls()[-1][2]["body"] == {
        "parent": "//artifactregistry.googleapis.com/projects/p1/locations/us-central1/repositories/z",
        "tagValue": "tagValues/42",
    }


def test_ensure_gives_up_with_a_message_naming_the_tag():
    fk = FakeClients()
    conditioned(fk)
    fk.ar_state["propagation"] = 99
    with pytest.raises(gcp_api.ApiError) as e:
        reg(fk).ensure("ramen-groups-demo", {}, "d")
    assert "ramen-repo=group" in str(e.value) and "§38.2" in str(e.value)


def test_list_refused_falls_back_to_get():
    # a console upgraded before terraform: no repositories.list yet → the old get-based check
    fk = FakeClients()
    r = reg(fk)
    fk.ar_state["fail"] = {"list": 403}
    assert r.ensure("a", {}, "d") is True
    fk.ar_state["fail"] = {"list": 403}
    assert r.ensure("a", {}, "d") is False
    fk.ar_state["fail"] = {"list": 403}
    assert r.delete("a") is True
    fk.ar_state["fail"] = {"list": 500}
    with pytest.raises(FakeApiError):
        r.exists("a")


def test_list_pages():
    fk = FakeClients()
    r = reg(fk)
    for i in range(3):
        r.ensure(f"r{i}", {}, "d")
    fk.ar_state["page_size"] = 1
    assert r.exists("r2") is True and r.exists("nope") is False


async def test_gcp_build_binds_the_group_tag_from_env(monkeypatch):
    monkeypatch.setenv("RAMEN_BUILDER", "1")
    monkeypatch.setenv("RAMEN_BUILD_REGISTRY", "us-central1-docker.pkg.dev/p1/ramen")
    monkeypatch.delenv("RAMEN_BUILD_IDENTITIES", raising=False)
    monkeypatch.delenv("RAMEN_AR_GROUP_TAG", raising=False)
    fk = FakeClients()
    fk.iam_state["number"] = "4242"
    conditioned(fk)
    fk.ar_state["propagation"] = 1
    cloud = GcpCloud(project="p1", bucket="b", image="img:1", clients=fk, wait_secs=1, poll=0)
    await cloud.build_image("demo", "b-demo-1", "FROM base:1\n", {"requirements.txt": "six\n"})
    assert fk.tag_state["bindings"] == {FULL: TAG}  # default: <project>/ramen-repo/group
    assert fk.ar_state["policies"][REPO]["bindings"][0]["role"] == "roles/artifactregistry.writer"
    removed = await cloud.detach_group("demo")
    assert removed["repositories"] == ["ramen-groups-demo"]
    monkeypatch.setenv("RAMEN_AR_GROUP_TAG", "none")
    assert cloud._ar_group_tag("p1") is None
    monkeypatch.setenv("RAMEN_AR_GROUP_TAG", "tagValues/7")
    assert cloud._ar_group_tag("p1") == "tagValues/7"


def test_gcp_clients_tag_bindings_use_the_regional_endpoint(monkeypatch):
    from ramen_console.cloud import gcp_clients

    seen = []

    def build(name, version, **kw):
        seen.append((name, version, kw["client_options"]["api_endpoint"]))
        return object()

    import googleapiclient.discovery as disc

    monkeypatch.setattr(disc, "build", build)
    monkeypatch.setattr(gcp_clients, "_creds", lambda: None)
    c = gcp_clients.GcpClients("p1")
    a = c.tag_bindings("us-central1")
    assert c.tag_bindings("us-central1") is a  # cached per location
    assert seen == [("cloudresourcemanager", "v3", "https://us-central1-cloudresourcemanager.googleapis.com/")]
