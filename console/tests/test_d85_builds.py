"""CONTRACTS §38.2 (D85, F2): per-group image repositories and build identities, the digest a build pushed, the
`requirements.txt` refusal of option / URL lines unless `build.allow_urls`, and the teardown with the group."""

import json
from types import SimpleNamespace as NS

import pytest

from ramen_console import imagebuild
from ramen_console.cloud import gcp_api
from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.azure import AzureCloud
from ramen_console.cloud.azure_api import BUILD_TAG, Acr, ArmError
from ramen_console.cloud.gcp import GcpCloud
from tests.fakes_aws import ACCOUNT as AWS_ACCOUNT
from tests.fakes_aws import BUCKET, FakeAwsClients, aws_env
from tests.fakes_azure import ACCOUNT, ISSUER, LOCATION, RG, SUB, VAULT, FakeAzureClients
from tests.fakes_gcp import FakeApiError, FakeClients
from tests.test_api import PW, app, client, demo, login, make_user, root  # noqa: F401 - pytest fixtures
from tests.test_v100_build import REQ, builds, cloud, deploy, requirements  # noqa: F401 - pytest fixtures
from tests.test_v100c_evals import by_kind, console_env, needs_helm, template

SYS = "ramen-system"
TAG = "b-demo-0123456789abcdef"
DOCKERFILE = "FROM base:1\n"
FILES = {"requirements.txt": "six==1.17.0\n"}
DIGEST = "sha256:" + "cd" * 32
ECR = f"{AWS_ACCOUNT}.dkr.ecr.us-east-1.amazonaws.com"


# requirements.txt -------------------------------------------------------------------------------------
@pytest.mark.parametrize(
    "text",
    [
        "six==1.17.0\nrequests>=2,<3 ; python_version >= '3.12'\n",
        "# comment\n\nhttpx[http2]==0.28.1  # inline\n",
        "--require-hashes\nsix==1.17.0 \\\n    --hash=sha256:abc\n",
        "--only-binary=:all:\n--prefer-binary\n--pre\nnumpy\n",
    ],
)
def test_plain_index_requirements_pass(text):
    assert imagebuild.refused(text) is None


@pytest.mark.parametrize(
    ("text", "line"),
    [
        ("six\n-e git+https://github.com/x/y#egg=y\n", 2),
        ("--index-url https://evil.example/simple\nsix\n", 1),
        ("-i https://evil.example/simple\n", 1),
        ("six\n--extra-index-url=https://evil.example/simple\n", 2),
        ("-f ./wheels\n", 1),
        ("--find-links https://evil.example/\n", 1),
        ("--trusted-host evil.example\n", 1),
        ("-r other.txt\n", 1),
        ("-c https://evil.example/constraints.txt\n", 1),
        ("pkg @ https://evil.example/pkg-1.0.tar.gz\n", 1),
        ("pkg[extra] @ file:///tmp/pkg\n", 1),
        ("git+https://github.com/x/y\n", 1),
        ("https://evil.example/pkg.whl\n", 1),
        ("./local/pkg\n", 1),
        ("/abs/pkg.tar.gz\n", 1),
        ("pkg-1.0-py3-none-any.whl\n", 1),
        ("six==1.17.0 --global-option=build_ext\n", 1),
        ("six\nfoo \\\n  @ https://evil.example/x\n", 2),
        ("six\n-e .\\", 2),
    ],
)
def test_option_and_url_lines_are_refused_naming_the_line(text, line):
    n, bad = imagebuild.refused(text.encode())
    assert n == line and bad


def test_repository_component_and_reference_parsing():
    assert imagebuild.repo_component("team-a") == "team-a"
    odd = imagebuild.repo_component("team--a")
    assert odd.startswith("g-") and len(odd) == 18 and odd != imagebuild.repo_component("team-a-")
    assert imagebuild.repo_of("r.example:5000/ramen/groups/d/worker:b-1") == "r.example:5000/ramen/groups/d/worker"
    assert imagebuild.repo_of(f"r.example/x/worker@{DIGEST}") == "r.example/x/worker"
    assert imagebuild.repo_of("r.example:5000/x/worker") == "r.example:5000/x/worker"
    assert imagebuild.repository(object(), "demo") is None


def test_a_refused_line_fails_the_deploy_until_a_group_admin_allows_urls(demo, cloud, tmp_path):  # noqa: F811
    requirements(tmp_path, b"six==1.17.0\n--extra-index-url https://evil.example/simple\nhttpx\n")
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    job = deploy(demo)
    assert job["status"] == "error" and builds(cloud) == [] and cloud.deploys == []
    assert "line 2" in job["error"] and "--extra-index-url https://evil.example/simple" in job["error"]
    assert "build.allow_urls" in job["error"]
    assert any("line 2 is refused" in ln for ln in job["log"])
    make_user(demo, "v@x", "viewer", ["demo"])
    from fastapi.testclient import TestClient

    with TestClient(demo.app) as v:
        login(v, "v@x", PW)
        assert v.put("/api/v1/groups/demo", json={"build": {"allow_urls": True}}).status_code == 403
    r = demo.put("/api/v1/groups/demo", json={"build": {"allow_urls": True}})
    assert r.status_code == 200 and r.json()["build"] == {"allow_urls": True}
    a = [x for x in demo.get("/api/v1/audit?limit=100").json() if x["action"] == "group.update" and x["ok"]]
    assert any("build.allow_urls:True" in x["tags"] for x in a)
    assert deploy(demo)["status"] == "ok" and len(builds(cloud)) == 1


def test_build_is_a_reserved_group_name(root):  # noqa: F811
    r = root.post("/api/v1/groups", json={"name": "build"})
    assert r.status_code == 422 and "reserved" in r.json()["detail"]


def test_a_shared_repository_record_is_rebuilt_into_the_group_repository_and_pinned_by_digest(
    demo,  # noqa: F811
    cloud,  # noqa: F811
    tmp_path,
):
    requirements(tmp_path)
    demo.put("/api/v1/groups/demo", json={"build_image": True})
    assert deploy(demo)["status"] == "ok" and len(builds(cloud)) == 1  # pre-D85: reg.example/ramen/worker:<tag>
    repo = "reg.example/ramen/groups/demo/worker"
    cloud.build_repository = lambda group: repo
    cloud.build_out = {"image": f"{repo}@{DIGEST}", "digest": DIGEST, "tag_ref": f"{repo}:x", "log": "pushed"}
    job = deploy(demo)
    assert job["status"] == "ok" and len(builds(cloud)) == 2, job["log"]  # the shared record no longer counts
    assert cloud.deploys[-1]["spec"]["image"] == f"{repo}@{DIGEST}"
    recs = demo.get("/api/v1/groups/demo/images").json()
    new = next(r for r in recs if r["ref"].startswith(repo))
    assert new["digest"] == DIGEST and new["status"] == "ok"
    job = deploy(demo)  # the group-repository record counts: nothing is built
    assert job["status"] == "ok" and len(builds(cloud)) == 2 and any("already built" in ln for ln in job["log"])


# adapters --------------------------------------------------------------------------------------------
@pytest.fixture
def build_env(monkeypatch):
    monkeypatch.setenv("RAMEN_BUILDER", "1")
    for k in ("RAMEN_BUILD_IDENTITIES", "RAMEN_AZURE_ACR_ID", "RAMEN_BUILD_TIMEOUT_SECS", "RAMEN_BUILD_ARGS"):
        monkeypatch.delenv(k, raising=False)
    return monkeypatch


def ksa(k8s, group="demo"):
    return k8s.objs[("ServiceAccount", SYS, f"ramen-build-{group}")]


def gcp(build_env, registry="us-central1-docker.pkg.dev/p1/ramen"):
    build_env.setenv("RAMEN_BUILD_REGISTRY", registry)
    fk = FakeClients()
    fk.iam_state["number"] = "4242"
    return GcpCloud(project="p1", bucket="b", image="img:1", clients=fk, wait_secs=1, poll=0), fk


async def test_gcp_creates_the_group_repository_with_its_ksa_as_the_only_writer(build_env):
    cloud, fk = gcp(build_env)
    fk.k8s.job_digest = DIGEST
    r = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    repo = "projects/p1/locations/us-central1/repositories/ramen-groups-demo"
    assert r["image"] == f"us-central1-docker.pkg.dev/p1/ramen-groups-demo/worker@{DIGEST}"
    made = fk.ar_state["repos"][repo]
    assert made["format"] == "DOCKER" and made["labels"] == {"ramen": "group-worker", "ramen-group": "demo"}
    (b,) = fk.ar_state["policies"][repo]["bindings"]
    assert b["role"] == "roles/artifactregistry.writer"
    assert b["members"] == [
        "principal://iam.googleapis.com/projects/4242/locations/global/workloadIdentityPools/p1.svc.id.goog"
        "/subject/ns/ramen-system/sa/ramen-build-demo"
    ]
    sa = ksa(fk.k8s)
    assert sa["automountServiceAccountToken"] is False and sa["metadata"]["annotations"] == {}
    assert sa["metadata"]["labels"]["ramen.io/group"] == "demo"
    assert not fk.iam_state.get("accounts")  # no GSA: the KSA itself is the principal
    creates = [c for c in fk.artifactregistry.calls if c[1] == "create"]
    await cloud.build_image("demo", TAG, DOCKERFILE, FILES)  # idempotent: no second repository or binding
    assert [c for c in fk.artifactregistry.calls if c[1] == "create"] == creates
    assert len(fk.ar_state["policies"][repo]["bindings"][0]["members"]) == 1

    removed = await cloud.detach_group("demo")
    assert removed["repositories"] == ["ramen-groups-demo"] and f"{SYS}/ramen-build-demo" in removed["service_accounts"]
    assert repo not in fk.ar_state["repos"] and ("ServiceAccount", SYS, "ramen-build-demo") not in fk.k8s.objs


async def test_gcp_without_a_digest_answers_the_tag_and_a_registry_failure_is_a_note(build_env):
    cloud, fk = gcp(build_env)
    r = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert r["image"] == f"us-central1-docker.pkg.dev/p1/ramen-groups-demo/worker:{TAG}" and r["digest"] is None
    fk.ar_state["fail"]["delete"] = 403
    removed = await cloud.detach_group("demo")
    assert "build identity not fully removed" in removed["build_note"] and "injected delete" in removed["build_note"]


async def test_gcp_registry_helper_edges():
    fk = FakeClients()
    reg = gcp_api.Registry(fk.artifactregistry, "p1", "us-central1", attempts=2, delay=0)
    assert reg.ensure("r1", {}, "d") is True and reg.ensure("r1", {}, "d") is False
    fk.ar_state["fail"]["get"] = 404  # created between our read and our create: someone else made it
    fk.ar_state["fail"]["create"] = 409
    assert reg.ensure("r2", {}, "d") is False
    fk.ar_state["fail"]["get"] = 500
    with pytest.raises(FakeApiError):
        reg.get("r1")
    fk.ar_state["fail"]["create"] = 403
    with pytest.raises(FakeApiError):
        reg.ensure("r3", {}, "d")
    assert reg.delete("nope") is False
    fk.ar_state["fail"]["delete"] = 500
    with pytest.raises(FakeApiError):
        reg.delete("r1")


async def test_identities_none_and_a_plain_registry_create_only_the_ksa(build_env):
    build_env.setenv("RAMEN_BUILD_IDENTITIES", "none")
    cloud, fk = gcp(build_env, "kind-registry:5000/ramen")
    r = await cloud.build_image("team--a", "b-t", DOCKERFILE, FILES)
    comp = imagebuild.repo_component("team--a")
    assert r["image"] == f"kind-registry:5000/ramen/groups/{comp}/worker:b-t"
    assert fk.artifactregistry.calls == [] and ksa(fk.k8s, "team--a")["metadata"]["annotations"] == {}
    removed = await cloud.detach_group("team--a")
    assert removed["service_accounts"] == [f"{SYS}/ramen-build-team--a"] and "repositories" not in removed
    build_env.setenv("RAMEN_BUILD_IDENTITIES", "cloud")  # identities on, but not Artifact Registry: nothing to make
    await cloud.build_image("demo", "b-t", DOCKERFILE, FILES)
    assert fk.artifactregistry.calls == []
    assert "repositories" not in await cloud.detach_group("demo")
    build_env.delenv("RAMEN_BUILD_REGISTRY")
    assert cloud.build_repository("demo") is None


async def test_aws_creates_the_ecr_repository_and_an_irsa_role_that_pushes_only_there(build_env):
    build_env.setenv("RAMEN_BUILD_REGISTRY", f"{ECR}/ramen")
    with aws_env():
        fk = FakeAwsClients().seed()
        cloud = AwsCloud(bucket=BUCKET, image=f"{ECR}/ramen/worker:1.0.0", clients=fk, wait_secs=1, poll=0)
        fk.k8s.job_digest = DIGEST
        r = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
        assert r["image"] == f"{ECR}/ramen/groups/demo/worker@{DIGEST}"
        (repo,) = fk.ecr.describe_repositories(repositoryNames=["ramen/groups/demo/worker"])["repositories"]
        tags = fk.ecr.list_tags_for_resource(resourceArn=repo["repositoryArn"])["tags"]
        assert {"Key": "group", "Value": "demo"} in tags
        role = fk.iam.get_role(RoleName="ramen-build-demo")["Role"]
        assert role["Path"] == "/ramen/build/"
        assert role["PermissionsBoundary"]["PermissionsBoundaryArn"].endswith(":policy/ramen/ramen-build-boundary")
        assert {"Key": "ramen-build", "Value": "demo"} in role["Tags"]
        trust = role["AssumeRolePolicyDocument"]
        trust = json.loads(trust) if isinstance(trust, str) else trust
        cond = trust["Statement"][0]["Condition"]["StringEquals"]
        assert "system:serviceaccount:ramen-system:ramen-build-demo" in cond.values()
        doc = fk.iam.get_role_policy(RoleName="ramen-build-demo", PolicyName="ramen-build")["PolicyDocument"]
        doc = json.loads(doc) if isinstance(doc, str) else doc
        push = [s for s in doc["Statement"] if "ecr:PutImage" in s["Action"]]
        assert [s["Resource"] for s in push] == [repo["repositoryArn"]]
        pull = [s for s in doc["Statement"] if s["Action"] == aws_api_pull()]
        assert pull[0]["Resource"] == [f"arn:aws:ecr:us-east-1:{AWS_ACCOUNT}:repository/ramen/worker"]
        assert ksa(fk.k8s)["metadata"]["annotations"] == {"eks.amazonaws.com/role-arn": role["Arn"]}
        assert role["Arn"] not in cloud._accounts()

        await cloud.build_image("other", TAG, DOCKERFILE, FILES)
        removed = await cloud.detach_group("build")  # a zone role prefix `ramen-build-` must not take build roles
        assert fk.iam.get_role(RoleName="ramen-build-other")["Role"]["Path"] == "/ramen/build/"
        assert "repositories" not in removed or removed["repositories"] == []
        removed = await cloud.detach_group("demo")
        assert removed["repositories"] == [f"{ECR}/ramen/groups/demo/worker"]
        assert "ramen-build-demo" in removed["service_accounts"]
        assert fk.ecr.describe_repositories()["repositories"][0]["repositoryName"] == "ramen/groups/other/worker"


def aws_api_pull():
    from ramen_console.cloud.aws_api import Iam

    return Iam.PULL


async def test_aws_public_base_needs_no_pull_grant_and_ecr_errors_propagate(build_env):
    from ramen_console.cloud import aws_api

    assert aws_api.ecr_repo_arn("ghcr.io/x/worker:1", "us-east-1", AWS_ACCOUNT) is None
    assert aws_api.ecr_repo_arn(f"{ECR}/ramen/worker@{DIGEST}", "us-east-1", AWS_ACCOUNT).endswith(
        ":repository/ramen/worker"
    )
    policy = aws_api.Iam(None, None, None, "us-east-1", "c", build_boundary="-").build_policy("arn:repo", [])
    assert len(policy["Statement"]) == 2
    assert aws_api.Iam(None, None, None, "us-east-1", "c", build_boundary="-").build_boundary() is None

    class Boom:
        def create_repository(self, **kw):
            raise RuntimeError("AccessDenied")

        def delete_repository(self, **kw):
            raise RuntimeError("AccessDenied")

    with pytest.raises(RuntimeError):
        aws_api.Ecr(Boom()).ensure("r", {})
    with pytest.raises(RuntimeError):
        aws_api.Ecr(Boom()).delete("r")


async def test_azure_identity_federated_with_a_repository_scoped_writer(build_env):
    build_env.setenv("RAMEN_BUILD_REGISTRY", "ramenabc.azurecr.io/ramen")
    fk = FakeAzureClients()
    image = "ramenabc.azurecr.io/ramen/worker:1.0.0"
    cloud = AzureCloud(SUB, RG, LOCATION, ACCOUNT, VAULT, ISSUER, image, "k", clients=fk, wait_secs=1, poll=0)
    fk.k8s.job_digest = DIGEST
    r = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert r["image"] == f"ramenabc.azurecr.io/ramen/groups/demo/worker@{DIGEST}"
    ident = fk.arm.identities["ramen-build-demo"]
    assert ident["tags"] == {BUILD_TAG: "demo"}
    fic = fk.arm.fics[("ramen-build-demo", "build")]["properties"]
    assert fic["subject"] == "system:serviceaccount:ramen-system:ramen-build-demo" and fic["issuer"] == ISSUER
    acr = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.ContainerRegistry/registries/ramenabc"
    grants = {
        a["properties"]["roleDefinitionId"].rsplit("/", 1)[1]: a["properties"]
        for a in fk.arm.assignments.values()
        if a["properties"]["principalId"] == ident["properties"]["principalId"]
    }
    writer, reader = grants["2a1e307c-b015-4ebd-883e-5b7698a07328"], grants["b93aa761-3e63-49ed-ac28-beffa264f7ac"]
    assert writer["scope"] == reader["scope"] == acr and writer["conditionVersion"] == "2.0"
    assert "StringEqualsIgnoreCase 'ramen/groups/demo/worker'" in writer["condition"]
    assert "content/write" in writer["condition"] and "content/write" not in reader["condition"]
    assert "StringEqualsIgnoreCase 'ramen/worker'" in reader["condition"]
    assert ksa(fk.k8s)["metadata"]["annotations"] == {
        "azure.workload.identity/client-id": ident["properties"]["clientId"]
    }
    assert "ramen-build-demo" not in cloud._ids().all() and cloud._ids().of_group("demo") == []

    fk.acr_plane.repos.add(("ramenabc.azurecr.io", "ramen/groups/demo/worker"))
    removed = await cloud.detach_group("demo")
    assert removed["repositories"] == ["ramenabc.azurecr.io/ramen/groups/demo/worker"]
    assert "ramen-build-demo" in removed["service_accounts"] and "ramen-build-demo" not in fk.arm.identities
    assert not [
        a for a in fk.arm.assignments.values() if a["properties"]["principalId"] == ident["properties"]["principalId"]
    ]


async def test_azure_acr_id_override_and_a_base_elsewhere(build_env):
    build_env.setenv("RAMEN_BUILD_REGISTRY", "ramenabc.azurecr.io/ramen")
    build_env.setenv(
        "RAMEN_AZURE_ACR_ID", "/subscriptions/s/resourceGroups/other/providers/Microsoft.ContainerRegistry/registries/r"
    )
    fk = FakeAzureClients()
    cloud = AzureCloud(
        SUB, RG, LOCATION, ACCOUNT, VAULT, ISSUER, "ghcr.io/x/worker:1", "k", clients=fk, wait_secs=1, poll=0
    )
    await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    (a,) = fk.arm.assignments.values()
    assert a["properties"]["scope"].endswith("/resourceGroups/other/providers/Microsoft.ContainerRegistry/registries/r")
    assert (await cloud.detach_group("demo")).get("repositories") == []


class FakeHttp:
    def __init__(self, delete_status=202, fail=None):
        self.calls, self.delete_status, self.fail = [], delete_status, fail

    def post(self, url, data=None, timeout=None):
        self.calls.append(("POST", url, data))
        if self.fail and self.fail in url:
            return NS(status_code=401, text="unauthorized", json=dict)
        body = {"refresh_token": "rt"} if url.endswith("/exchange") else {"access_token": "at"}
        return NS(status_code=200, text="", json=lambda: body)

    def delete(self, url, headers=None, timeout=None):
        self.calls.append(("DELETE", url, headers))
        return NS(status_code=self.delete_status, text="x")


def test_acr_repository_delete_exchanges_a_token_scoped_to_that_repository():
    http = FakeHttp()
    acr = Acr("r.azurecr.io", lambda: "aad", http=http)
    assert acr.delete_repository("ramen/groups/demo/worker") is True
    (_, _, ex), (_, _, tok), (_, url, headers) = http.calls
    assert ex == {"grant_type": "access_token", "service": "r.azurecr.io", "access_token": "aad"}
    assert tok["scope"] == "repository:ramen/groups/demo/worker:delete" and tok["refresh_token"] == "rt"
    assert url == "https://r.azurecr.io/acr/v1/ramen/groups/demo/worker" and headers == {"Authorization": "Bearer at"}
    assert Acr("r.azurecr.io", lambda: "aad", http=FakeHttp(404)).delete_repository("x") is False
    with pytest.raises(ArmError):
        Acr("r.azurecr.io", lambda: "aad", http=FakeHttp(500)).delete_repository("x")
    with pytest.raises(ArmError):
        Acr("r.azurecr.io", lambda: "aad", http=FakeHttp(fail="/oauth2/token")).delete_repository("x")


# chart -------------------------------------------------------------------------------------------------------
@needs_helm
def test_the_chart_pins_platform_images_by_digest_and_has_no_shared_builder():
    d1, d2 = "sha256:" + "1" * 64, "sha256:" + "2" * 64
    rc, err, docs = template("image.digest=" + d1, "image.workerDigest=" + d2, "builder.enabled=true")
    assert rc == 0, err
    console = by_kind(docs, "Deployment", "console")["spec"]["template"]["spec"]["containers"][0]
    assert console["image"].endswith(f"/ramen/console@{d1}")
    env = console_env(docs)
    assert env["RAMEN_IMAGE_WORKER"].endswith(f"/ramen/worker@{d2}") and env["RAMEN_BUILD_IDENTITIES"] == "cloud"
    assert by_kind(docs, "ServiceAccount", "ramen-builder") is None
    rules = by_kind(docs, "Role", "ramen-console-build")["rules"]
    assert {"apiGroups": [""], "resources": ["serviceaccounts"], "verbs": ["create", "patch", "delete"]} in rules
    rc, _, docs = template("builder.enabled=true", "builder.identities=none")
    assert console_env(docs)["RAMEN_BUILD_IDENTITIES"] == "none"
    console = by_kind(docs, "Deployment", "console")["spec"]["template"]["spec"]["containers"][0]
    assert console["image"].endswith("/ramen/console:1.0.0")  # no digest: the tag
    rc, err, _ = template("image.digest=sha256:short")
    assert rc != 0 and "is not sha256:<64 hex>" in err


async def test_gcp_registry_treats_a_403_on_get_as_not_created_yet():
    # GKE 1.0.0c: AR answers 403 (not 404) on get for a repository that does not exist yet when the console's get
    # right is conditioned on the repository name — ensure must go on to create it
    fk = FakeClients()
    reg = gcp_api.Registry(fk.artifactregistry, "p1", "us-central1", attempts=2, delay=0)
    reg.get("warm-up")  # the fake builds its state on the first call
    fk.ar_state["fail"]["get"] = 403
    assert reg.ensure("ramen-groups-demo", {}, "d") is True
    fk.ar_state["fail"]["get"] = 403  # injected failures are one-shot
    with pytest.raises(FakeApiError):  # a plain get still raises on 403
        reg.get("ramen-groups-demo")
