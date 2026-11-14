"""CONTRACTS §23.7: nothing a 0.8 client sees changes, and the modern era is the same payload in the new envelope.

`golden/legacy-0.8.0.json` holds every exchange `golden/record.py` captured from a node and runtime built from the
clean `v0.8.0` tag: per fixture group (and the demo repo), per transport, `initialize` through every tool, resource
and prompt (valid, invalid, unknown), `ping`, unknown methods, and on HTTP the status and the headers that matter.

1. Legacy: the current node answers each recorded request identically — body, HTTP status, `MCP-Protocol-Version`,
   presence of `Mcp-Session-Id`.
2. Modern: the same calls with per-request `_meta` (and the standard headers on HTTP) return the recorded payload plus
   exactly the §23.1 envelope, and the §23.1 code wherever the legacy one was in `-32000..-32019`.

The demo repo gains tools in 1.0.0, so its lists are compared entry by entry (every recorded entry unchanged, in
order); its README resource is the repo's own README, which the release edits, so only that text is masked."""

import json
import shutil

import pytest

from ramen_tests import golden
from ramen_tests.localnode import LocalNode
from ramen_tests.modern import MODERN, SERVER_INFO, Modern, assert_envelope, strip_envelope

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
RECORDED = golden.load()["groups"] if golden.GOLDEN.exists() else {}
LISTS = {"tools/list": "tools", "resources/list": "resources", "prompts/list": "prompts"}
TTL = {"tools/list": 60000, "resources/list": 60000, "prompts/list": 60000, "resources/read": 0}
REMOVED = ("initialize", "ping")
README = "ramen://demo/readme"


@pytest.fixture(scope="module", params=sorted(RECORDED), ids=sorted(RECORDED))
def group(request, tmp_path_factory):
    bucket = golden.GROUPS[request.param]
    if not bucket.exists():
        pytest.skip(f"{bucket} is not a checkout (RAMEN_DEMO_DIR, or the ramen-master submodule)")
    if request.param == "demo_repo":  # a copy: running a node in the checkout would leave bytecode in the submodule
        copy = tmp_path_factory.mktemp("demo-repo") / "repo"
        shutil.copytree(bucket, copy, ignore=shutil.ignore_patterns("__pycache__", ".git"))
        bucket = copy
    with LocalNode(bucket=bucket) as n:
        n.wait_serving()
        n.group_name = request.param
        yield n


def test_the_baseline_covers_every_fixture_group_on_both_transports():
    assert {"demo_group", "guarded_group", "demo_repo"} <= set(RECORDED), sorted(RECORDED)
    for name, per_transport in RECORDED.items():
        assert set(per_transport) == set(TRANSPORTS), name
        methods = {e["request"]["method"] for e in per_transport["http"]}
        assert {
            "initialize",
            "ping",
            "tools/list",
            "resources/list",
            "prompts/list",
            "tools/call",
            "no/such",
        } <= methods


def mask(group: str, method: str, response):
    """The one legitimately moving text: the demo repo's README, served by its `demo_readme` resource."""
    if group != "demo_repo" or method != "resources/read" or not isinstance(response, dict):
        return response
    contents = (response.get("result") or {}).get("contents")
    if not contents or contents[0].get("uri") != README:
        return response
    assert contents[0]["text"].startswith("# ramen-demo-mcp-group"), contents[0]["text"][:80]
    out = json.loads(json.dumps(response))
    out["result"]["contents"][0]["text"] = golden.VOLATILE
    return out


def same_payload(group: str, method: str, want, got) -> bool:
    """Equality, except that the demo repo's lists may have grown: every recorded entry present, unchanged, in order."""
    want, got = mask(group, method, want), mask(group, method, got)
    key = LISTS.get(method)
    if group != "demo_repo" or key is None or not isinstance(want, dict) or not isinstance(got, dict):
        return want == got
    w, g = want.get("result"), got.get("result")
    if not isinstance(w, dict) or not isinstance(g, dict):
        return want == got
    kept = [x for x in g.get(key, []) if x in w[key]]
    return (
        kept == w[key] and {**g, key: None} == {**w, key: None} and {**got, "result": None} == {**want, "result": None}
    )


def show(x) -> str:
    return json.dumps(x, sort_keys=True)[:600]


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_every_legacy_exchange_is_answered_as_0_8_0_answered_it(group, transport):
    recorded = RECORDED[group.group_name][transport]
    replayed = golden.replay_transport(group, transport, recorded)
    wrong = []
    for want, got in zip(recorded, replayed, strict=True):
        method = want["request"]["method"]
        if not same_payload(group.group_name, method, want["response"], got["response"]):
            wrong.append(f"{want['name']}: body\n   0.8.0: {show(want['response'])}\n   now:   {show(got['response'])}")
        for field in ("status", "headers"):
            if transport == "http" and want[field] != got[field]:
                wrong.append(f"{want['name']}: {field} 0.8.0 {want[field]} now {got[field]}")
    assert not wrong, f"{len(wrong)} legacy answer(s) changed:\n" + "\n".join(wrong)


def modern_error_for(method: str, params: dict, legacy: dict, transport: str) -> dict:
    """What §23.1/§23.2 make of a legacy error: `code` always; `message`, `data`, `status` where the contract fixes
    them."""
    name_key = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}.get(method)
    if transport == "http" and name_key and not isinstance(params.get(name_key), str):
        return {"code": -32020, "status": 400}  # §23.2: Mcp-Name is required and cannot be sent — refused first
    if -32019 <= legacy["code"] <= -32000:
        if method == "tools/call":
            return {"code": -32602, "message": f"Unknown tool: {params['name']}", "status": 200}
        if method == "resources/read":
            return {"code": -32602, "message": "Resource not found", "data": {"uri": params["uri"]}, "status": 200}
        return {"code": -32602, "status": 200}
    if legacy["code"] == -32601:
        return {"code": -32601, "status": 404}
    return {"code": legacy["code"], "message": legacy["message"], "status": 200}


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_the_modern_era_returns_the_same_payload_in_the_modern_envelope(group, transport):
    modern = Modern(group.for_transport(transport))
    wrong = []
    for entry in RECORDED[group.group_name][transport]:
        request, legacy, name = entry["request"], entry["response"], entry["name"]
        method, params = request["method"], request.get("params") or {}
        if method.startswith("notifications/") or entry.get("bare"):
            continue
        reply = modern.raw(method, params if method not in REMOVED else {})
        if transport == "http" and "mcp-session-id" in reply.headers:
            wrong.append(f"{name}: a modern answer carried Mcp-Session-Id")
        if method in REMOVED:
            if reply.code != -32601 or (transport == "http" and reply.status != 404):
                wrong.append(f"{name}: removed method must be -32601/404, got {reply.code}/{reply.status}")
            continue
        if "error" in legacy:
            want = modern_error_for(method, params, legacy["error"], transport)
            got = reply.error or {}
            bad = [k for k in ("code", "message", "data") if k in want and got.get(k) != want[k]]
            if transport == "http" and reply.status != want["status"]:
                bad.append("status")
            if got.get("code") is not None and -32019 <= got["code"] <= -32000:
                bad.append("code in -32000..-32019")
            if bad:
                wrong.append(f"{name}: {bad} want {want} got {reply.status} {show(reply.message)}")
            continue
        result = reply.result
        if result is None or (transport == "http" and reply.status != 200):
            wrong.append(f"{name}: want a result, got {reply.status} {show(reply.message)}")
            continue
        try:
            assert_envelope(result)
        except AssertionError as e:
            wrong.append(f"{name}: envelope: {str(e)[:300]}")
            continue
        cache = {k: result[k] for k in ("ttlMs", "cacheScope") if k in result}
        want_cache = {"ttlMs": TTL[method], "cacheScope": "private"} if method in TTL else {}
        if cache != want_cache:
            wrong.append(f"{name}: caching fields {cache}, want {want_cache}")
        if set(result["_meta"]) - set(legacy["result"].get("_meta") or {}) != {SERVER_INFO}:
            wrong.append(f"{name}: _meta gained more than serverInfo: {sorted(result['_meta'])}")
        got = {"jsonrpc": "2.0", "id": request["id"], "result": strip_envelope(result)}
        if not same_payload(group.group_name, method, legacy, got):
            wrong.append(f"{name}: payload\n   0.8.0:  {show(legacy['result'])}\n   modern: {show(got['result'])}")
    assert not wrong, f"{len(wrong)} modern answer(s) differ ({MODERN}):\n" + "\n".join(wrong)
