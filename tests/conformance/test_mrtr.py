"""CONTRACTS §23.4 (v1.0.0): multi round-trip requests, on real `ramen-node` processes and both transports.

A tool (or callable prompt) raises `InputRequired`; a modern client gets `resultType: "input_required"` with the
requests and — when the tool gave state — a signed `requestState`, and retries with `inputResponses`. The state is an
HMAC over the tool's state, the caller, group, zone, method, name, argument digest and an expiry, so any pod of the
zone verifies it without a store and nothing else accepts it. A legacy client is told the tool needs a newer client.
Fixture: `fixtures/mrtr_group`."""

import base64
import json
import re
import secrets
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, text_of
from ramen_tests.modern import ALL_INPUT, Modern, assert_envelope

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
STATE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")  # b64url(payload) "." b64url(mac)
YES = {"confirm": {"action": "accept", "content": {"ok": True}}}
INTEGRITY = "requestState integrity check failed"
NEEDS_MODERN = "it can only ask a client that speaks MCP 2026-07-28"
SHARED = {"RAMEN_SESSION_SECRET": secrets.token_urlsafe(32)}  # one group secret: every node below is "the same group"
KEY_A, KEY_B = ("rmk_" + secrets.token_hex(12) for _ in range(2))


def start(**env) -> LocalNode:
    return LocalNode(
        bucket=E.FIXTURES / "mrtr_group", key=KEY_A, env={**SHARED, "RAMEN_MCP_KEYS": f"{KEY_A},{KEY_B}", **env}
    )


@pytest.fixture(scope="module")
def node():
    with start() as n:
        n.wait_serving()
        yield n


@pytest.fixture(params=TRANSPORTS, ids=TRANSPORTS)
def m(request, node) -> Modern:
    return Modern(node.for_transport(request.param), ALL_INPUT)


@pytest.fixture
def http(node) -> Modern:
    return Modern(node.http, ALL_INPUT)


def ask(m: Modern, item: str = "note-1") -> dict:
    first = m.call("ask_confirm", {"item": item})
    assert first["resultType"] == "input_required", first
    return first


def integrity_failure(reply) -> bool:
    return reply.code == -32602 and reply.error["message"] == INTEGRITY and reply.status in (None, 200)


# --- rounds ----------------------------------------------------------------------------------------------------------
def test_round_one_is_an_input_required_result_with_signed_state(m):
    first = ask(m)
    assert_envelope(first, "input_required")
    assert set(first) == {"resultType", "inputRequests", "requestState", "_meta"}, sorted(first)
    request = first["inputRequests"]["confirm"]
    assert request["method"] == "elicitation/create", request
    assert request["params"]["message"] == "Delete note-1?"
    assert request["params"]["requestedSchema"]["required"] == ["ok"]
    assert request["params"].get("mode", "form") == "form"
    assert STATE.match(first["requestState"]), first["requestState"]
    assert "content" not in first and "isError" not in first


def test_the_retry_with_the_answer_and_the_state_completes(m):
    first = ask(m)
    done = m.call("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=first["requestState"])
    assert_envelope(done)
    assert text_of(done) == "deleted note-1" and done.get("isError") is not True, done
    assert "requestState" not in done and "inputRequests" not in done


def test_declining_is_an_ordinary_result(m):
    first = ask(m)
    for answer in ({"action": "decline"}, {"action": "cancel"}, {"action": "accept", "content": {"ok": False}}):
        done = m.call(
            "ask_confirm", {"item": "note-1"}, inputResponses={"confirm": answer}, requestState=first["requestState"]
        )
        assert text_of(done) == "kept note-1" and done["resultType"] == "complete", done


def test_a_tool_without_state_gets_no_request_state_and_needs_none(m):
    first = m.call("ask_name")
    assert first["resultType"] == "input_required" and list(first["inputRequests"]) == ["who"], first
    assert "requestState" not in first, "requestState only when the tool gave state"
    done = m.call("ask_name", inputResponses={"who": {"action": "accept", "content": {"name": "Alice"}}})
    assert text_of(done) == "Hello, Alice!", done


def test_a_missing_answer_asks_again_and_unknown_keys_are_ignored(m):
    again = m.call("ask_name", inputResponses={"wrong_key": {"action": "accept", "content": {"name": "x"}}})
    assert again["resultType"] == "input_required" and "who" in again["inputRequests"], again
    done = m.call(
        "ask_name",
        inputResponses={
            "who": {"action": "accept", "content": {"name": "Bo"}},
            "extra": {"action": "accept", "content": {}},
        },
    )
    assert text_of(done) == "Hello, Bo!", done


def test_the_state_is_not_single_use(m):
    first = ask(m)
    for _ in range(2):
        done = m.call("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=first["requestState"])
        assert text_of(done) == "deleted note-1"


def test_each_input_method_is_spelled_as_the_spec_spells_it(m):
    url = m.call("ask_url")["inputRequests"]["signin"]
    assert url["method"] == "elicitation/create" and url["params"]["mode"] == "url", url
    assert (url["params"]["message"], url["params"]["url"]) == ("Sign in to continue", "https://auth.example/start")
    sample = m.call("ask_sample")["inputRequests"]["llm"]
    assert sample["method"] == "sampling/createMessage" and sample["params"]["maxTokens"] == 20, sample
    assert sample["params"]["messages"] == [{"role": "user", "content": {"type": "text", "text": "Name a noodle"}}]
    roots = m.call("ask_roots")["inputRequests"]["roots"]
    assert roots["method"] == "roots/list", roots
    answer = {"role": "assistant", "content": {"type": "text", "text": "soba"}, "model": "m", "stopReason": "endTurn"}
    assert text_of(m.call("ask_sample", inputResponses={"llm": answer})) == "soba"
    listed = {"roots": [{"uri": "file:///a", "name": "A"}, {"uri": "file:///b"}]}
    assert text_of(m.call("ask_roots", inputResponses={"roots": listed})) == "file:///a,file:///b"
    assert text_of(m.call("ask_url", inputResponses={"signin": {"action": "accept"}})) == "signed in"


def test_a_callable_prompt_can_ask_too(m):
    first = m.request("prompts/get", {"name": "ask_prompt", "arguments": {}})
    assert first["resultType"] == "input_required" and first["inputRequests"]["topic"]["method"] == "elicitation/create"
    assert "messages" not in first
    answer = {"topic": {"action": "accept", "content": {"topic": "noodles"}}}
    done = m.request("prompts/get", {"name": "ask_prompt", "arguments": {}, "inputResponses": answer})
    assert done["resultType"] == "complete", done
    assert done["messages"] == [{"role": "user", "content": {"type": "text", "text": "Write about noodles."}}], done


def test_list_methods_never_answer_input_required(m):
    for method in ("tools/list", "prompts/list", "resources/list"):
        assert m.request(method, {"inputResponses": YES})["resultType"] == "complete"


# --- signed state: tamper, malformed --------------------------------------------------------------------------------
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def tampered(state: str) -> dict[str, object]:
    payload, mac = state.split(".")
    doc = json.loads(unb64url(payload))
    forged = b64url(json.dumps({**doc, "s": {"item": "everything", "asked": 1}}).encode())
    later = b64url(json.dumps({**doc, "e": doc["e"] + 86400}).encode())
    flipped = payload[:-2] + ("A" if payload[-2] != "A" else "B") + payload[-1]
    return {
        "suffix appended": state + "-TAMPERED",
        "payload rewritten, old mac": f"{forged}.{mac}",
        "expiry pushed out, old mac": f"{later}.{mac}",
        "one payload character flipped": f"{flipped}.{mac}",
        "mac truncated": f"{payload}.{mac[:-4]}",
        "mac of zeros": f"{payload}.{b64url(bytes(32))}",
        "no mac": payload,
        "empty": "",
        "not base64": "!!!.???",
        "three parts": state + "." + mac,
        "a number": 5,
        "an object": {"s": 1},
    }


def test_the_payload_is_what_the_contract_says_it_is(http):
    """`{"v":1,"s":state,"b":binding,"g":group,"z":zone,"m":method,"n":name,"a":args digest,"e":expiry}` — and the
    key itself is nowhere in it."""
    state = ask(http)["requestState"]
    doc = json.loads(unb64url(state.split(".")[0]))
    assert set(doc) == {"v", "s", "b", "g", "z", "m", "n", "a", "e"}, sorted(doc)
    assert (doc["v"], doc["s"]) == (1, {"item": "note-1", "asked": 1})
    assert (doc["g"], doc["z"], doc["m"], doc["n"]) == ("demo", "local", "tools/call", "ask_confirm")
    assert 590 <= doc["e"] - time.time() <= 601, "default RAMEN_MRTR_TTL_SECS is 600"
    assert len(unb64url(state.split(".")[1])) == 32, "HMAC-SHA256"
    assert KEY_A not in json.dumps(doc) and KEY_A not in state


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_tampered_or_malformed_state_is_refused(node, transport):
    m = Modern(node.for_transport(transport), ALL_INPUT)
    state = ask(m)["requestState"]
    accepted = []
    for case, bad in tampered(state).items():
        r = m.call_raw("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=bad)
        if not integrity_failure(r):
            accepted.append(f"{case}: {r.status} {r.message}")
    assert not accepted, "\n".join(accepted)
    lines = [x for x in node.log_lines() if x.get("reason") == "state_invalid" and x.get("transport") == transport]
    assert lines and lines[-1]["name"] == "ask_confirm", node.log_lines()[-3:]
    again = m.call("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=state)
    assert text_of(again) == "deleted note-1", "the genuine state still works after the forgeries"


# --- signed state: replay somewhere it was not issued for -------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_state_is_bound_to_the_caller_the_tool_the_arguments_and_the_method(node, transport):
    m = Modern(node.for_transport(transport), ALL_INPUT)
    state = ask(m)["requestState"]
    other_key = m.with_key(KEY_B).call_raw("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=state)
    assert integrity_failure(other_key), f"another key: {other_key.message}"
    other_args = m.call_raw("ask_confirm", {"item": "note-2"}, inputResponses=YES, requestState=state)
    assert integrity_failure(other_args), f"other arguments: {other_args.message}"
    other_tool = m.call_raw("confirm_job", {"item": "note-1"}, inputResponses=YES, requestState=state)
    assert integrity_failure(other_tool), f"another tool: {other_tool.message}"
    stateless_tool = m.call_raw("ask_name", requestState=state)
    assert integrity_failure(stateless_tool), f"a tool that never issued state: {stateless_tool.message}"
    other_method = m.raw("prompts/get", {"name": "ask_prompt", "arguments": {}, "requestState": state})
    assert integrity_failure(other_method), f"another method: {other_method.message}"
    own = m.with_key(KEY_B)
    theirs = ask(own)["requestState"]
    assert (
        text_of(own.call("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=theirs))
        == "deleted note-1"
    )


def test_the_argument_digest_is_exact(http):
    """C12's digest is over the canonical arguments: a value that differs only in case is another call."""
    state = ask(http, "a")["requestState"]
    assert integrity_failure(http.call_raw("ask_confirm", {"item": "A"}, inputResponses=YES, requestState=state))
    assert text_of(http.call("ask_confirm", {"item": "a"}, inputResponses=YES, requestState=state)) == "deleted a"


def test_another_pod_of_the_same_zone_accepts_the_state_and_other_zones_and_groups_do_not(node):
    """No store, no affinity: a second process with the group's secret verifies what the first one issued."""
    state = ask(Modern(node.http, ALL_INPUT))["requestState"]
    retry = {"inputResponses": YES, "requestState": state}
    with start() as twin, start(RAMEN_ZONE="other") as zone, start(RAMEN_GROUP="noodles") as group:
        for n in (twin, zone, group):
            n.wait_serving()
        for transport in TRANSPORTS:
            same = Modern(twin.for_transport(transport), ALL_INPUT).call("ask_confirm", {"item": "note-1"}, **retry)
            assert text_of(same) == "deleted note-1", same
            z = Modern(zone.for_transport(transport, zone="other"), ALL_INPUT)
            assert integrity_failure(z.call_raw("ask_confirm", {"item": "note-1"}, **retry)), "another zone"
            g = Modern(group.for_transport(transport, group="noodles"), ALL_INPUT)
            assert integrity_failure(g.call_raw("ask_confirm", {"item": "note-1"}, **retry)), "another group"
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", key=KEY_A) as stranger:  # same key, its own generated secret
        stranger.wait_serving()
        r = Modern(stranger.http, ALL_INPUT).call_raw("ask_confirm", {"item": "note-1"}, **retry)
        assert integrity_failure(r), "a node with another session secret"


def test_an_expired_state_is_refused():
    with start(RAMEN_MRTR_TTL_SECS="2") as n:
        n.wait_serving()
        m = Modern(n.http, ALL_INPUT)
        state = ask(m)["requestState"]
        fresh = m.call("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=state)
        assert text_of(fresh) == "deleted note-1"
        time.sleep(3.2)
        late = m.call_raw("ask_confirm", {"item": "note-1"}, inputResponses=YES, requestState=state)
        assert integrity_failure(late), late.message
        assert any(x.get("reason") == "state_invalid" for x in n.log_lines())


# --- inputResponses shape ---------------------------------------------------------------------------------------------
@pytest.mark.parametrize("bad", [5, "yes", [], [{"who": {}}], {"who": 12345}, {"who": "Alice"}, {"who": None}, None])
def test_input_responses_must_be_an_object_of_objects(m, bad):
    r = m.call_raw("ask_name", inputResponses=bad)
    assert r.code == -32602 and r.status in (None, 200), (bad, r.message)


# --- capability check -------------------------------------------------------------------------------------------------
CAPABILITY_CASES = [
    ("ask_name", {}, {"elicitation"}),
    ("ask_name", {"sampling": {}, "roots": {}}, {"elicitation"}),
    ("ask_sample", {"elicitation": {}}, {"sampling"}),
    ("ask_roots", {"elicitation": {}, "sampling": {}}, {"roots"}),
    ("ask_url", {}, {"elicitation"}),
    ("ask_url", {"elicitation": {}}, {"elicitation"}),  # an empty elicitation object is form mode only
    ("ask_url", {"elicitation": {"form": {}}}, {"elicitation"}),
]


@pytest.mark.parametrize(
    "tool,caps,missing", CAPABILITY_CASES, ids=[f"{c[0]}:{sorted(c[1])}" for c in CAPABILITY_CASES]
)
def test_an_input_request_the_client_did_not_declare_is_32021(m, tool, caps, missing):
    r = m.call_raw(tool, capabilities=caps)
    assert r.code == -32021 and r.status in (None, 400), r.message
    required = r.error["data"]["requiredCapabilities"]
    assert set(required) == missing and all(isinstance(v, dict) for v in required.values()), required
    if tool == "ask_url":
        assert "url" in required["elicitation"], required
    assert r.result is None


@pytest.mark.parametrize(
    "tool,caps",
    [
        ("ask_name", {"elicitation": {}}),
        ("ask_name", {"elicitation": {"form": {}}}),
        ("ask_url", {"elicitation": {"url": {}}}),
        ("ask_url", {"elicitation": {"form": {}, "url": {}}}),
        ("ask_sample", {"sampling": {}}),
        ("ask_roots", {"roots": {"listChanged": True}}),
    ],
)
def test_a_declared_input_method_is_asked(m, tool, caps):
    assert m.call(tool, capabilities=caps)["resultType"] == "input_required"


def test_missing_capability_raised_by_the_tool_is_32021(m):
    r = m.call_raw("needs_sampling", capabilities={})
    assert r.code == -32021 and r.status in (None, 400), r.message
    assert r.error["data"] == {"requiredCapabilities": {"sampling": {}}}, r.error
    assert text_of(m.call("needs_sampling", capabilities={"sampling": {}})) == "has sampling"


def test_the_tool_sees_the_capabilities_of_this_request(m):
    caps = {"sampling": {}, "elicitation": {"form": {}}, "extensions": {"x.example/thing": {"a": 1}}}
    assert json.loads(text_of(m.call("caps_echo", capabilities=caps))) == caps
    assert json.loads(text_of(m.call("caps_echo", capabilities={}))) == {}


# --- legacy downgrade -------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("transport", TRANSPORTS)
def test_a_legacy_client_is_told_the_tool_needs_a_modern_client(node, transport):
    c = node.for_transport(transport)
    c.initialize()
    r = c.call_tool("ask_name")
    assert r["isError"] is True and text_of(r) == f"this tool needs more input (who); {NEEDS_MODERN}", r
    assert "inputRequests" not in r and "resultType" not in r and "requestState" not in r
    r = c.call_tool("ask_confirm", {"item": "x"})
    assert r["isError"] is True and text_of(r) == f"this tool needs more input (confirm); {NEEDS_MODERN}", r
    with pytest.raises(JsonRpcError) as e:
        c.get_prompt("ask_prompt")
    assert e.value.code == -32602 and "needs more input (topic)" in e.value.message and NEEDS_MODERN in e.value.message
    missing = c.call_tool("needs_sampling")
    assert missing["isError"] is True and "sampling" in text_of(missing) and "2026-07-28" in text_of(missing), missing
    assert text_of(c.call_tool("plain_tool", {"text": "still fine"})) == "still fine"


# --- progress ---------------------------------------------------------------------------------------------------------
def progress_of(reply, token) -> list[dict]:
    return [
        n["params"]
        for n in reply.notifications
        if n["method"] == "notifications/progress" and n["params"].get("progressToken") == token
    ]


def test_progress_rides_the_post_as_sse_then_the_response(http):
    r = http.call_raw("progress_tool", {"steps": 3}, meta_extra={"progressToken": "tok-1"})
    assert r.status == 200 and r.sse, r.headers
    updates = progress_of(r, "tok-1")
    assert [(u["progress"], u["total"], u["message"]) for u in updates] == [
        (1, 3, "step 1"),
        (2, 3, "step 2"),
        (3, 3, "step 3"),
    ]
    assert all(n.get("jsonrpc") == "2.0" and "id" not in n for n in r.notifications)
    assert text_of(r.result) == "done 3" and r.result["resultType"] == "complete"


def test_an_integer_progress_token_is_echoed_as_an_integer(http):
    r = http.call_raw("progress_tool", {"steps": 1}, meta_extra={"progressToken": 42})
    assert [u["progressToken"] for u in progress_of(r, 42)] == [42], r.notifications


def test_progress_is_dropped_without_a_token_or_without_sse_in_accept(http, node):
    plain = http.call_raw("progress_tool", {"steps": 2})
    assert not plain.sse and plain.notifications == [] and text_of(plain.result) == "done 2"
    json_only = http.call_raw(
        "progress_tool", {"steps": 2}, meta_extra={"progressToken": "t"}, accept="application/json"
    )
    assert not json_only.sse and text_of(json_only.result) == "done 2"
    grpc = Modern(node.node).call_raw("progress_tool", {"steps": 2}, meta_extra={"progressToken": "t"})
    assert text_of(grpc.result) == "done 2", "gRPC is unary: the lines are dropped, the result arrives"


def test_a_legacy_client_gets_progress_the_same_way(node):
    c = node.http_client()
    c.initialize()
    c.notifications.clear()
    params = {"name": "progress_tool", "arguments": {"steps": 3}, "_meta": {"progressToken": "legacy-1"}}
    result = c.request("tools/call", params)
    assert c.last_response.headers["content-type"].startswith("text/event-stream")
    got = [n["params"]["progress"] for n in c.notifications if n["method"] == "notifications/progress"]
    assert got == [1, 2, 3] and text_of(result) == "done 3" and "resultType" not in result, (got, result)


# --- secret-named form fields -----------------------------------------------------------------------------------------
SECRETISH = ["password", "PASSWORD", "passwd", "client_secret", "access_token", "api_key", "apikey", "api-key",
             "ApiKey", "credential", "card_number", "cvv", "ssn"]  # fmt: skip


@pytest.mark.parametrize("field", SECRETISH)
def test_a_form_field_named_like_a_credential_is_refused(m, field):
    r = m.call("ask_field", {"field": field})
    assert r["resultType"] == "complete" and r["isError"] is True, r
    assert text_of(r) == f"input request refused: login.{field} looks like a secret; use elicit_url", r
    assert "inputRequests" not in r


@pytest.mark.parametrize("field", ["username", "email", "colour", "pass_count"])
def test_an_ordinary_form_field_is_asked(m, field):
    r = m.call("ask_field", {"field": field})
    assert (
        r["resultType"] == "input_required"
        and field in r["inputRequests"]["login"]["params"]["requestedSchema"]["properties"]
    )


def test_the_refusal_reaches_a_legacy_client_as_the_same_tool_error(node):
    c = node.http_client()
    c.initialize()
    r = c.call_tool("ask_field", {"field": "password"})
    assert (
        r["isError"] is True
        and text_of(r) == "input request refused: login.password looks like a secret; use elicit_url"
    )
