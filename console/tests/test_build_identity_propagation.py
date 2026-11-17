"""1.0.0 (Azure final pass): a group's first deploy failed its image build with AADSTS70025 — the build identity and its
federated credential were created moments before and Entra had not propagated them. A build that fails with an identity-
propagation error is retried for a bounded time; any other failure fails at once."""

import pytest

from ramen_console.cloud import gcp as gcp_mod
from ramen_console.cloud.base import BuildError
from ramen_console.cloud.gcp import GcpCloud
from tests.fakes_gcp import FakeClients

AZURE = "AADSTS70025: The client 'ramen-build-demo' has no configured federated identity credentials."


def cloud(monkeypatch, outcomes):
    c = GcpCloud(project="p1", region="us-central1", bucket="b", image="i", clients=FakeClients(), wait_secs=1, poll=0)
    calls = []

    def fake_build(group, tag, dockerfile, files):
        calls.append(tag)
        out = outcomes.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    monkeypatch.setattr(c, "_build", fake_build)
    monkeypatch.setattr(c, "_use", lambda *_a, **_k: None)
    monkeypatch.setattr(gcp_mod.time, "sleep", lambda _s: None)
    return c, calls


@pytest.mark.parametrize(
    "log",
    [
        AZURE,
        "AADSTS700213: No matching federated identity record found for presented assertion subject",
        'Permission "artifactregistry.repositories.uploadArtifacts" denied on resource',
        "is not authorized to perform: ecr:InitiateLayerUpload because no identity-based policy allows",
    ],
)
async def test_an_identity_propagation_failure_is_retried_until_the_build_succeeds(monkeypatch, log):
    ok = {"image": "r/worker@sha256:x", "tag_ref": "r/worker:t", "digest": "sha256:x", "log": ""}
    c, calls = cloud(monkeypatch, [BuildError("failed", log), BuildError("failed", log), ok])
    assert await c.build_image("demo", "t", "FROM x", {}) == ok and len(calls) == 3


async def test_any_other_failure_fails_at_once(monkeypatch):
    c, calls = cloud(monkeypatch, [BuildError("failed", "pip: no matching distribution for nope==9")])
    with pytest.raises(BuildError):
        await c.build_image("demo", "t", "FROM x", {})
    assert len(calls) == 1


async def test_the_retry_is_bounded(monkeypatch):
    monkeypatch.setenv("RAMEN_BUILD_IDENTITY_WAIT_SECS", "0")
    c, calls = cloud(monkeypatch, [BuildError("failed", AZURE), BuildError("failed", AZURE)])
    with pytest.raises(BuildError) as e:
        await c.build_image("demo", "t", "FROM x", {})
    assert len(calls) == 1 and "AADSTS70025" in str(e.value.log if hasattr(e.value, "log") else e.value)
