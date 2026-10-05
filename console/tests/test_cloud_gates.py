"""0.7.0: the k8s adapters run the console's deploy gate on the canary (B3/C1) and read a repo file from the bucket."""

import pytest

from ramen_console.cloud.base import Cloud, GateError
from tests import test_aws_cloud as aws
from tests.fakes_aws import aws_env
from tests.test_gcp_cloud import SPEC, cloud, fk, http_state, obj  # noqa: F401 - pytest fixtures

CFG = {"RAMEN_MCP_KEYS": "rmk_1"}


async def test_gate_runs_on_the_canary_with_its_reload_result_and_a_bound_call(cloud, fk, http_state):  # noqa: F811
    seen = []

    async def gate(result, call):
        seen.append(result)
        r = await call({"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "calc", "arguments": {}}})
        assert r["result"]["content"][0]["text"] == "{}"

    res = await cloud.deploy("demo", "prod", "a", canary=True, config=CFG, spec=SPEC, gate=gate)
    assert res["ok"] is True, res
    assert seen == [http_state.load_result]  # once: the canary pod, not the stable pods after promotion
    call = [c for c in http_state.calls if c[0] == "Mcp/Call" and c[2].get("method") == "tools/call"]
    assert call and call[0][1]["authorization"] == "Bearer rmk_1" and call[0][1]["ramen-zone"] == "a"


async def test_a_gate_refusal_scales_the_canary_down_and_leaves_stable_alone(cloud, fk, http_state):  # noqa: F811
    await cloud.attach_zone("demo", "a", SPEC)
    import json

    main_before = json.dumps(obj(fk, "Deployment", "ramen-demo-a", "worker"), sort_keys=True)

    async def gate(result, call):
        raise GateError("breaking schema change: removed tool calc")

    lines = []
    res = await cloud.deploy("demo", "prod", "a", canary=True, config=CFG, spec=SPEC, gate=gate, log=lines.append)
    assert res["ok"] is False and res["error"] == "GateError: breaking schema change: removed tool calc"
    assert obj(fk, "Deployment", "ramen-demo-a", "worker-canary")["spec"]["replicas"] == 0
    assert json.dumps(obj(fk, "Deployment", "ramen-demo-a", "worker"), sort_keys=True) == main_before
    assert any("scaled canary to 0" in x for x in lines)


async def test_no_canary_means_no_gate_and_no_key_means_no_call(cloud, fk, http_state):  # noqa: F811
    seen = []

    async def gate(result, call):
        seen.append(call)

    res = await cloud.deploy("demo", "prod", "a", canary=False, config=CFG, spec=SPEC, gate=gate)
    assert res["ok"] and seen == []
    res = await cloud.deploy("demo", "prod", "a", canary=True, config={}, spec=SPEC, gate=gate)
    assert res["ok"] and seen == [None]


async def test_gcp_read_file_comes_from_the_synced_bucket(cloud, fk):  # noqa: F811
    assert await cloud.read_file("demo", "mcp/tests.yaml") is None
    fk.storage.bucket("p1-groups").data["demo/mcp/tests.yaml"] = b"cases: []"
    assert await cloud.read_file("demo", "mcp/tests.yaml") == b"cases: []"


async def test_aws_passes_the_gate_through_and_reads_s3(monkeypatch):
    monkeypatch.setattr(aws.aws_api.time, "sleep", lambda s: None)
    monkeypatch.setenv("RAMEN_ALB_SPLIT_DRAIN_SECS", "0")
    with aws_env():
        from tests.fakes_aws import FakeAwsClients

        fk_ = FakeAwsClients().seed()
        w = aws.FakeWorker(admin_key=aws.ADMIN, mcp_keys=("rmk_1",)).start()
        try:
            c = aws.AwsCloud(
                region="us-east-1",
                bucket=aws.BUCKET,
                image="img",
                admin_key=aws.ADMIN,
                clients=fk_,
                rpc=aws.Client(deadline=2, resolve=w.resolve),
                wait_secs=1,
                poll=0,
            )
            seen = []

            async def gate(result, call):
                seen.append(result)

            res = await c.deploy("demo", "prod", "a", canary=True, config=CFG, spec=SPEC, gate=gate)
            assert res["ok"] and seen == [w.load_result]
            assert await c.read_file("demo", "mcp/tests.yaml") is None
            fk_.s3.put_object(Bucket=aws.BUCKET, Key="demo/mcp/tests.yaml", Body=b"cases: []")
            assert await c.read_file("demo", "mcp/tests.yaml") == b"cases: []"
        finally:
            w.stop()


async def test_base_defaults():
    class Bare(Cloud):
        async def sync_repo(self, *a): ...

        async def deploy(self, *a, **k): ...

        async def rebalance(self, *a): ...

        async def workers(self, *a): ...

        async def logs(self, *a, **k): ...

        async def set_ip_rules(self, *a): ...

        async def create_service_account(self, *a): ...

        async def refresh(self): ...

    assert await Bare().read_file("g", "mcp/tests.yaml") is None
    assert issubclass(GateError, RuntimeError)


@pytest.mark.parametrize("name", ["GateError"])
def test_gate_error_is_importable_from_base(name):
    import ramen_console.cloud.base as base

    assert hasattr(base, name)
