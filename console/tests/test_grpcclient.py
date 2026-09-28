"""ramen_console.grpcclient against the in-process fake worker (CONTRACTS §11)."""

import grpc
import pytest

from ramen_console import grpcclient
from ramen_console.grpcclient import Client, GrpcError, target_of
from tests.fake_grpc import DOWN, FakeWorker


@pytest.fixture
def worker():
    w = FakeWorker().start()
    yield w
    w.stop()


def test_target_of():
    assert target_of("worker:8080") == ("worker:8080", False)
    assert target_of("http://worker:8080") == ("worker:8080", False)
    assert target_of("https://w.example:443/mcp") == ("w.example:443", True)
    assert target_of("grpcs://w:9") == ("w:9", True)
    assert target_of("10.1.0.2") == ("10.1.0.2:8080", False)


def test_credentials():
    assert grpcclient.credentials(None) is None and grpcclient.credentials(False) is None
    assert isinstance(grpcclient.credentials(True), grpc.ChannelCredentials)
    assert isinstance(grpcclient.credentials({"ca": b"-----BEGIN CERTIFICATE-----\n"}), grpc.ChannelCredentials)


async def test_call_reload_metrics_health(worker):
    c = Client(deadline=5)
    r = await c.call(worker.target, "rmk_1", "demo", "a", {"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert r["result"] == {"tools": []}
    rpc, md, body = worker.calls[-1]
    assert rpc == "Mcp/Call" and md["ramen-group"] == "demo" and md["ramen-zone"] == "a"
    assert md["authorization"] == "Bearer rmk_1" and body["method"] == "tools/list"
    # a notification returns an empty body
    assert await c.call(worker.target, "rmk_1", "demo", "a", {"jsonrpc": "2.0", "method": "notifications/x"}) == {}
    assert (await c.reload(worker.target, "adm"))["tools"] == [{"name": "calc"}]
    assert (await c.metrics(worker.target, "adm"))["load"] == "even"
    assert await c.health(worker.target) is True
    worker.set_serving(False)
    assert await c.health(worker.target) is False


async def test_errors_surface_grpc_codes(worker):
    c = Client(deadline=5)
    with pytest.raises(GrpcError) as e:
        await c.call(worker.target, "nope", "demo", "a", {"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert e.value.code == grpc.StatusCode.UNAUTHENTICATED and "UNAUTHENTICATED" in str(e.value)
    with pytest.raises(GrpcError) as e:
        await c.reload(worker.target, "wrong")
    assert e.value.code == grpc.StatusCode.UNAUTHENTICATED
    with pytest.raises(GrpcError) as e:
        await c.metrics(DOWN, "adm")
    assert e.value.code == grpc.StatusCode.UNAVAILABLE and e.value.target == DOWN
    assert await c.health(DOWN) is False


async def test_resolver_and_module_functions(worker):
    c = Client(deadline=5, resolve=lambda t: worker.target)
    assert (await c.metrics("10.2.0.1:8080", "adm"))["total"] == 9
    assert await grpcclient.health(worker.target) is True
    assert (await grpcclient.reload(worker.target, "adm"))["errors"] == []
    assert (await grpcclient.metrics(worker.target, "adm"))["inflight"] == 2
    r = await grpcclient.call(worker.target, "rmk_1", "g", "z", {"jsonrpc": "2.0", "id": 7, "method": "ping"})
    assert r["id"] == 7
