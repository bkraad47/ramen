"""D92 / CONTRACTS §39.7: the TLS front of promptfoo and Jaeger (`ramen_console.tlsproxy`) asks for a credential —
the operator's (Basic or Bearer, everything) or the evals Jobs' upload-only one (one share upload per connection)."""

import asyncio
import base64
import ssl

import pytest

from ramen_console import tls, tlsproxy

FULL, UPLOAD = ("admin", "s3cr3t-full"), ("evals", "s3cr3t-upload")
ROUTES = [("POST", r"^/api/eval$"), ("POST", r"^/api/eval/[A-Za-z0-9:_-]+/results$")]


def _basic(user, pw) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


async def _http_upstream(seen: list):
    """A tiny HTTP/1.1 server: records each request head + body, answers 200 with the path; honours Connection."""

    async def handle(reader, writer):
        try:
            while True:
                head = await reader.readuntil(b"\r\n\r\n")
                lines = head.decode().split("\r\n")
                hdrs = {k.lower(): v.strip() for k, _, v in (h.partition(":") for h in lines[1:] if h)}
                body = await reader.readexactly(int(hdrs.get("content-length", "0")))
                seen.append((lines[0], hdrs, body))
                path = lines[0].split()[1].encode()
                close = hdrs.get("connection", "").lower() == "close"
                writer.write(
                    b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n%s\r\n%s"
                    % (len(path), b"Connection: close\r\n" if close else b"", path)
                )
                await writer.drain()
                if close:
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        writer.close()

    return await asyncio.start_server(handle, "127.0.0.1", 0)


@pytest.fixture
async def proxy(tmp_path):
    ca = tls.new_ca()
    leaf = ca.issue(["promptfoo", "localhost", "127.0.0.1"])
    (tmp_path / "c").write_text(leaf.cert)
    (tmp_path / "k").write_text(leaf.key)
    seen: list = []
    up = await _http_upstream(seen)
    auth = tlsproxy.Auth(full=[FULL], upload=[UPLOAD], routes=tlsproxy.routes([f"{m} {p}" for m, p in ROUTES]))
    server, reloader = await tlsproxy.serve(
        "127.0.0.1:0", f"127.0.0.1:{up.sockets[0].getsockname()[1]}", str(tmp_path / "c"), str(tmp_path / "k"), 60, auth
    )
    ctx = ssl.create_default_context(cadata=ca.bundle)
    port = server.sockets[0].getsockname()[1]

    async def send(raw: bytes) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", port, ssl=ctx, server_hostname="promptfoo")
        w.write(raw)
        await w.drain()
        try:
            return await asyncio.wait_for(r.read(65536), 5)
        finally:
            w.close()

    yield send, seen
    reloader.cancel()
    server.close()
    up.close()


def _req(method: str, path: str, auth: str | None = None, body: bytes = b"", extra: str = "") -> bytes:
    h = f"{method} {path} HTTP/1.1\r\nHost: promptfoo\r\n"
    if auth:
        h += f"Authorization: {auth}\r\n"
    if body or method == "POST":
        h += f"Content-Length: {len(body)}\r\n"
    return (h + extra + "\r\n").encode() + body


async def test_no_credential_no_way_in(proxy):
    send, seen = proxy
    out = await send(_req("GET", "/api/results"))
    assert out.startswith(b"HTTP/1.1 401") and b'WWW-Authenticate: Basic realm="ramen"' in out
    assert seen == []
    out = await send(_req("GET", "/", _basic("admin", "wrong")))
    assert out.startswith(b"HTTP/1.1 401") and seen == []
    out = await send(_req("GET", "/health"))  # no open paths: the probes are TCP checks
    assert out.startswith(b"HTTP/1.1 401")


async def test_the_operator_credential_reaches_everything(proxy):
    send, seen = proxy
    out = await send(_req("GET", "/api/results?x=1", _basic(*FULL)))
    assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"/api/results?x=1")
    out = await send(_req("GET", "/api/traces", f"Bearer {FULL[1]}"))
    assert out.startswith(b"HTTP/1.1 200")
    assert len(seen) == 2


async def test_the_operator_connection_keeps_alive(proxy):
    send, seen = proxy
    two = _req("GET", "/a", _basic(*FULL)) + _req("GET", "/b", _basic(*FULL))
    out = await send(two)
    deadline = asyncio.get_running_loop().time() + 2
    while len(seen) < 2 and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)
    assert [s[0] for s in seen] == ["GET /a HTTP/1.1", "GET /b HTTP/1.1"]
    assert out.startswith(b"HTTP/1.1 200")


async def test_the_upload_credential_may_only_share_one_run_per_connection(proxy):
    send, seen = proxy
    out = await send(_req("POST", "/api/eval", _basic(*UPLOAD), b'{"config":{}}'))
    assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"/api/eval")
    line, hdrs, body = seen[-1]
    assert line == "POST /api/eval HTTP/1.1" and body == b'{"config":{}}' and hdrs["connection"] == "close"
    out = await send(_req("POST", "/api/eval/eval-abc-2026-11-17T10:00:00/results", _basic(*UPLOAD), b"[]"))
    assert out.startswith(b"HTTP/1.1 200")
    for method, path in (
        ("GET", "/api/results"),
        ("GET", "/api/eval/x"),
        ("DELETE", "/api/eval/x"),
        ("POST", "/api/x"),
    ):
        n = len(seen)
        out = await send(_req(method, path, _basic(*UPLOAD)))
        assert out.startswith(b"HTTP/1.1 403"), (method, path)
        assert len(seen) == n
    n = len(seen)
    # a second request smuggled after the allowed one never reaches the upstream
    out = await send(_req("POST", "/api/eval", _basic(*UPLOAD), b"{}") + _req("GET", "/api/results", _basic(*UPLOAD)))
    await asyncio.sleep(0.1)
    assert [s[0] for s in seen[n:]] == ["POST /api/eval HTTP/1.1"]


async def test_the_upload_needs_a_length(proxy):
    send, seen = proxy
    raw = b"POST /api/eval HTTP/1.1\r\nHost: p\r\nAuthorization: %s\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n" % (
        _basic(*UPLOAD).encode()
    )
    assert (await send(raw)).startswith(b"HTTP/1.1 411") and seen == []


async def test_a_garbage_head_is_closed(proxy):
    send, seen = proxy
    assert (await send(b"\x16\x03garbage\r\n\r\n")).startswith(b"HTTP/1.1 400") and seen == []


def test_auth_from_env(monkeypatch):
    assert tlsproxy.auth_from_env({}, []) is None  # no password: a plain pipe, as before D92
    env = {
        "RAMEN_PROXY_USER": "admin",
        "RAMEN_PROXY_PASSWORD": "p1",
        "RAMEN_PROXY_UPLOAD_USER": "evals",
        "RAMEN_PROXY_UPLOAD_PASSWORD": "p2",
    }
    a = tlsproxy.auth_from_env(env, ["POST ^/api/eval$"])
    assert a.check(_basic("admin", "p1")) == "full" and a.check("Bearer p1") == "full"
    assert a.check(_basic("evals", "p2")) == "upload" and a.check("Bearer p2") is None
    assert a.check(_basic("evals", "p1")) is None and a.check("Basic !!!") is None and a.check(None) is None
    assert a.allowed("POST", "/api/eval") and not a.allowed("POST", "/api/eval/../x")
    with pytest.raises(ValueError):
        tlsproxy.routes(["^/api/eval$"])


def test_the_evals_job_shares_with_the_upload_credential_only(monkeypatch):
    from ramen_console import evals

    monkeypatch.setenv("RAMEN_EVALS_SA", "ramen-evals")
    monkeypatch.setenv("RAMEN_ENGINES_SECRET", "ramen-engines")
    api = "https://promptfoo.ramen-system:3000"
    spec = evals.job_spec(
        "demo", "a", "tests: []", {"RAMEN_MCP_KEY": "k"}, image="pf", api_url=api, worker_ip="10.0.0.9"
    )
    c = spec["job"]["spec"]["template"]["spec"]["containers"][0]
    env = {e["name"]: e for e in c["env"]}
    ref = env["RAMEN_PROMPTFOO_UPLOAD_PASSWORD"]["valueFrom"]["secretKeyRef"]
    assert ref == {"name": "ramen-engines", "key": "upload-password"}
    # Kubernetes expands $(VAR) from the entry above; promptfoo turns URL userinfo into Basic auth and strips it
    assert env["PROMPTFOO_REMOTE_API_BASE_URL"]["value"] == (
        "https://evals:$(RAMEN_PROMPTFOO_UPLOAD_PASSWORD)@promptfoo.ramen-system:3000"
    )
    names = [e["name"] for e in c["env"]]
    assert names.index("RAMEN_PROMPTFOO_UPLOAD_PASSWORD") < names.index("PROMPTFOO_REMOTE_API_BASE_URL")
    assert env["PROMPTFOO_INSECURE_SSL"]["value"] == "false"  # promptfoo skips verification by default
    assert "password" not in str(spec["secret"]) and "upload-password" not in str(spec["configmap"])
    # without the chart's Secret (an older chart): no credential, as before
    monkeypatch.delenv("RAMEN_ENGINES_SECRET")
    spec = evals.job_spec("demo", "a", "tests: []", {}, image="pf", api_url=api)
    env = {e["name"]: e for e in spec["job"]["spec"]["template"]["spec"]["containers"][0]["env"]}
    assert env["PROMPTFOO_REMOTE_API_BASE_URL"]["value"] == api and "RAMEN_PROMPTFOO_UPLOAD_PASSWORD" not in env


# ---- the chart ----------------------------------------------------------------------------------------------------
from tests.test_v100c_evals import by_kind, console_env, needs_helm, template  # noqa: E402


def _c(docs, name, container=0):
    return by_kind(docs, "Deployment", name)["spec"]["template"]["spec"]["containers"][container]


def _env_of(c) -> dict:
    return {e["name"]: e.get("value") or e.get("valueFrom") for e in c.get("env", [])}


def _ref(key):
    return {"secretKeyRef": {"name": "ramen-engines", "key": key}}


@needs_helm
def test_the_chart_generates_the_engines_secret_only_with_an_engine():
    rc, err, docs = template()
    assert rc == 0, err
    assert not [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] == "ramen-engines"]
    rc, err, docs = template("otel.collector=phoenix", "evals.engine=promptfoo")
    assert rc == 0, err
    data = by_kind(docs, "Secret", "ramen-engines")["stringData"]
    assert data["username"] == "admin" and data["upload-username"] == "evals"
    for k in ("password", "upload-password", "phoenix-secret", "phoenix-admin-secret"):
        v = data[k]
        assert len(v) >= 32 and any(c.isdigit() for c in v) and any(c.islower() for c in v), k
    assert len({data[k] for k in ("password", "upload-password", "phoenix-secret", "phoenix-admin-secret")}) == 4
    assert console_env(docs)["RAMEN_ENGINES_SECRET"] == "ramen-engines"
    rc, err, docs = template("otel.collector=jaeger", "engines.existingSecret=mine")
    assert rc == 0, err
    assert not [d for d in docs if d["kind"] == "Secret" and d["metadata"]["name"] in ("ramen-engines", "mine")]
    assert _env_of(_c(docs, "jaeger", 1))["RAMEN_PROXY_PASSWORD"]["secretKeyRef"]["name"] == "mine"


@needs_helm
def test_phoenix_requires_a_sign_in_and_its_collector_posts_with_the_admin_secret():
    rc, err, docs = template("otel.collector=phoenix")
    assert rc == 0, err
    env = _env_of(_c(docs, "phoenix"))
    assert env["PHOENIX_ENABLE_AUTH"] == "true" and env["PHOENIX_USE_SECURE_COOKIES"] == "true"
    assert env["PHOENIX_SECRET"] == _ref("phoenix-secret")
    assert env["PHOENIX_ADMIN_SECRET"] == _ref("phoenix-admin-secret")
    assert env["PHOENIX_DEFAULT_ADMIN_INITIAL_PASSWORD"] == _ref("password")
    assert _c(docs, "phoenix")["readinessProbe"]["httpGet"]["path"] == "/healthz"  # unauthenticated by design
    assert _env_of(_c(docs, "phoenix", 1))["PHOENIX_ADMIN_SECRET"] == _ref("phoenix-admin-secret")
    cfg = by_kind(docs, "ConfigMap", "phoenix-collector")["data"]["config.yaml"]
    assert 'authorization: "Bearer ${env:PHOENIX_ADMIN_SECRET}"' in cfg


@needs_helm
def test_jaeger_query_listens_on_loopback_behind_the_authenticated_front():
    rc, err, docs = template("otel.collector=jaeger")
    assert rc == 0, err
    args = _c(docs, "jaeger")["args"]
    assert "--query.http-server.host-port=127.0.0.1:16687" in args
    assert "--query.grpc-server.host-port=127.0.0.1:16685" in args
    assert not any(a.startswith("--query.http.tls") for a in args)  # plaintext on loopback; the front is TLS
    front = _c(docs, "jaeger", 1)
    assert front["command"][:3] == ["python", "-m", "ramen_console.tlsproxy"]
    assert "0.0.0.0:16686" in front["command"] and "127.0.0.1:16687" in front["command"]
    assert _env_of(front)["RAMEN_PROXY_PASSWORD"] == _ref("password")
    assert front["readinessProbe"] == {"tcpSocket": {"port": "query"}, "periodSeconds": 5}
    ports = {p["name"]: p["targetPort"] for p in by_kind(docs, "Service", "jaeger")["spec"]["ports"]}
    assert ports == {"otlp-http": "otlp-http", "query": "query"}


@needs_helm
def test_promptfoos_front_holds_both_credentials_and_the_upload_routes():
    rc, err, docs = template("evals.engine=promptfoo")
    assert rc == 0, err
    front = _c(docs, "promptfoo", 1)
    env = _env_of(front)
    assert env["RAMEN_PROXY_PASSWORD"] == _ref("password") and env["RAMEN_PROXY_UPLOAD_PASSWORD"] == _ref(
        "upload-password"
    )
    cmd = front["command"]
    routes = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--upload-route"]
    assert routes == ["POST ^/api/eval$", "POST ^/api/eval/[A-Za-z0-9:_-]+/results$"]
    assert "tcpSocket" in front["readinessProbe"]


@needs_helm
@pytest.mark.parametrize("collector", ["phoenix", "jaeger"])
def test_workers_reach_no_engine_ui_or_api(collector):
    rc, err, docs = template(f"otel.collector={collector}", "evals.engine=promptfoo")
    assert rc == 0, err
    ui = 6006 if collector == "phoenix" else 16686

    def from_workers(rule):
        return any("namespaceSelector" in f and "ramen.io/group" in str(f) for f in rule["from"])

    rules = by_kind(docs, "NetworkPolicy", collector)["spec"]["ingress"]
    for r in rules:
        ports = [p["port"] for p in r["ports"]]  # no rule admits every port any more
        if from_workers(r):
            assert ports == [4318], "workers may only post OTLP"
        else:
            assert ports in ([4318], [ui])
    pf = by_kind(docs, "NetworkPolicy", "promptfoo")["spec"]["ingress"]
    assert not any(from_workers(r) for r in pf)
