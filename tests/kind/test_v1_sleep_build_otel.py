"""CONTRACTS §24 (v1.0.0) on the two-zone kind cluster: sleeping workers, an image built on deploy, OpenTelemetry
with consent — through the zone's own edge port, against the same Kubernetes code paths GKE runs.

The public demo repo's `requirements.txt` holds comments only, so this module serves its own group repo from a
`git daemon` pod (the console image, namespace `ramen-tests`): the demo fixture plus one real requirement (`six`), a
tool that imports it, a task tool and a tool that asks a question. The demo group is pointed at it for the module and
put back afterwards. Three deploys in all; the waits are the feature (a zone sleeps after 60 idle seconds at least).

Order matters and is the file's order: build → telemetry → consent → (consent off) → what keeps a zone awake →
sleep on idle → wake by a call."""

import base64
import hashlib
import io
import json
import secrets
import shutil
import subprocess
import tarfile
import time
from datetime import datetime
from urllib.parse import parse_qs, urlparse

import grpc
import httpx
import pytest

from ramen_tests import env as E
from ramen_tests import kube
from ramen_tests.localconsole import REDIRECT, pkce
from ramen_tests.mcp_client import HttpNode, JsonRpcError, Node, text_of
from ramen_tests.modern import ALL_INPUT, WITH_TASKS, Modern
from ramen_tests.tokens import claims_of

from .conftest import audit_entries

pytestmark = pytest.mark.kind
NS_TESTS = "ramen-tests"
REPO_URL = f"git://gitrepo.{NS_TESTS}.svc.cluster.local/repo"
REDIS_URL = f"redis://redis.{NS_TESTS}.svc.cluster.local:6379"
REDIS = """
apiVersion: apps/v1
kind: Deployment
metadata: {{name: redis, namespace: {ns}}}
spec:
  replicas: 1
  selector: {{matchLabels: {{app: redis}}}}
  template:
    metadata: {{labels: {{app: redis}}}}
    spec: {{containers: [{{name: redis, image: "redis:7-alpine", ports: [{{containerPort: 6379}}]}}]}}
---
apiVersion: v1
kind: Service
metadata: {{name: redis, namespace: {ns}}}
spec: {{selector: {{app: redis}}, ports: [{{port: 6379}}]}}
"""
REQUIREMENTS = "# the kind build proof: one small pure-Python package\nsix==1.17.0\n"
OTLP = "http://jaeger.ramen-system:4318"
CONSENT = "ramen_telemetry_consent"
ACCEPT = {CONSENT: {"action": "accept", "content": {"accept": True}}}
SLEEP_AFTER = 60
WAKE_TIMEOUT = 90  # RAMEN_WAKE_TIMEOUT_SECS default (§24.1)
PING = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
SIX_JSON = {
    "type": "tool",
    "name": "six_version_tool",
    "description": "Answer with the version of the `six` package this worker imports.",
    "callable": "run",
    "input": {},
}
SIX_PY = "import six\n\n\ndef run():\n    return six.__version__\n"
GITREPO = """
apiVersion: v1
kind: Namespace
metadata: {{ name: {ns} }}
---
apiVersion: apps/v1
kind: Deployment
metadata: {{ name: gitrepo, namespace: {ns} }}
spec:
  replicas: 1
  selector: {{ matchLabels: {{ app: gitrepo }} }}
  template:
    metadata: {{ labels: {{ app: gitrepo }}, annotations: {{ ramen.test/content: "{digest}" }} }}
    spec:
      containers:
        - name: git
          image: {image}
          imagePullPolicy: IfNotPresent
          command: ["sh", "-c"]
          args:
            - |
              set -e
              mkdir -p /tmp/srv/repo && cd /tmp/srv/repo
              tar xzf /src/repo.tgz
              git init -q -b main .
              git -c user.name=tests -c user.email=tests@ramen.test add -A
              git -c user.name=tests -c user.email=tests@ramen.test commit -q -m "kind fixture"
              exec git daemon --export-all --base-path=/tmp/srv --reuseaddr --listen=0.0.0.0 --port=9418 /tmp/srv
          ports: [{{ containerPort: 9418 }}]
          readinessProbe: {{ tcpSocket: {{ port: 9418 }}, periodSeconds: 2 }}
          resources: {{ requests: {{ cpu: 10m, memory: 32Mi }} }}
          volumeMounts: [{{ name: src, mountPath: /src }}]
      volumes: [{{ name: src, configMap: {{ name: gitrepo-src }} }}]
---
apiVersion: v1
kind: Service
metadata: {{ name: gitrepo, namespace: {ns} }}
spec:
  selector: {{ app: gitrepo }}
  ports: [{{ port: 9418, targetPort: 9418 }}]
"""


# --- the group repo this module deploys ------------------------------------------------------------------------------
def repo_tarball(root) -> bytes:
    src = root / "repo"
    ignore = shutil.ignore_patterns("__pycache__", ".ramen_requirements.sha256")
    shutil.copytree(E.FIXTURES / "demo_group", src, ignore=ignore)
    for tool in ("slow_job", "ask_name", "plain_tool"):
        shutil.copytree(E.FIXTURES / "mrtr_group" / "mcp" / "tools" / tool, src / "mcp" / "tools" / tool, ignore=ignore)
    six = src / "mcp" / "tools" / "six_version_tool"
    six.mkdir()
    (six / "six_version_tool.json").write_text(json.dumps(SIX_JSON))
    (six / "six_version_tool.py").write_text(SIX_PY)
    (src / "mcp" / "requirements.txt").write_text(REQUIREMENTS)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for path in sorted(src.rglob("*")):
            tar.add(path, arcname=str(path.relative_to(src)), recursive=False)
    return buf.getvalue()


def console_env(name: str) -> str | None:
    c = kube.deployment("ramen-system", "console")["spec"]["template"]["spec"]["containers"][0]
    return next((e.get("value") for e in c.get("env", []) if e["name"] == name), None)


def serve_repo(root) -> None:
    data = repo_tarball(root)
    image = kube.deployment("ramen-system", "console")["spec"]["template"]["spec"]["containers"][0]["image"]
    cm = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": "gitrepo-src", "namespace": NS_TESTS},
        "binaryData": {"repo.tgz": base64.b64encode(data).decode()},
    }
    kube.apply(json.dumps({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": NS_TESTS}}))
    kube.apply(json.dumps(cm))
    kube.apply(GITREPO.format(ns=NS_TESTS, image=image, digest=hashlib.sha256(data).hexdigest()[:16]))
    kube.write("-n", NS_TESTS, "rollout", "status", "deployment/gitrepo", "--timeout=180s", timeout=200)
    # the group's Redis: with it a task is found from either pod of a zone (stable and canary) and a consent is
    # remembered across them; without it a pod hands out no tasks (§23.5)
    kube.apply(REDIS.format(ns=NS_TESTS))
    kube.write("-n", NS_TESTS, "rollout", "status", "deployment/redis", "--timeout=180s", timeout=200)


def start_deploy(admin, group, env_name):
    """This module swaps the group's repo for its own and back, so every deploy here replaces the whole tool set:
    the compatibility gate (§19) rightly calls that breaking, and the module accepts it."""
    return admin.post("deploy", {"canary": True, "breaking": True}, group=group, env=env_name)


def deploy(admin, group, env_name) -> dict:
    job = admin.wait_job(start_deploy(admin, group, env_name).json()["id"], timeout=1500)
    assert job["status"] == "ok", f"deploy failed: {job.get('error')}\n" + "\n".join(job.get("log", [])[-60:])
    return job


def put_ok(admin, route: str, body: dict, **kw) -> dict:
    r = admin.put(route, body, **kw)
    assert r.status_code == 200, f"PUT {route} {body}: {r.status_code} {r.text[:300]}"
    return r.json()


@pytest.fixture(scope="module")
def v1(admin, stack, kube_ready, group, env_name, tmp_path_factory):
    """Telemetry on, the group on this module's repo with `build_image`, consent required; one deploy. Restored (repo,
    consent, telemetry, sleeping, scheduler interval) and redeployed when the module ends."""
    before_group = admin.get("group", group=group).json()
    before_sched = admin.get("config_scheduler").json()
    serve_repo(tmp_path_factory.mktemp("gitrepo"))
    put_ok(admin, "config_otel", {"enabled": True, "endpoint": OTLP})
    put_ok(admin, "group", {"repo_url": REPO_URL, "ref": "main"}, group=group)
    put_ok(admin, "group", {"build_image": True}, group=group)
    r = admin.http.put(  # base_url and the csrf hook are the client's own
        f"/api/v1/groups/{group}/throttle",
        json={"redis_url": REDIS_URL, "ip_per_min": 100000, "token_per_min": 100000},
    )
    assert r.status_code == 200, f"group throttle (Redis): {r.status_code} {r.text[:300]}"
    put_ok(admin, "group", {"require_telemetry_consent": True}, group=group)
    put_ok(admin, "config_scheduler", {"interval_seconds": 10})
    keys = {}  # consent is remembered per key: one per consent case, minted before the deploy that hands them out
    for name in ("prompt-http", "prompt-grpc", "rounds", "refused"):
        r = admin.create_mcp_key(group, f"v1-{name}-{secrets.token_hex(3)}")
        assert r.status_code == 201, r.text
        keys[name] = r.json()["key"]
    images_before = images(admin, group)
    world = {**stack, "keys": keys, "images_before": images_before, "job": deploy(admin, group, env_name)}
    yield world
    for zone in stack["zones"]:
        admin.put("workers", {"sleep_after_secs": 0}, group=group, zone=zone)
        admin.post("zone_wake", group=group, zone=zone)
    admin.put("group", {"require_telemetry_consent": False}, group=group)
    # before the namespace (and the Redis in it) goes: workers handed a Redis that no longer exists would have none
    admin.http.put(f"/api/v1/groups/{group}/throttle", json={"redis_url": ""})
    admin.put("config_otel", {"enabled": False})
    admin.put("config_scheduler", before_sched)
    admin.put("group", {"repo_url": before_group["repo_url"], "ref": before_group.get("ref", "main")}, group=group)
    admin.wait_job(start_deploy(admin, group, env_name).json()["id"], timeout=1500)
    kube.write("delete", "namespace", NS_TESTS, "--ignore-not-found", "--wait=false")


def images(admin, group) -> list[dict]:
    r = admin.get("images", group=group)
    assert r.status_code == 200, r.text
    body = r.json()
    return body if isinstance(body, list) else body.get("items", body.get("images", []))


def built(admin, group) -> list[dict]:
    return [i for i in images(admin, group) if i.get("source") == "build"]


def edge(zone_targets, zone: str) -> str:
    return zone_targets[zone]


def http_node(zone_targets, zone, key, group, timeout: float = 30) -> HttpNode:
    return HttpNode(E.node_http_url(edge(zone_targets, zone)), key, group=group, zone=zone, timeout=timeout)


def call(m: Modern, tool: str, arguments: dict | None = None, **kw) -> dict:
    """`tools/call`, accepting the telemetry notice when a pod that has not seen this key's consent asks for it (the
    kind stack has no scope Redis: a consent is remembered per pod)."""
    arguments = arguments or {}
    first = m.call(tool, arguments, **kw)
    if first.get("resultType") == "input_required" and CONSENT in first.get("inputRequests", {}):
        return m.call(tool, arguments, inputResponses=ACCEPT, requestState=first["requestState"], **kw)
    return first


def worker_pods(ns: str) -> list[dict]:
    return kube.pods(ns, "app=worker,ramen.io/role=worker")


def activator_pods(ns: str) -> list[dict]:
    return kube.pods(ns, "app=worker,ramen.io/role=activator")


def ready(pod: dict) -> bool:
    return any(c["type"] == "Ready" and c["status"] == "True" for c in (pod.get("status") or {}).get("conditions", []))


def wait_for(fn, timeout: float, what: str, every: float = 3):
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        last = fn()
        if last:
            return last
        time.sleep(every)
    raise AssertionError(f"{what}: not within {timeout:.0f}s (last={last!r})")


def zone_state(admin, group, zone) -> str:
    return admin.get("workers", group=group, zone=zone).json().get("state", "awake")


def audited(admin, action: str, zone: str, trigger: str, since: float) -> list[dict]:
    out = []
    for e in audit_entries(admin):
        tags = e.get("tags", [])
        if e.get("action") != action or f"zone:{zone}" not in tags or f"trigger:{trigger}" not in tags:
            continue
        try:
            at = datetime.fromisoformat(str(e.get("ts", "")).replace("Z", "+00:00")).timestamp()
        except ValueError:
            at = since
        if at >= since - 2:
            out.append(e)
    return out


# --- §24.2 an image built on deploy -----------------------------------------------------------------------------------
def test_the_deploy_built_the_groups_image_and_recorded_it(admin, v1, group):
    log = "\n".join(v1["job"].get("log", []))
    assert "image: building b-" in log and "image: built " in log, log[-1500:]
    new = [i for i in built(admin, group) if i["id"] not in {x["id"] for x in v1["images_before"]}]
    ok = [i for i in new if i.get("status") == "ok"]
    assert len(ok) == 1, f"one build for this deploy (both zones share it): {new}"
    image = ok[0]
    base, registry = console_env("RAMEN_IMAGE_WORKER"), console_env("RAMEN_BUILD_REGISTRY")
    want = hashlib.sha256(base.encode() + b"\n" + REQUIREMENTS.encode()).hexdigest()[:16]
    assert image["tag"] == f"b-{group}-{want}", "tag = b-<group>-<sha256(base image ref + \\n + requirements.txt)[:16]>"
    assert image["ref"] == f"{registry}/worker:{image['tag']}", (image["ref"], registry)
    v1["image"] = image


def test_the_image_is_in_the_kind_registry(v1):
    if "image" not in v1:
        pytest.skip("no built image recorded (see the test above)")
    registry = E.env("RAMEN_KIND_REGISTRY", "http://localhost:5001")
    repo = v1["image"]["ref"].split("/", 1)[1].rsplit(":", 1)[0]  # ramen/worker
    r = httpx.get(f"{registry}/v2/{repo}/tags/list", timeout=30)
    assert r.status_code == 200 and v1["image"]["tag"] in (r.json().get("tags") or []), (r.status_code, r.text[:300])


def test_the_build_job_and_its_context_are_cleaned_up(v1):
    jobs = [j["metadata"]["name"] for j in kube.get("job", ns="ramen-system").get("items", [])]
    running = [j for j in jobs if j.startswith("ramen-build-")]
    cms = [c["metadata"]["name"] for c in kube.get("configmap", ns="ramen-system").get("items", [])]
    assert not [c for c in cms if c.startswith("ramen-build-")], f"build context ConfigMaps left behind: {cms}"
    assert not running, f"build Jobs left behind (deleted afterwards, TTL 600 s): {running}"


def test_worker_pods_run_the_built_image_and_say_deps_baked(v1, group):
    if "image" not in v1:
        pytest.skip("no built image recorded")
    for zone in v1["zones"]:
        ns = kube.ns_name(group, zone)
        pods = wait_for(lambda ns=ns: [p for p in worker_pods(ns) if ready(p)], 180, f"{ns}: a Ready worker pod")
        for pod in pods:
            images = [c["image"] for c in pod["spec"]["containers"]]
            assert v1["image"]["ref"] in images, f"{ns}/{pod['metadata']['name']} runs {images}"
            text = kube.logs(ns, pod["metadata"]["name"])
            assert "deps baked" in text, f"{pod['metadata']['name']}: no `deps baked` in its log"
            assert '"pip install"' not in text, "a baked pod ran pip install"


def test_the_baked_package_is_importable_through_the_edge(v1, zone_targets, group):
    for zone in v1["zones"]:
        with http_node(zone_targets, zone, v1["key"], group) as h:
            assert text_of(call(Modern(h, ALL_INPUT), "six_version_tool")) == "1.17.0", zone


# --- §24.3 spans in Jaeger --------------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def jaeger():
    """The chart's Jaeger query API: the kind NodePort, else `RAMEN_JAEGER_URL`, else a port-forward."""
    url = E.env("RAMEN_JAEGER_URL", "http://localhost:18086")
    try:
        if httpx.get(f"{url}/api/services", timeout=5).status_code == 200:
            yield url
            return
    except httpx.HTTPError:
        pass
    port = 16686 + secrets.randbelow(2000)
    cmd = ["kubectl", "--context", kube.kind_context(), "-n", "ramen-system", "port-forward", "svc/jaeger"]
    proc = subprocess.Popen([*cmd, f"{port}:16686"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    try:
        url = f"http://127.0.0.1:{port}"
        wait_for(lambda: _up(url), 30, "jaeger port-forward", every=1)
        yield url
    finally:
        proc.terminate()


def _up(url: str) -> bool:
    try:
        return httpx.get(f"{url}/api/services", timeout=3).status_code == 200
    except httpx.HTTPError:
        return False


def tag(span: dict, key: str):
    return next((t["value"] for t in span.get("tags", []) if t["key"] == key), None)


def test_jaeger_holds_the_node_span_of_a_call_with_its_runtime_child(v1, jaeger, zone_targets, group):
    zone = v1["zones"][0]
    trace_id, parent = secrets.token_hex(16), secrets.token_hex(8)
    with http_node(zone_targets, zone, v1["key"], group) as h:
        m = Modern(h, ALL_INPUT)
        call(m, "plain_tool", {"text": "warm"})  # consent for this pod first, so the traced call is the tool's
        got = m.call_raw("plain_tool", {"text": "traced"}, headers={"traceparent": f"00-{trace_id}-{parent}-01"})
        assert got.result and got.result.get("resultType") in ("complete", "input_required"), got.message

    def fetch():
        r = httpx.get(f"{jaeger}/api/traces/{trace_id}", timeout=10)
        data = r.json().get("data") if r.status_code == 200 else None
        return data[0] if data and len(data[0].get("spans", [])) >= 1 else None

    found = wait_for(fetch, 60, f"trace {trace_id} in Jaeger")
    services = {p["serviceName"] for p in found["processes"].values()}
    assert services == {"ramen-node"}, services
    server = [s for s in found["spans"] if s["operationName"] == "mcp tools/call"]
    assert len(server) == 1, [s["operationName"] for s in found["spans"]]
    s = server[0]
    assert (tag(s, "mcp.method"), tag(s, "mcp.name")) == ("tools/call", "plain_tool")
    assert (tag(s, "ramen.group"), tag(s, "ramen.zone"), tag(s, "ramen.transport")) == (group, zone, "http")
    assert tag(s, "span.kind") == "server"
    assert any(r.get("spanID") == parent for r in s.get("references", [])), "the caller's span is the parent"
    if got.result.get("resultType") == "complete":
        children = [x for x in found["spans"] if any(r.get("spanID") == s["spanID"] for r in x.get("references", []))]
        assert [c["operationName"] for c in children if c["operationName"].startswith("runtime ")], found["spans"]
    assert "traced" not in json.dumps(found) and v1["key"] not in json.dumps(found), "an argument or the key in a span"


def test_jaeger_holds_the_consoles_deploy_span(v1, jaeger, group, env_name):
    def fetch():
        r = httpx.get(
            f"{jaeger}/api/traces",
            params={"service": "ramen-console", "operation": "ramen.deploy", "lookback": "1h", "limit": 20},
            timeout=10,
        )
        return (r.json().get("data") or None) if r.status_code == 200 else None

    traces = wait_for(fetch, 60, "a ramen.deploy span from ramen-console")
    spans = [s for t in traces for s in t["spans"] if s["operationName"] == "ramen.deploy"]
    mine = [s for s in spans if tag(s, "ramen.group") == group and tag(s, "ramen.env") == env_name]
    assert mine, [(tag(s, "ramen.group"), tag(s, "ramen.env")) for s in spans]
    last = max(mine, key=lambda s: s["startTime"])
    assert tag(last, "ramen.outcome") == "ok", last["tags"]
    if "image" in v1:
        assert v1["image"]["tag"] in str(tag(last, "ramen.image")), tag(last, "ramen.image")


def test_discover_tells_an_agent_where_to_send_its_telemetry(v1, zone_targets, group):
    zone = v1["zones"][0]
    with http_node(zone_targets, zone, v1["key"], group) as h:
        told = Modern(h).discover()["_meta"].get("io.ramen/telemetry")
    assert told and told["otlp_endpoint"] == OTLP and told["headers"] == {}, told
    assert told["resource_attributes"] == {"ramen.group": group, "ramen.zone": zone}


# --- §24.3 consent through the edge -----------------------------------------------------------------------------------
@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_consent_by_prompt_works_through_the_edge(v1, zone_targets, group, transport):
    zone, fresh_key = v1["zones"][0], v1["keys"][f"prompt-{transport}"]
    if transport == "http":
        client = http_node(zone_targets, zone, fresh_key, group)
    else:
        client = Node(edge(zone_targets, zone), fresh_key, group=group, zone=zone)
    with client:
        m = Modern(client, ALL_INPUT)
        first = m.call("plain_tool", {"text": f"consent-{transport}"})
        assert first["resultType"] == "input_required" and list(first["inputRequests"]) == [CONSENT], first
        message = first["inputRequests"][CONSENT]["params"]["message"]
        assert "jaeger.ramen-system:4318" in message and "never arguments or results" in message, message
        refused = m.call_raw(
            "plain_tool",
            {"text": f"consent-{transport}"},
            inputResponses={CONSENT: {"action": "decline"}},
            requestState=first["requestState"],
        )
        assert refused.code == -32602 and refused.error["message"] == "telemetry consent declined", refused.message
        done = m.call(
            "plain_tool", {"text": f"consent-{transport}"}, inputResponses=ACCEPT, requestState=first["requestState"]
        )
        assert text_of(done) == f"consent-{transport}", done
        assert text_of(m.call("plain_tool", {"text": "again"})) == "again", "remembered on this connection's pod"


def test_a_tool_that_asks_its_own_question_follows_the_consent_round(v1, zone_targets, group):
    zone = v1["zones"][0]
    with http_node(zone_targets, zone, v1["keys"]["rounds"], group) as h:
        m = Modern(h, ALL_INPUT)
        first = m.call("ask_name")
        assert list(first["inputRequests"]) == [CONSENT], first
        second = m.call("ask_name", inputResponses=ACCEPT, requestState=first["requestState"])
        assert second["resultType"] == "input_required" and list(second["inputRequests"]) == ["who"], second
        third = m.call("ask_name", inputResponses={"who": {"action": "accept", "content": {"name": "Kind"}}})
        assert text_of(third) == "Hello, Kind!", third


def test_consent_is_refused_to_clients_that_cannot_be_asked(v1, zone_targets, group):
    zone = v1["zones"][0]
    with http_node(zone_targets, zone, v1["keys"]["refused"], group) as h:
        r = Modern(h, {}).call_raw("plain_tool", {"text": "x"})
        assert r.code == -32021 and r.error["data"] == {"requiredCapabilities": {"elicitation": {}}}, r.message
        assert Modern(h, {}).request("tools/list")["tools"], "lists stay open"
        h.initialize()
        with pytest.raises(JsonRpcError) as e:
            h.call_tool("plain_tool", {"text": "x"})
        assert e.value.code == -32003 and "requires telemetry consent" in e.value.message, e.value
        assert {t["name"] for t in h.list_tools()} >= {"plain_tool"}


def test_consent_by_token_is_never_prompted(v1, admin, console_url, zone_targets, group):
    """The person ticks the box on the console's consent page; the token says `tel: true`; the worker never asks."""
    zone = v1["zones"][0]
    base = console_url.rstrip("/")
    r = admin.http.post(f"{base}/api/v1/oauth/clients", json={"name": "v1-kind", "redirect_uris": [REDIRECT]})
    assert r.status_code == 201, r.text
    cid = r.json()["client_id"]
    verifier, challenge = pkce()
    scope = f"mcp:{group}:{zone}"
    q = {
        "response_type": "code",
        "client_id": cid,
        "redirect_uri": REDIRECT,
        "scope": scope,
        "state": "k1",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": scope,
    }
    page = admin.http.get(f"{base}/oauth/authorize", params=q, follow_redirects=False)
    assert page.status_code == 200 and 'name="telemetry_consent"' in page.text, page.text[:300]
    form = {**q, "decision": "allow", "csrf_token": admin.http.cookies.get("ramen_csrf", "")}
    without = admin.http.post(f"{base}/oauth/authorize", data=form, follow_redirects=False)
    assert "code=" not in without.headers.get("location", ""), "approved without the checkbox"
    r = admin.http.post(f"{base}/oauth/authorize", data={**form, "telemetry_consent": "1"}, follow_redirects=False)
    assert r.status_code == 303, r.text[:300]
    code = parse_qs(urlparse(r.headers["location"]).query)["code"][0]
    tok = admin.http.post(
        f"{base}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "code": code,
            "client_id": cid,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        },
    ).json()
    assert claims_of(tok["access_token"]).get("tel") is True
    assert tok["telemetry"]["otlp_endpoint"] == OTLP, tok.get("telemetry")
    with http_node(zone_targets, zone, tok["access_token"], group) as h:
        if h.status(PING) == 401:
            pytest.skip("the kind workers do not verify console tokens (no RAMEN_OAUTH_ISSUER on this stack)")
        assert text_of(Modern(h, {}).call("plain_tool", {"text": "signed in"})) == "signed in"
        h.initialize()
        assert text_of(h.call_tool("plain_tool", {"text": "legacy"})) == "legacy"


# --- second deploy: consent off, nothing built ------------------------------------------------------------------------
@pytest.fixture(scope="module")
def plain(admin, v1, group, env_name):
    """Consent off for the sleep proofs (their waits must not be disturbed by a prompt), by a second deploy — whose
    requirements did not change."""
    before = built(admin, group)
    put_ok(admin, "group", {"require_telemetry_consent": False}, group=group)
    job = deploy(admin, group, env_name)
    return {**v1, "second_job": job, "built_before": before}


def test_a_second_deploy_without_a_requirements_change_builds_nothing(admin, plain, group):
    log = "\n".join(plain["second_job"].get("log", []))
    assert "image: building" not in log, log[-1200:]
    assert "already built" in log, log[-1200:]
    assert [i["id"] for i in built(admin, group)] == [i["id"] for i in plain["built_before"]], "no new image record"
    if "image" in plain:
        for zone in plain["zones"]:
            ns = kube.ns_name(group, zone)
            pods = wait_for(lambda ns=ns: [p for p in worker_pods(ns) if ready(p)], 180, f"{ns}: a Ready worker pod")
            assert all(plain["image"]["ref"] in [c["image"] for c in p["spec"]["containers"]] for p in pods)


def test_without_the_requirement_nobody_is_asked(plain, zone_targets, group):
    with http_node(zone_targets, plain["zones"][0], plain["key"], group) as h:
        assert text_of(Modern(h, {}).call("plain_tool", {"text": "free"})) == "free"


# --- §24.1 what keeps a zone awake ------------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def sleepy(admin, plain, group):
    """The last zone sleeps after 60 idle seconds; the other keeps the rest of the kind suite's ports answering."""
    zone = plain["zones"][-1]
    ns = kube.ns_name(group, zone)
    wait_for(lambda: [p for p in worker_pods(ns) if ready(p)], 180, f"{ns}: a Ready worker pod")
    w = put_ok(admin, "workers", {"sleep_after_secs": SLEEP_AFTER}, group=group, zone=zone)
    assert w["sleep_after_secs"] == SLEEP_AFTER
    return {**plain, "zone": zone, "ns": ns}


def stays_awake(admin, group, sleepy, seconds: float) -> None:
    """No MCP call for `seconds` (console API reads are not MCP calls): the zone must not sleep meanwhile."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        assert zone_state(admin, group, sleepy["zone"]) == "awake", "the zone went to sleep"
        assert not activator_pods(sleepy["ns"]), "an activator appeared"
        time.sleep(5)
    assert kube.replicas(sleepy["ns"], "worker")[0] >= 1


def test_a_zone_with_an_open_subscription_does_not_sleep(admin, sleepy, zone_targets, group):
    with http_node(zone_targets, sleepy["zone"], sleepy["key"], group) as h:
        with Modern(h).listen({"toolsListChanged": True}) as s:
            assert s.status == 200 and s.next(15) is not None, "the acknowledgement"
            stays_awake(admin, group, sleepy, SLEEP_AFTER + 35)  # past the setting and two scheduler ticks
            assert not s.closed and s.quiet(1), "the stream is still open"


def test_a_zone_with_a_running_task_does_not_sleep(admin, sleepy, zone_targets, group):
    with http_node(zone_targets, sleepy["zone"], sleepy["key"], group) as h:
        m = Modern(h, {**ALL_INPUT, **WITH_TASKS})
        created = m.call("slow_job", {"seconds": SLEEP_AFTER + 45, "label": "kind"})
        assert created["resultType"] == "task" and created["status"] == "working", created
        stays_awake(admin, group, sleepy, SLEEP_AFTER + 35)  # no poll meanwhile: a poll would be a call
        done = m.wait_task(created["taskId"], timeout=60)
        assert done["status"] == "completed" and "job kind done" in text_of(done["result"]), done


# --- §24.1 sleep on idle, wake by a call ------------------------------------------------------------------------------
def test_an_idle_zone_goes_to_sleep(admin, sleepy, zone_targets, group):
    zone, ns = sleepy["zone"], sleepy["ns"]
    sleepy["slept_after"] = began = time.time()
    wait_for(lambda: zone_state(admin, group, zone) == "asleep", SLEEP_AFTER + 180, f"{zone} asleep on idle", every=5)
    assert time.time() - began >= SLEEP_AFTER - 15, "it slept before the setting had passed since the last call"
    acts = wait_for(lambda: [p for p in activator_pods(ns) if ready(p)], 60, "a Ready activator pod")
    assert len(acts) == 1
    spec = acts[0]["spec"]["containers"][0]
    env = {e["name"]: e.get("value") for e in spec.get("env", [])}
    assert env.get("RAMEN_ACTIVATOR") == "1", sorted(env)
    assert spec["resources"]["requests"] == {"cpu": "10m", "memory": "32Mi"}, spec["resources"]
    assert acts[0]["metadata"]["labels"]["ramen.io/role"] == "activator"
    wait_for(lambda: not worker_pods(ns), 120, "the worker pods are gone")
    assert kube.replicas(ns, "worker")[0] == 0
    canary = kube.deployment_or_none(ns, "worker-canary")
    assert canary is None or int(canary["spec"].get("replicas", 0)) == 0, "worker-canary at 0 too"
    assert kube.replicas(ns, "worker-activator") == (1, 1)
    rows = audited(admin, "workers.sleep", zone, "idle", began)
    assert rows and rows[0].get("ok") is True and rows[0].get("user") == "scheduler", audit_entries(admin)[:5]
    w = admin.get("workers", group=group, zone=zone).json()
    assert w["state"] == "asleep" and (w.get("last_sleep") or {}).get("trigger") == "idle", w


def test_the_sleeping_zones_edge_stays_healthy_and_closed_to_strangers(admin, sleepy, zone_targets, group):
    zone = sleepy["zone"]
    with Node(edge(zone_targets, zone), None, group=group, zone=zone) as n:
        assert n.health() == "SERVING", "the edge's health check stays green"
        assert n.outcome(PING) == "UNAUTHENTICATED"
        assert n.outcome(PING, key="rmk_" + secrets.token_hex(12)) == "UNAUTHENTICATED"
    with http_node(zone_targets, zone, None, group) as h:
        assert h.status(PING) == 401
    time.sleep(8)
    assert zone_state(admin, group, zone) == "asleep", "an unauthenticated call woke the zone"
    assert not worker_pods(sleepy["ns"])


def test_the_other_zone_did_not_sleep(admin, sleepy, zone_targets, group):
    other = sleepy["zones"][0]
    assert zone_state(admin, group, other) == "awake"
    with http_node(zone_targets, other, sleepy["key"], group) as h:
        assert text_of(Modern(h).call("plain_tool", {"text": "awake"})) == "awake"


def test_a_call_through_the_edge_wakes_the_zone_and_is_answered(admin, sleepy, zone_targets, group):
    zone, ns = sleepy["zone"], sleepy["ns"]
    began, t0 = time.time(), time.monotonic()
    with http_node(zone_targets, zone, sleepy["key"], group, timeout=WAKE_TIMEOUT + 30) as h:
        result = Modern(h).call("plain_tool", {"text": "good morning"}, timeout=WAKE_TIMEOUT + 30)
    took = time.monotonic() - t0
    assert text_of(result) == "good morning", result
    assert took <= WAKE_TIMEOUT + 5, f"answered within the wake timeout: {took:.0f}s"
    wait_for(lambda: zone_state(admin, group, zone) == "awake", 180, f"{zone} awake")
    assert [p for p in worker_pods(ns) if ready(p)], "a worker pod serves the zone again"
    wait_for(lambda: not activator_pods(ns), 120, "the activator is gone")
    assert kube.replicas(ns, "worker-activator")[0] == 0
    rows = audited(admin, "workers.wake", zone, "call", began)
    assert len(rows) == 1 and rows[0].get("ok") is True, rows
    w = admin.get("workers", group=group, zone=zone).json()
    assert (w.get("last_wake") or {}).get("trigger") == "call", w
    with http_node(zone_targets, zone, sleepy["key"], group) as h:
        assert text_of(Modern(h).call("six_version_tool")) == "1.17.0", "the woken workers run the built image"


def test_sleep_by_hand_and_wake_by_a_grpc_call(admin, sleepy, zone_targets, group):
    zone, ns = sleepy["zone"], sleepy["ns"]
    began = time.time()
    r = admin.post("zone_sleep", group=group, zone=zone)
    assert r.status_code == 200, r.text
    assert zone_state(admin, group, zone) == "asleep"
    wait_for(lambda: not worker_pods(ns), 120, "the worker pods are gone")
    assert audited(admin, "workers.sleep", zone, "manual", began)
    t0 = time.monotonic()
    with Node(edge(zone_targets, zone), sleepy["key"], group=group, zone=zone, timeout=WAKE_TIMEOUT + 30) as n:
        try:
            result = Modern(n).call("plain_tool", {"text": "grpc morning"}, timeout=WAKE_TIMEOUT + 30)
        except grpc.RpcError as e:
            raise AssertionError(f"the waking call failed after {time.monotonic() - t0:.0f}s: {e.code()}") from None
    assert text_of(result) == "grpc morning" and time.monotonic() - t0 <= WAKE_TIMEOUT + 5
    wait_for(lambda: zone_state(admin, group, zone) == "awake", 180, f"{zone} awake")
    wait_for(lambda: not activator_pods(ns), 120, "the activator is gone")
    assert audited(admin, "workers.wake", zone, "call", began)


def test_a_deploy_wakes_a_sleeping_zone_first(admin, sleepy, group, env_name):
    zone, ns = sleepy["zone"], sleepy["ns"]
    began = time.time()
    assert admin.post("zone_sleep", group=group, zone=zone).status_code == 200
    wait_for(lambda: not worker_pods(ns), 120, "the worker pods are gone")
    deploy(admin, group, env_name)
    assert zone_state(admin, group, zone) == "awake"
    assert [p for p in worker_pods(ns) if ready(p)] and not activator_pods(ns)
    assert audited(admin, "workers.wake", zone, "deploy", began)
