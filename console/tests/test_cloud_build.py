"""§24.2 worker images built on deploy: the kaniko Job per provider, success, failure with the log tail, timeout,
re-runs, and the local adapter's docker path."""

import hashlib
import json
import subprocess
from types import SimpleNamespace as NS

import pytest

from ramen_console.cloud.aws import AwsCloud
from ramen_console.cloud.azure import AzureCloud
from ramen_console.cloud.base import BuildError
from ramen_console.cloud.gcp import GcpCloud
from ramen_console.cloud.gcp_k8s import BUILDER_IMAGE, build_name, build_objects, job_state
from ramen_console.cloud.local import LocalCloud, docker_build
from ramen_console.errors import ApiError
from tests.fakes_aws import BUCKET, FakeAwsClients, aws_env
from tests.fakes_azure import ACCOUNT, ISSUER, LOCATION, RG, SUB, VAULT, FakeAzureClients
from tests.fakes_gcp import FakeClients

TAG = "b-demo-0123456789abcdef"
JOB = "ramen-build-" + hashlib.sha256(TAG.encode()).hexdigest()[:16]
SYS = "ramen-system"
DOCKERFILE = "FROM reg.example/ramen/worker:1.0.0\nCOPY requirements.txt /opt/ramen/requirements.txt\n"
FILES = {"requirements.txt": "httpx==0.28.1\n"}
REGISTRY = {
    "gcp": "us-central1-docker.pkg.dev/p1/ramen",
    "aws": "123456789012.dkr.ecr.us-east-1.amazonaws.com/ramen",
    "azure": "ramenabc.azurecr.io/ramen",
}


@pytest.fixture(params=["gcp", "aws", "azure"])
def env(request, monkeypatch):
    """(provider, cloud, fake k8s) with a builder configured."""
    p = request.param
    monkeypatch.setenv("RAMEN_BUILDER", "1")
    monkeypatch.setenv("RAMEN_BUILD_REGISTRY", REGISTRY[p])
    for k in ("RAMEN_BUILDER_IMAGE", "RAMEN_BUILD_ARGS", "RAMEN_BUILD_TIMEOUT_SECS", "RAMEN_BUILD_INIT_IMAGE"):
        monkeypatch.delenv(k, raising=False)
    if p == "gcp":
        fk = FakeClients()
        yield p, GcpCloud(project="p1", bucket="b", image="img:1", clients=fk, wait_secs=1, poll=0), fk.k8s
    elif p == "aws":
        with aws_env():
            fk = FakeAwsClients().seed()
            yield p, AwsCloud(bucket=BUCKET, image="img:1", clients=fk, wait_secs=1, poll=0), fk.k8s
    else:
        fk = FakeAzureClients()
        cloud = AzureCloud(SUB, RG, LOCATION, ACCOUNT, VAULT, ISSUER, "img:1", "k", clients=fk, wait_secs=1, poll=0)
        yield p, cloud, fk.k8s


def capture(k8s):
    """Keep what the adapter created: it deletes the Job and the ConfigMap when the build is over."""
    seen = {}
    real = k8s._create

    def create(kind, ns, body):
        seen[kind] = json.loads(json.dumps(body))
        return real(kind, ns, body)

    k8s._create = create
    return seen


async def test_build_job_shape_and_success(env):
    provider, cloud, k8s = env
    assert cloud.builder_available() is True
    seen = capture(k8s)
    k8s.job_polls = 2  # two reads say "running" first
    k8s.pod_logs[(SYS, f"{JOB}-pod", "build")] = [f"line {i}" for i in range(100)]
    r = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    image = f"{REGISTRY[provider]}/worker:{TAG}"
    assert r["image"] == image
    assert r["log"].splitlines() == [f"line {i}" for i in range(60, 100)]  # the last 40
    assert build_name(TAG) == JOB

    cm, job = seen["ConfigMap"], seen["Job"]
    assert cm["metadata"]["name"] == job["metadata"]["name"] == JOB
    assert cm["metadata"]["namespace"] == job["metadata"]["namespace"] == SYS
    assert job["metadata"]["labels"] == {"app.kubernetes.io/name": "ramen-build", "ramen.io/group": "demo"}
    assert sorted(v for k, v in cm["data"].items() if k != "docker-config") == sorted([DOCKERFILE, *FILES.values()])
    spec = job["spec"]
    assert spec["backoffLimit"] == 0 and spec["ttlSecondsAfterFinished"] == 600
    pod = spec["template"]["spec"]
    assert pod["serviceAccountName"] == "ramen-builder" and pod["restartPolicy"] == "Never"
    build = pod["containers"][0]
    assert build["image"] == BUILDER_IMAGE and ":v1." in BUILDER_IMAGE and "latest" not in BUILDER_IMAGE
    assert build["args"][:3] == [
        "--context=dir:///workspace",
        "--dockerfile=/workspace/Dockerfile",
        f"--destination={image}",
    ]
    # GKE Autopilot: no privileged pod, no host access, explicit requests (with ephemeral storage) = limits
    assert "privileged" not in json.dumps(job) and "hostPath" not in json.dumps(job)
    # requests a small node can place (1.0.0 cloud run: 2Gi fitted no EKS or AKS test node), a roomier memory limit
    assert build["resources"]["requests"] == {"cpu": "250m", "memory": "768Mi", "ephemeral-storage": "2Gi"}
    assert build["resources"]["limits"] == {"cpu": "250m", "memory": "2Gi", "ephemeral-storage": "4Gi"}
    # the context: ConfigMap keys mapped back to their paths, copied into the emptyDir kaniko reads
    items = {i["path"]: cm["data"][i["key"]] for i in pod["volumes"][0]["configMap"]["items"]}
    assert items == {"Dockerfile": DOCKERFILE, **FILES} and pod["volumes"][0]["configMap"]["name"] == JOB
    init = pod["initContainers"][0]
    assert init["image"] == "reg.example/ramen/worker:1.0.0" and "cp -RL /context/. /workspace/" in init["command"][-1]
    assert {"name": "workspace", "mountPath": "/workspace"} in init["volumeMounts"]
    assert {"name": "workspace", "mountPath": "/workspace"} in build["volumeMounts"]
    assert {"name": "workspace", "emptyDir": {}} in pod["volumes"]

    host = REGISTRY[provider].split("/")[0]
    labels = spec["template"]["metadata"]["labels"]
    if provider == "gcp":  # workload identity through the metadata server: nothing else on the pod
        assert "docker-config" not in cm["data"] and build["env"] == [] and len(pod["volumes"]) == 2
    elif provider == "aws":
        assert json.loads(cm["data"]["docker-config"]) == {"credHelpers": {host: "ecr-login"}}
        assert {"name": "AWS_REGION", "value": "us-east-1"} in build["env"]
        assert {"name": "docker", "mountPath": "/kaniko/.docker"} in build["volumeMounts"]
    else:
        assert json.loads(cm["data"]["docker-config"]) == {"credHelpers": {host: "acr-env"}}
        assert labels["azure.workload.identity/use"] == "true"
        assert pod["volumes"][2]["configMap"]["items"] == [{"key": "docker-config", "path": "config.json"}]

    # Job and ConfigMap are gone afterwards
    assert ("Job", SYS, JOB) not in k8s.objs and ("ConfigMap", SYS, JOB) not in k8s.objs
    assert ("propagation", "Background") in k8s.calls

    again = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)  # idempotent: same tag, same reference
    assert again["image"] == image and ("Job", SYS, JOB) not in k8s.objs


async def test_build_failure_raises_with_the_log_tail(env):
    _, cloud, k8s = env
    k8s.job_outcome = "failed"
    k8s.pod_logs[(SYS, f"{JOB}-pod", "build")] = [f"l{i}" for i in range(50)] + ["ERROR: No matching distribution"]
    with pytest.raises(BuildError) as e:
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert "failed" in str(e.value) and str(e.value).endswith("ERROR: No matching distribution")
    assert len(e.value.log.splitlines()) == 40 and e.value.log.splitlines()[0] == "l11"
    assert ("Job", SYS, JOB) not in k8s.objs and ("ConfigMap", SYS, JOB) not in k8s.objs


async def test_build_timeout_stops_the_job(env, monkeypatch):
    _, cloud, k8s = env
    monkeypatch.setenv("RAMEN_BUILD_TIMEOUT_SECS", "0")
    k8s.job_outcome = "running"
    k8s.pod_logs[(SYS, f"{JOB}-pod", "build")] = ["Unpacking rootfs"]
    with pytest.raises(BuildError) as e:
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert "timed out after 0s" in str(e.value) and e.value.log == "Unpacking rootfs"
    assert ("Job", SYS, JOB) not in k8s.objs and ("ConfigMap", SYS, JOB) not in k8s.objs


async def test_rerun_replaces_a_failed_job_and_joins_a_running_one(env):
    _, cloud, k8s = env
    _, job = build_objects("demo", TAG, "x/worker:t", DOCKERFILE, FILES)
    k8s.objs[("Job", SYS, JOB)] = {**job, "marker": "old"}
    k8s.job_outcome = "failed"
    seen = capture(k8s)
    with pytest.raises(BuildError):
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert "Job" in seen  # the failed leftover was deleted and a new Job created

    k8s.objs[("Job", SYS, JOB)] = {**job, "marker": "running elsewhere"}
    k8s.job_outcome, k8s.job_polls = "succeeded", 2
    seen.clear()
    r = await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert r["image"].endswith(f"/worker:{TAG}") and "Job" not in seen and "ConfigMap" not in seen  # joined it


async def test_a_job_removed_by_someone_else_is_an_error_not_a_hang(env):
    _, cloud, k8s = env
    real = k8s._create

    def create(kind, ns, body):
        out = real(kind, ns, body)
        if kind == "Job":
            del k8s.objs[("Job", ns, body["metadata"]["name"])]
        return out

    k8s._create = create
    with pytest.raises(BuildError) as e:
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert "removed before it finished" in str(e.value)


async def test_a_build_pod_that_never_started_says_why(env):
    _, cloud, k8s = env
    k8s.job_outcome = "failed"
    with pytest.raises(BuildError) as e:
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert f"build pod {JOB}-pod" in e.value.log


async def test_builder_settings_from_env(env, monkeypatch):
    _, cloud, k8s = env
    monkeypatch.setenv("RAMEN_BUILDER_IMAGE", "mirror.example/kaniko:v9")
    monkeypatch.setenv("RAMEN_BUILD_ARGS", "--insecure --insecure-pull")
    monkeypatch.setenv("RAMEN_BUILD_INIT_IMAGE", "busybox:1")
    monkeypatch.setenv("RAMEN_BUILD_MEMORY", "512Mi")
    seen = capture(k8s)
    await cloud.build_image("demo", TAG, DOCKERFILE, {"pkg/extra.txt": "x"})
    pod = seen["Job"]["spec"]["template"]["spec"]
    assert pod["containers"][0]["image"] == "mirror.example/kaniko:v9"
    assert pod["containers"][0]["args"][-2:] == ["--insecure", "--insecure-pull"]
    assert pod["initContainers"][0]["image"] == "busybox:1"
    assert pod["containers"][0]["resources"]["requests"]["memory"] == "512Mi"
    assert "pkg/extra.txt" in [i["path"] for i in pod["volumes"][0]["configMap"]["items"]]
    assert all("/" not in k for k in seen["ConfigMap"]["data"])


async def test_no_builder_is_a_422(env, monkeypatch):
    _, cloud, k8s = env
    monkeypatch.setenv("RAMEN_BUILDER", "0")
    assert cloud.builder_available() is False
    with pytest.raises(ApiError) as e:
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert e.value.status_code == 422 and not k8s.objs
    monkeypatch.setenv("RAMEN_BUILDER", "1")
    monkeypatch.setenv("RAMEN_BUILD_REGISTRY", " ")
    assert cloud.builder_available() is False


async def test_kube_failures_surface_as_502(env):
    provider, cloud, k8s = env

    def boom(kind, ns, body):
        raise RuntimeError("apiserver down")

    k8s._create = boom
    with pytest.raises(ApiError) as e:
        await cloud.build_image("demo", TAG, DOCKERFILE, FILES)
    assert e.value.status_code == 502 and e.value.detail.lower().startswith(f"{provider} build_image")


def test_job_state_and_from_parsing():
    assert job_state(None) == "running" and job_state({"status": {"active": 1}}) == "running"
    assert job_state({"status": {"conditions": [{"type": "Failed", "status": "True"}]}}) == "failed"
    assert job_state({"status": {"succeeded": 1}}) == "succeeded"
    _, job = build_objects("g", "t", "r/worker:t", "ARG X\nfrom --platform=linux/amd64 base:1 AS b\n", {})
    assert job["spec"]["template"]["spec"]["initContainers"][0]["image"] == "base:1"
    _, job = build_objects("g", "t", "r/worker:t", "# nothing\n", {})
    assert job["spec"]["template"]["spec"]["initContainers"][0]["image"].startswith("busybox:")


# local adapter ---------------------------------------------------------------------
async def test_local_has_no_builder_without_a_registry(tmp_path, monkeypatch):
    monkeypatch.delenv("RAMEN_BUILD_REGISTRY", raising=False)
    monkeypatch.setenv("RAMEN_BUILDER", "1")
    c = LocalCloud(tmp_path)
    assert c.builder_available() is False
    with pytest.raises(ApiError) as e:
        await c.build_image("demo", TAG, DOCKERFILE, FILES)
    assert e.value.status_code == 422


def test_local_docker_build_and_push(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1] == "build":
            ctx = cmd[-1]
            assert open(f"{ctx}/Dockerfile").read() == DOCKERFILE
            assert open(f"{ctx}/requirements.txt").read() == FILES["requirements.txt"]
        return NS(returncode=0, stdout=f"{cmd[1]} ok\n", stderr="")

    r = docker_build("localhost:5001/ramen/worker:t", DOCKERFILE, FILES, run=run)
    assert r == {"image": "localhost:5001/ramen/worker:t", "log": "build ok\npush ok"}
    assert [c[:2] for c in calls] == [["docker", "build"], ["docker", "push"]]

    with pytest.raises(BuildError) as e:
        docker_build("r/worker:t", DOCKERFILE, FILES, run=lambda cmd, **kw: NS(returncode=1, stdout="", stderr="boom"))
    assert "failed (build)" in str(e.value) and e.value.log == "boom"

    def slow(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, 1)

    with pytest.raises(BuildError) as e:
        docker_build("r/worker:t", DOCKERFILE, FILES, run=slow)
    assert "timed out" in str(e.value)
    with pytest.raises(ValueError, match="escapes"):
        docker_build("r/worker:t", DOCKERFILE, {"../x": "1"}, run=run)
    monkeypatch.setattr("shutil.which", lambda _: None)
    with pytest.raises(ApiError):
        docker_build("r/worker:t", DOCKERFILE, FILES)


async def test_local_build_with_a_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMEN_BUILDER", "1")
    monkeypatch.setenv("RAMEN_BUILD_REGISTRY", "localhost:5001/ramen/")
    seen = {}

    def fake(image, dockerfile, files):
        seen.update(image=image, files=files)
        return {"image": image, "log": ""}

    monkeypatch.setattr("ramen_console.cloud.local.docker_build", fake)
    c = LocalCloud(tmp_path)
    assert c.builder_available() is True
    assert (await c.build_image("demo", TAG, DOCKERFILE, FILES))["image"] == f"localhost:5001/ramen/worker:{TAG}"
    assert seen["files"] == FILES
