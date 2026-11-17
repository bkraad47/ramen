"""A pooled Kubernetes API connection the server closed while idle (AKS, live 1.0.0b26: two canary deploys failed with
`ProtocolError: ('Connection aborted.', RemoteDisconnected(...))` one second after "applying zone manifests"):
urllib3 resends a GET on that error but never a POST/PATCH, so the first write after an idle gap failed the deploy."""

import json
import socket
import threading

import pytest
import urllib3
from kubernetes import client

from ramen_console.cloud import gcp_clients
from ramen_console.cloud.gcp_clients import kube_retry, remote_kube

NS = {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "ramen-demo-a"}}


class DroppingServer:
    """HTTP/1.1 keep-alive; on each connection the second request is read and the socket closed with no answer — what
    the client sees when the server's idle close races its next request on a pooled connection."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen()
        self.port, self.requests, self.dropped = self.sock.getsockname()[1], [], 0
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while True:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._conn, args=(conn,), daemon=True).start()

    def _conn(self, conn):
        with conn, conn.makefile("rb") as f:
            for n in range(2):
                line = f.readline()
                if not line:
                    return
                headers = {}
                while (h := f.readline()) not in (b"\r\n", b""):
                    k, _, v = h.decode().partition(":")
                    headers[k.strip().lower()] = v.strip()
                f.read(int(headers.get("content-length") or 0))
                if n == 1:
                    self.dropped += 1
                    return
                self.requests.append(line.decode().split(" ")[0])
                body = json.dumps(NS).encode()
                conn.sendall(
                    b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: keep-alive\r\n"
                    + f"Content-Length: {len(body)}\r\n\r\n".encode()
                    + body
                )

    def close(self):
        self.sock.close()


@pytest.fixture
def server():
    s = DroppingServer()
    yield s
    s.close()


def core(port: int, retries) -> client.CoreV1Api:
    cfg = client.Configuration()
    cfg.host = f"http://127.0.0.1:{port}"
    cfg.retries = retries
    return client.CoreV1Api(client.ApiClient(cfg))


def test_urllib3_alone_resends_a_get_but_fails_a_post_on_a_dropped_pooled_connection(server):
    api = core(server.port, None)  # the kubernetes client's default: urllib3's Retry(3), idempotent methods only
    api.read_namespace("ramen-demo-a")
    api.read_namespace("ramen-demo-a")  # dropped, resent on a new connection
    assert server.requests == ["GET", "GET"] and server.dropped == 1
    with pytest.raises(urllib3.exceptions.ProtocolError, match="Remote end closed connection without response"):
        api.create_namespace(NS)  # the second request on the new connection: dropped, never resent


def test_kube_retry_resends_any_method_once_the_connection_was_dropped_unanswered(server):
    api = core(server.port, kube_retry())
    api.read_namespace("ramen-demo-a")
    api.create_namespace(NS)
    api.patch_namespace("ramen-demo-a", {"metadata": {"labels": {"a": "b"}}})
    assert server.requests == ["GET", "POST", "PATCH"] and server.dropped == 2


def test_kube_retry_still_never_resends_a_post_after_a_read_timeout():
    """Only an unanswered close is resent: a timeout may mean the server is still applying the write."""
    r = kube_retry()
    err = urllib3.exceptions.ReadTimeoutError(None, "/api/v1/namespaces", "read timed out")
    with pytest.raises(urllib3.exceptions.ReadTimeoutError):
        r.increment("POST", "/api/v1/namespaces", error=err)
    reset = urllib3.exceptions.ProtocolError("Connection aborted.", ConnectionResetError(104, "reset"))
    assert r.increment("POST", "/api/v1/namespaces", error=reset).total == r.total - 1


def test_every_kube_client_the_console_builds_carries_kube_retry(monkeypatch):
    apis = remote_kube(client, "aks.example:443", b"-----BEGIN CERTIFICATE-----\n", lambda: "t")
    assert isinstance(apis.api.configuration.retries, gcp_clients.retry_class())

    class FakeConfig:
        @staticmethod
        def load_incluster_config():
            c = client.Configuration()
            c.host = "https://10.0.0.1:443"
            client.Configuration.set_default(c)

    before = client.Configuration.get_default_copy()
    try:
        c = gcp_clients.GcpClients("p")
        monkeypatch.setattr(c, "_kube_modules", lambda: (FakeConfig, client))
        api = c.core
        assert api.api_client.configuration.host == "https://10.0.0.1:443"
        assert isinstance(api.api_client.configuration.retries, gcp_clients.retry_class())
    finally:
        client.Configuration.set_default(before)
