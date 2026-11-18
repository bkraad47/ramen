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
import os
import secrets
import shutil
import socket
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
from ramen_tests import telemetry_checks as T
from ramen_tests.localconsole import REDIRECT, pkce
from ramen_tests.mcp_client import TELEMETRY_META, HttpNode, JsonRpcError, Node, text_of
from ramen_tests.modern import ALL_INPUT, WITH_TASKS, Modern
from ramen_tests.tokens import claims_of

from .conftest import audit_entries

pytestmark = pytest.mark.kind
NS_TESTS = "ramen-tests"
REPO_URL = f"git://gitrepo.{NS_TESTS}.svc.cluster.local/repo"
REQUIREMENTS = "# the kind build proof: one small pure-Python package\nsix==1.17.0\n"
OTLP = "https://jaeger.ramen-system:4318"  # D86: the chart's Jaeger takes OTLP over TLS
CONSENT = "ramen_telemetry_consent"
ACCEPT = {CONSENT: {"action": "accept", "content": {"accept": True}}}
TELEMETRY = {"version": None}  # §26.3: the config version every gated call reports, set once telemetry is on
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
    otel = put_ok(admin, "config_otel", {"enabled": True, "endpoint": OTLP})
    TELEMETRY["version"] = otel["telemetry_version"]
    put_ok(admin, "group", {"repo_url": REPO_URL, "ref": "main"}, group=group)
    put_ok(admin, "group", {"build_image": True}, group=group)
    r = admin.http.put(  # base_url and the csrf hook are the client's own
        f"/api/v1/groups/{group}/throttle",
        # §33.4a: no Redis of its own — the chart's (`redis.enabled`, password in Secret ramen-redis) is the zone's
        # store, so a task is found from either pod (stable and canary) and a consent is remembered across them (§23.5)
        json={"redis_url": "", "ip_per_min": 100000, "token_per_min": 100000},
    )
    assert r.status_code == 200, f"group throttle: {r.status_code} {r.text[:300]}"
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
    h = HttpNode(E.node_http_url(edge(zone_targets, zone)), key, group=group, zone=zone, timeout=timeout)
    h.telemetry = TELEMETRY["version"]  # §26.3: every gated call reports
    return h


def grpc_node(zone_targets, zone, key, group, **kw) -> Node:
    n = Node(edge(zone_targets, zone), key, group=group, zone=zone, **kw)
    n.telemetry = TELEMETRY["version"]
    return n


call = T.consenting  # `tools/call`, accepting the telemetry notice when the answering pod has no record of the caller


def worker_pods(ns: str) -> list[dict]:
    return kube.pods(ns, "app=worker,ramen.io/role=worker")


def draining_worker_pods(ns: str) -> list[str]:
    """Worker pods still terminating (`kube.pods` leaves them out): they serve until their drain ends."""
    out = json.loads(kube.kubectl("get", "pods", "-n", ns, "-l", "app=worker,ramen.io/role=worker", "-o", "json"))
    return [p["metadata"]["name"] for p in out.get("items", []) if p["metadata"].get("deletionTimestamp")]


def activator_pods(ns: str) -> list[dict]:
    return kube.pods(ns, "app=worker,ramen.io/role=activator")


def settled(group, zones) -> None:
    """Every track a config push rolled is done — the stable one and, when it runs, the canary (P4-B1: the push rolls
    both) — and no old pod is still draining. The kind NodePort spans both tracks, so until then a fresh connection
    can still land on a pod that runs the old Secret."""
    for z in zones:
        ns = kube.ns_name(group, z)
        canary = kube.deployment_or_none(ns, "worker-canary")
        names = ["worker"] + (["worker-canary"] if canary and int(canary["spec"].get("replicas", 0)) > 0 else [])
        for name in names:
            kube.kubectl("-n", ns, "rollout", "status", f"deployment/{name}", "--timeout=300s", timeout=320)
        wait_for(lambda ns=ns: not draining_worker_pods(ns), 120, f"{ns}: the old worker pods have drained")


def edge_diagnosis(zone_targets, zone: str, ns: str) -> str:
    """What answers the zone's edge right now and what stands behind it, for a transport error there (1.0.1 kind run:
    `[SSL: WRONG_VERSION_NUMBER]` from the TLS-only NodePort). Diagnostics only; nothing is retried."""
    out = []
    host, _, port = edge(zone_targets, zone).rpartition(":")
    for _ in range(4):  # fresh connections: kube-proxy picks a pod for each
        try:
            with socket.create_connection((host, int(port)), timeout=5) as s:
                s.settimeout(5)
                s.sendall(b"GET / HTTP/1.0\r\n\r\n")  # a TLS server refuses this; a plaintext one answers HTTP
                out.append(f"plain probe answered {s.recv(64)!r}")
        except OSError as e:
            out.append(f"plain probe: {e!r}")
    try:
        for p in json.loads(kube.kubectl("get", "pods", "-n", ns, "-o", "json")).get("items", []):
            md, st = p["metadata"], p.get("status") or {}
            labels, deleting = md.get("labels") or {}, bool(md.get("deletionTimestamp"))
            out.append(
                f"pod {md['name']} role={labels.get('ramen.io/role')} track={labels.get('ramen.io/track')} "
                f"ip={st.get('podIP')} phase={st.get('phase')} ready={ready(p)} deleting={deleting}"
            )
        out.append("endpoints worker-host " + kube.kubectl("get", "endpoints", "worker-host", "-n", ns, "-o", "json"))
    except Exception as e:  # noqa: BLE001 - diagnostics only
        out.append(f"({e})")
    return "\n".join(out) + "\n" + wake_logs(ns)


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


def awake_or_explain(admin, group, zone, ns, timeout: float = 300) -> None:
    """The zone reads `awake` within `timeout` (the console's own stale-wake limit, sleeping.WAKE_STALE): the wake
    finishes once every worker and the canary are Ready. On failure: the states seen, the pods, the worker record."""
    seen: list[str] = []

    def awake() -> bool:
        seen.append(zone_state(admin, group, zone))
        return seen[-1] == "awake"

    try:
        wait_for(awake, timeout, f"{zone} awake")
    except AssertionError as e:
        pods = [
            (p["metadata"]["name"], (p.get("status") or {}).get("phase"), ready(p)) for p in kube.pods(ns, "app=worker")
        ]
        w = admin.get("workers", group=group, zone=zone).json()
        raise AssertionError(f"{e}; states {sorted(set(seen))}, last {seen[-1:]}; pods {pods}; workers {w}") from None


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
    base, registry = console_env("RAMEN_IMAGE_WORKER"), console_env("RAMEN_BUILD_REGISTRY")
    want = hashlib.sha256(base.encode() + b"\n" + REQUIREMENTS.encode()).hexdigest()[:16]
    repo = f"{registry}/groups/{group}/worker"  # §38.2: the group's own repository; older records are rebuilt there
    known = [
        i
        for i in v1["images_before"]
        if i["tag"] == f"b-{group}-{want}" and i.get("status") == "ok" and i["ref"].startswith(repo)
    ]
    if known:  # the module ran on this cluster before: the same requirements mean nothing is built, and the log says so
        assert "already built" in log and "image: building" not in log, log[-1500:]
        image = known[0]
    else:
        assert "image: building b-" in log and "image: built " in log, log[-1500:]
        new = [i for i in built(admin, group) if i["id"] not in {x["id"] for x in v1["images_before"]}]
        ok = [i for i in new if i.get("status") == "ok"]
        assert len(ok) == 1, f"one build for this deploy (both zones share it): {new}"
        image = ok[0]
    assert image["tag"] == f"b-{group}-{want}", "tag = b-<group>-<sha256(base image ref + \\n + requirements.txt)[:16]>"
    assert (image.get("digest") or "").startswith("sha256:"), image  # kaniko's --digest-file (§38.2)
    assert image["ref"] == f"{repo}@{image['digest']}", (image["ref"], registry)  # deploys pin the digest
    v1["image"] = image


def test_the_image_is_in_the_kind_registry(v1):
    if "image" not in v1:
        pytest.skip("no built image recorded (see the test above)")
    registry = E.env("RAMEN_KIND_REGISTRY", "http://localhost:5001")
    repo = v1["image"]["ref"].split("/", 1)[1].split("@")[0]  # ramen/groups/<group>/worker
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
    """The chart's Jaeger query API: the kind NodePort, else `RAMEN_JAEGER_URL`, else a port-forward. D92: behind its
    TLS front, with the operator's credential (`RAMEN_ENGINES_PASSWORD`, else read from Secret ramen-engines)."""
    if not E.engines_auth():
        out = subprocess.run(  # noqa: S603
            [
                "kubectl",
                "--context",
                kube.kind_context(),
                "-n",
                "ramen-system",
                "get",
                "secret",
                "ramen-engines",
                "-o",
                "jsonpath={.data.password}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if out.returncode == 0 and out.stdout:
            os.environ["RAMEN_ENGINES_PASSWORD"] = base64.b64decode(out.stdout).decode()
    url = E.env("RAMEN_JAEGER_URL", "https://localhost:18086")
    try:
        if (
            httpx.get(f"{url}/api/services", timeout=5, verify=E.console_verify(), auth=E.engines_auth()).status_code
            == 200
        ):
            yield url
            return
    except httpx.HTTPError:
        pass
    port = 16686 + secrets.randbelow(2000)
    cmd = ["kubectl", "--context", kube.kind_context(), "-n", "ramen-system", "port-forward", "svc/jaeger"]
    proc = subprocess.Popen([*cmd, f"{port}:16686"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # noqa: S603
    try:
        url = f"https://127.0.0.1:{port}"
        wait_for(lambda: _up(url), 30, "jaeger port-forward", every=1)
        yield url
    finally:
        proc.terminate()


def _up(url: str) -> bool:
    try:
        return (
            httpx.get(f"{url}/api/services", timeout=3, verify=E.console_verify(), auth=E.engines_auth()).status_code
            == 200
        )
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
        r = httpx.get(f"{jaeger}/api/traces/{trace_id}", timeout=10, verify=E.console_verify(), auth=E.engines_auth())
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
            verify=E.console_verify(),
            auth=E.engines_auth(),
        )
        return (r.json().get("data") or None) if r.status_code == 200 else None

    traces = wait_for(fetch, 60, "a ramen.deploy span from ramen-console")
    spans = [s for t in traces for s in t["spans"] if s["operationName"] == "ramen.deploy"]
    mine = [s for s in spans if tag(s, "ramen.group") == group and tag(s, "ramen.env") == env_name]
    assert mine, [(tag(s, "ramen.group"), tag(s, "ramen.env")) for s in spans]
    last = max(mine, key=lambda s: s["startTime"])
    assert tag(last, "ramen.outcome") == "ok", last["tags"]
    if "image" in v1:
        ran = str(tag(last, "ramen.image"))  # §38.2: the deploy pins the digest, so the span names repo@sha256:…
        assert ran == v1["image"]["ref"] or v1["image"]["tag"] in ran, (ran, v1["image"])


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
        client = grpc_node(zone_targets, zone, fresh_key, group)
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


# --- §26.3 the report on every call, through the edge ----------------------------------------------------------------
@pytest.mark.parametrize("transport", ["http", "grpc"])
def test_a_gated_call_needs_the_current_report(v1, zone_targets, group, transport):
    zone = v1["zones"][0]
    client = (
        http_node(zone_targets, zone, v1["key"], group)
        if transport == "http"
        else grpc_node(zone_targets, zone, v1["key"], group)
    )
    with client:
        m = Modern(client, ALL_INPUT)
        T.check_gate(m, TELEMETRY["version"], "plain_tool", {"text": "gate"})
        T.check_report_method(m, TELEMETRY["version"])


def test_a_legacy_body_carrying_the_report_is_served(v1, zone_targets, group):
    """The bridge in front of a 2025-06-18 host: the report is what matters, not the era."""
    with http_node(zone_targets, v1["zones"][0], v1["key"], group) as h:
        call(Modern(h, ALL_INPUT), "plain_tool", {"text": "consented"})
        h.initialize()
        assert text_of(h.call_tool("plain_tool", {"text": "legacy"})) == "legacy"
        h.telemetry = None
        with pytest.raises(JsonRpcError) as e:
            h.call_tool("plain_tool", {"text": "legacy"})
        assert e.value.code == -32003 and "requires telemetry; use a client that reports it" in e.value.message


def test_the_client_events_are_client_spans_in_jaeger(v1, jaeger, zone_targets, group):
    zone, reasoning = v1["zones"][0], "the user asked for the version of six"
    with http_node(zone_targets, zone, v1["key"], group) as h:
        trace_id, _ = T.call_with_events(
            Modern(h, ALL_INPUT), "plain_tool", {"text": "spans"}, TELEMETRY["version"], reasoning
        )
    found = T.jaeger_trace(jaeger, trace_id)
    T.check_client_spans(found, group, zone, reasoning)
    assert "spans" not in json.dumps([s["tags"] for s in found["spans"]]), "an argument in a span"


def told_version(zone_targets, zone, key, group) -> str | None:
    """What `server/discover` names as the telemetry version, on a fresh connection each time: a NodePort pins a
    keep-alive connection to one pod, and the old pod answers while it drains (20 s) after the push."""
    try:
        with http_node(zone_targets, zone, key, group, timeout=10) as h:
            told = Modern(h).discover()["_meta"].get(TELEMETRY_META) or {}
            return told.get("version") if isinstance(told, dict) else None
    except Exception:  # noqa: BLE001 - the pod may be mid-restart
        return None


def test_saving_the_telemetry_config_pushes_the_version_to_every_zone_without_a_deploy(admin, v1, zone_targets, group):
    before = TELEMETRY["version"]
    zone = v1["zones"][0]
    r = admin.put("config_otel", {"enabled": True, "endpoint": OTLP, "client_headers": {"x-ramen-kind": "push"}})
    assert r.status_code == 200, r.text
    body = r.json()
    after = body["telemetry_version"]
    assert after != before and body["pushed"] == {f"{group}/{z}": "ok" for z in v1["zones"]}, body
    rows = [e for e in audit_entries(admin) if e.get("action") == "telemetry.push"]
    assert rows and rows[0].get("ok") is True and f"version:{after}" in rows[0].get("tags", []), rows[:1]
    try:
        # on Kubernetes the push rewrites the Secret and rolls every track that runs; the NodePort spans both, so the
        # old canary (or a draining pod) still answered with the old version after the stable roll alone (1.0.1 kind)
        settled(group, v1["zones"])
        wait_for(lambda: told_version(zone_targets, zone, v1["key"], group) == after, 300, f"discover names {after}")
        with http_node(zone_targets, zone, v1["key"], group) as h:
            m = Modern(h, ALL_INPUT)
            r = T.gated_without_report(m, "plain_tool", {"text": "old"}, before)
            assert r.code == -32025 and r.error["data"] == {"version": after}, r.message
            assert text_of(T.gated_without_report(m, "plain_tool", {"text": "new"}, after).result) == "new"
    finally:
        r = admin.put("config_otel", {"enabled": True, "endpoint": OTLP, "client_headers": {}})
        assert r.status_code == 200 and r.json()["telemetry_version"] == before, r.text
        settled(group, v1["zones"])  # the tests after this one report `before`: no pod may still hold `after`
        wait_for(lambda: told_version(zone_targets, zone, v1["key"], group) == before, 300, f"discover names {before}")


# --- §27 zones attached through their environments (groups derived), §26.2 the job in the store ----------------------
def test_a_zones_groups_are_derived_from_its_environments(admin, v1, console_url, group, env_name):
    r = admin.get("group_zones", group=group)
    assert r.status_code == 200 and set(r.json()) == {"zones"} and set(r.json()["zones"]) >= set(v1["zones"]), r.text
    assert admin.put("group_zones", {"zones": []}, group=group).status_code in (404, 405), "PUT on it is gone"
    unbound = f"unb{secrets.token_hex(2)}"
    r = admin.http.post(
        "/api/v1/zones", json={"name": unbound, "provider": "gcp", "region": "us-central1-a", "groups": [group]}
    )
    assert r.status_code == 422, f"`groups` on POST /zones is gone (§27.2): {r.status_code} {r.text[:200]}"
    r = admin.create_zone(unbound, provider="gcp", region="us-central1-a")
    assert r.status_code == 201 and r.json()["groups"] == [] and r.json()["environments"] == [], r.text
    assert admin.http.put(f"/api/v1/zones/{unbound}/groups", json={"groups": [group]}).status_code == 404
    pw = secrets.token_hex(8) + "Aa1!"
    email = f"gadmin-{secrets.token_hex(3)}@ramen.test"
    r = admin.http.post(
        f"{console_url.rstrip('/')}/api/v1/users",
        json={"email": email, "password": pw, "memberships": {group: "group_admin"}},
    )
    assert r.status_code == 201, r.text
    uid = r.json()["id"]
    gadmin = type(admin)(console_url)
    assert gadmin.login(email, pw).status_code in (200, 303)
    try:
        # §27.1 D65: a zone with none of their groups is not theirs to edit — by any route that would attach to it
        r = gadmin.create_environment(group, f"e{unbound}", [unbound], wait=False)
        assert r.status_code == 403 and unbound in r.text, f"{r.status_code} {r.text[:300]}"
        r = gadmin.put("environment", {"zones": [*v1["zones"], unbound]}, group=group, env=env_name)
        assert r.status_code == 403 and unbound in r.text, f"{r.status_code} {r.text[:300]}"
        assert gadmin.update_zone(unbound, environments=[env_name]).status_code == 403
        assert admin.get("workers", group=group, zone=unbound).status_code == 404, "no worker for an unbound pair"
        assert admin.update_zone(unbound, environments=["no-such-env"]).status_code == 422
        r = admin.update_zone(unbound, environments=[env_name])  # attach: bound + deployed (§27.3)
        if r.status_code == 502 and E.no_cloud("iam") and "credential" in r.text.lower():
            pytest.skip(f"no cloud IAM here: attaching a zone to a group needs one ({r.text[:120]})")
        assert r.status_code == 200 and r.json()["groups"] == [group] and "jobs" in r.json(), r.text
        assert unbound in admin.get("group_zones", group=group).json()["zones"]
        assert admin.get("workers", group=group, zone=unbound).status_code == 200, "attaching created the worker record"
        assert gadmin.get("group_zones", group=group).status_code == 200, "viewers of the group read it"
        jobs = admin.wait_jobs(r.json()["jobs"], timeout=900)
        assert jobs and all(j["kind"] == "deploy" and j["target"] == f"{group}/{env_name}" for j in jobs), jobs
        assert gadmin.update_zone(unbound, environments=[]).status_code == 200, "now its groups are all theirs"
        assert admin.get("workers", group=group, zone=unbound).status_code == 404, "detaching removed the record"
        acts = [
            e
            for e in audit_entries(admin)
            if e.get("action") == "zone.update" and f"zone:{unbound}" in e.get("tags", [])
        ]
        assert acts and any(f"env:{env_name}" in e.get("tags", []) for e in acts), acts[:2]
    finally:
        gadmin.close()
        admin.delete("user", id=uid)
        admin.delete("zone", zone=unbound)


def test_the_deploy_job_is_in_the_store(admin, v1, group, env_name):
    job = admin.get("job", id=v1["job"]["id"])
    assert job.status_code == 200, job.text
    j = job.json()
    assert j["id"] == v1["job"]["id"] and j["kind"] == "deploy" and j["status"] == "ok", j
    assert j["target"] == f"{group}/{env_name}" and j["started"] and j["finished"] and j["log"], sorted(j)


def test_the_deploy_job_survives_a_console_restart(admin, v1):
    """kind only: the console pod is restarted and the job read again. The kind chart runs `RAMEN_STORE=memory`
    (deploy/kind/values.yaml), in which case nothing survives a restart and this skips with the reason."""
    env = {
        e["name"]: e.get("value")
        for e in kube.deployment("ramen-system", "console")["spec"]["template"]["spec"]["containers"][0].get("env", [])
    }
    if env.get("RAMEN_STORE", "memory") == "memory":
        pytest.skip("the kind console runs RAMEN_STORE=memory: no store survives a pod restart (§26.2 needs one)")
    kube.write("-n", "ramen-system", "rollout", "restart", "deployment/console")
    kube.write("-n", "ramen-system", "rollout", "status", "deployment/console", "--timeout=300s", timeout=320)
    r = wait_for(
        lambda: (x := admin.get("job", id=v1["job"]["id"])).status_code == 200 and x, 120, "the job after the restart"
    )
    assert r.json()["status"] == "ok" and r.json()["log"]


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
        try:
            done = m.wait_task(created["taskId"], timeout=60)
        except httpx.TransportError as e:  # never retried: a TLS port answering otherwise is a finding, not noise
            why = edge_diagnosis(zone_targets, sleepy["zone"], sleepy["ns"])
            raise AssertionError(f"tasks/get through the edge failed: {e!r}\n{why}") from e
        assert done["status"] == "completed" and "job kind done" in text_of(done["result"]), done


# --- §24.1 sleep on idle, wake by a call ------------------------------------------------------------------------------
def test_an_idle_zone_goes_to_sleep(admin, sleepy, zone_targets, group):
    """The last call is this test's own: what the test before it sent last is not known here (1.0.1 kind run — its
    final poll never reached a worker, so the zone had been idle since the task began and slept 15 s into this test)."""
    zone, ns = sleepy["zone"], sleepy["ns"]
    with http_node(zone_targets, zone, sleepy["key"], group) as h:
        assert text_of(Modern(h).call("plain_tool", {"text": "last call"})) == "last call"
    sleepy["slept_after"] = began = time.time()  # the call reached a worker before this
    awake_or_explain(admin, group, zone, ns)  # had the zone been asleep, that call woke it
    wait_for(lambda: zone_state(admin, group, zone) == "asleep", SLEEP_AFTER + 180, f"{zone} asleep on idle", every=5)
    w = admin.get("workers", group=group, zone=zone).json()
    slept = w.get("state_since")  # the console's own time of the sleep (set once the workers were scaled to 0)
    assert isinstance(slept, (int, float)), w
    assert slept - began >= SLEEP_AFTER - 15, f"it slept {slept - began:.0f}s after the last call: {w}"
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


def wake_logs(ns: str) -> str:
    """The zone's worker logs (every container, previous ones too) and the console's revocation/reload lines of the
    last minutes: what a call that fails during a wake needs explained (a runtime restart mid-call, 1.0.1)."""
    out = []
    for args in (
        ("logs", "-n", ns, "-l", "app=worker", "--all-containers", "--prefix", "--since=6m", "--tail=150"),
        ("logs", "-n", ns, "-l", "app=worker", "--all-containers", "--prefix", "--previous", "--tail=60"),
        ("logs", "-n", "ramen-system", "deploy/console", "--since=6m", "--tail=400"),
    ):
        try:
            text = kube.kubectl(*args)
        except Exception as e:  # noqa: BLE001 - diagnostics only
            text = f"({e})"
        if args[2] == "ramen-system":
            text = "\n".join(line for line in text.splitlines() if any(w in line for w in ("revoc", "reload", ns)))
        out.append(f"--- kubectl {' '.join(args)}\n{text}")
    return "\n".join(out)


def test_a_call_through_the_edge_wakes_the_zone_and_is_answered(admin, sleepy, zone_targets, group):
    zone, ns = sleepy["zone"], sleepy["ns"]
    # only the activator may take the call, as in the gRPC case below: the activator forwards to a worker still
    # draining (20 s) without a wake, and that worker stops mid-call when its drain ends (1.0.1 kind run on db0b488:
    # -32603 `sidecar exited during call`, `"woke": false`, the old canary's `shutdown` 3 s into the call)
    wait_for(lambda: [p for p in activator_pods(ns) if ready(p)], 60, "a Ready activator pod")
    wait_for(lambda: not draining_worker_pods(ns), 120, "the draining worker pods are gone")
    began, t0 = time.time(), time.monotonic()
    with http_node(zone_targets, zone, sleepy["key"], group, timeout=WAKE_TIMEOUT + 30) as h:
        try:
            result = Modern(h).call("plain_tool", {"text": "good morning"}, timeout=WAKE_TIMEOUT + 30)
        except Exception as e:  # keep the evidence of whatever stopped the runtime during the wake
            took = time.monotonic() - t0
            raise AssertionError(f"the waking call failed after {took:.0f}s: {e}\n{wake_logs(ns)}") from e
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
    # only the activator may take the call: a worker still draining (20 s) would answer it without a wake
    wait_for(lambda: [p for p in activator_pods(ns) if ready(p)], 60, "a Ready activator pod")
    wait_for(lambda: not draining_worker_pods(ns), 120, "the draining worker pods are gone")
    assert audited(admin, "workers.sleep", zone, "manual", began)
    t0 = time.monotonic()
    with grpc_node(zone_targets, zone, sleepy["key"], group, timeout=WAKE_TIMEOUT + 30) as n:
        try:
            result = Modern(n).call("plain_tool", {"text": "grpc morning"}, timeout=WAKE_TIMEOUT + 30)
        except grpc.RpcError as e:
            raise AssertionError(f"the waking call failed after {time.monotonic() - t0:.0f}s: {e.code()}") from None
    assert text_of(result) == "grpc morning" and time.monotonic() - t0 <= WAKE_TIMEOUT + 5
    awake_or_explain(admin, group, zone, ns)
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


# --- §33.4a hardening: what the chart and the console put on the wire -------------------------------------------
def test_ramen_system_network_policies_and_the_redis_secret_are_in_place(kube_ready):
    names = {i["metadata"]["name"] for i in kube.get("networkpolicy", ns="ramen-system")["items"]}
    assert {"console", "jaeger", "redis"} <= names, names
    assert {"password", "url"} <= set(kube.secret_keys("ramen-system", "ramen-redis"))
    env = kube.deployment("ramen-system", "console")["spec"]["template"]["spec"]["containers"][0]["env"]
    redis = next(e for e in env if e["name"] == "RAMEN_REDIS_URL")
    assert redis["valueFrom"]["secretKeyRef"] == {"name": "ramen-redis", "key": "url"}  # never a plain value
    assert console_env("RAMEN_WORKER_EGRESS") == "1" and console_env("RAMEN_CLUSTER_SERVICE_CIDR") == "10.96.0.0/16"
    assert console_env("RAMEN_EVALS_SA") in (None, "ramen-evals")  # set only with an evals or scan engine


def test_every_zone_has_the_worker_egress_policy_and_the_chart_redis_as_its_store(v1, stack, group):
    for zone in stack["zones"]:
        ns = kube.ns_name(group, zone)
        np = kube.get("networkpolicy", "worker-egress", ns)
        assert np["spec"]["policyTypes"] == ["Egress"]
        internet = next(r for r in np["spec"]["egress"] if any("ipBlock" in t for t in r["to"]))
        assert internet["to"][0]["ipBlock"] == {"cidr": "0.0.0.0/0", "except": ["10.96.0.0/16"]}
        assert "RAMEN_REDIS_SCOPE_URL" in kube.secret_keys(ns)  # the chart's Redis URL is in the deploy Secret
