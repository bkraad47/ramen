"""CONTRACTS §24.3 (v1.0.0), consent half: a group with `require_telemetry_consent` serves `tools/call`, `prompts/get`
and `resources/read` only to a caller who accepted the telemetry notice. Real node + runtime, both transports.

Three ways in: a console token with `tel: true` (accepted on the OAuth consent page — never asked), the console's own
call (worker admin key), or the prompt: a 2026-07-28 client that declared `elicitation` gets an MRTR round asking
`ramen_telemetry_consent` with a signed `requestState`; `accept: true` is remembered for that caller for 30 days and
the call runs — a tool that asks its own question does so in the next round. Everyone else is refused with a reason a
person can act on. Lists and `server/discover` stay open, and discover tells an agent where to send its telemetry.
Fixture: `fixtures/mrtr_group`."""

import base64
import json
import re
import secrets
import time

import pytest

from ramen_tests import env as E
from ramen_tests.localconsole import SESSION_SECRET
from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError, text_of
from ramen_tests.modern import ALL_INPUT, Modern, assert_envelope
from ramen_tests.tokens import user_token

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
ISSUER = "https://console.example"
ADMIN = secrets.token_hex(16)
KEYS = ["rmk_" + secrets.token_hex(12) for _ in range(80)]  # consent is remembered per key: each test takes its own
HOST = "otel.example:4318"
CLIENT = {
    "otlp_endpoint": f"http://{HOST}",
    "headers": {"x-team": "noodles"},
    "resource_attributes": {"ramen.group": "demo", "ramen.zone": "local"},
}
CONSENT = "ramen_telemetry_consent"
MESSAGE = (
    "This Ramen group records telemetry about your calls (tool name, timing, outcome — never arguments or results) "
    f"at {HOST} and asks your agent to send its telemetry there. Accept to continue."
)
LEGACY_REFUSAL = (
    "forbidden: this group requires telemetry consent; use a client that speaks MCP 2026-07-28 or sign in through OAuth"
)
STATE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
ACCEPT = {CONSENT: {"action": "accept", "content": {"accept": True}}}
REFUSALS = {
    "decline": {"action": "decline"},
    "cancel": {"action": "cancel"},
    "accept-false": {"action": "accept", "content": {"accept": False}},
}
ENV = {
    "RAMEN_TELEMETRY_CONSENT": "1",
    "RAMEN_TELEMETRY_NOTICE": HOST,
    "RAMEN_OTEL_CLIENT": json.dumps(CLIENT),
    "RAMEN_SESSION_SECRET": SESSION_SECRET,
    "RAMEN_OAUTH_ISSUER": ISSUER,
    "RAMEN_MCP_KEYS": ",".join(KEYS),
}
_pool = iter(KEYS[1:])  # KEYS[0] is the node fixture's default key and never consents


@pytest.fixture(scope="module")
def node():
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", key=KEYS[0], admin_key=ADMIN, env=ENV) as n:
        n.wait_serving()
        yield n


@pytest.fixture(params=TRANSPORTS, ids=TRANSPORTS)
def transport(request) -> str:
    return request.param


@pytest.fixture
def m(node, transport) -> Modern:
    """A modern client that declares every input method, with a key nobody has consented with."""
    return Modern(node.for_transport(transport), ALL_INPUT, key=next(_pool))


def person(node, transport, sub: str | None = None, **claims) -> Modern:
    token, _ = user_token(SESSION_SECRET, ISSUER, 600, sub=sub or "u-" + secrets.token_hex(4), **claims)
    return Modern(node.for_transport(transport), ALL_INPUT, key=token)


def asked(result: dict) -> dict:
    """Assert `result` is the consent round and return it."""
    assert result.get("resultType") == "input_required", result
    assert list(result["inputRequests"]) == [CONSENT], result["inputRequests"]
    return result


def consent(m: Modern, tool: str = "plain_tool", arguments: dict | None = None) -> dict:
    """Round one + the accepting retry → the retry's result."""
    arguments = {"text": "hi"} if arguments is None and tool == "plain_tool" else (arguments or {})
    first = asked(m.call(tool, arguments))
    return m.call(tool, arguments, inputResponses=ACCEPT, requestState=first["requestState"])


def lines(node, reason: str | None = None) -> list[dict]:
    out = [x for x in node.log_lines() if x.get("msg") == "mcp"]
    return [x for x in out if x.get("reason") == reason] if reason else out


# --- the prompt -------------------------------------------------------------------------------------------------------
def test_an_unconsented_call_is_answered_with_the_consent_request(m):
    first = m.call("plain_tool", {"text": "hi"})
    assert_envelope(first, "input_required")
    assert set(first) == {"resultType", "inputRequests", "requestState", "_meta"}, sorted(first)
    request = asked(first)["inputRequests"][CONSENT]
    assert request["method"] == "elicitation/create", request
    assert request["params"]["message"] == MESSAGE
    assert request["params"].get("mode", "form") == "form"
    schema = request["params"]["requestedSchema"]
    assert schema["properties"]["accept"]["type"] == "boolean" and schema.get("type", "object") == "object", schema
    assert STATE.match(first["requestState"]), "a signed requestState (§23.4)"
    assert "content" not in first, "the tool did not run"


def test_accepting_runs_the_call(m):
    done = consent(m)
    assert_envelope(done)
    assert text_of(done) == "hi" and "inputRequests" not in done and "requestState" not in done, done


def test_a_tool_that_asks_its_own_question_does_so_in_the_next_round(m):
    first = asked(m.call("ask_name"))
    second = m.call("ask_name", inputResponses=ACCEPT, requestState=first["requestState"])
    assert second["resultType"] == "input_required", second
    assert second["inputRequests"]["who"]["params"]["message"] == "What is your name?"
    assert CONSENT not in second["inputRequests"]
    assert "requestState" not in second, "ask_name keeps no state: the consent round's state is not passed on"
    third = m.call("ask_name", inputResponses={"who": {"action": "accept", "content": {"name": "Mika"}}})
    assert text_of(third) == "Hello, Mika!", third


def test_a_tool_with_its_own_signed_state_works_after_the_consent_round(m):
    """`ask_confirm` gives state: round 2 carries the TOOL's requestState, round 3 answers it."""
    args = {"item": "note-7"}
    first = asked(m.call("ask_confirm", args))
    second = m.call("ask_confirm", args, inputResponses=ACCEPT, requestState=first["requestState"])
    assert second["resultType"] == "input_required" and list(second["inputRequests"]) == ["confirm"], second
    assert STATE.match(second["requestState"]) and second["requestState"] != first["requestState"]
    yes = {"confirm": {"action": "accept", "content": {"ok": True}}}
    third = m.call("ask_confirm", args, inputResponses=yes, requestState=second["requestState"])
    assert text_of(third) == "deleted note-7", third


def test_the_consent_state_is_not_a_tool_state(m, node, transport):
    """The two kinds of requestState are not interchangeable: a consent state does not resume a tool."""
    args = {"item": "note-8"}
    first = asked(m.call("ask_confirm", args))
    m.call("ask_confirm", args, inputResponses=ACCEPT, requestState=first["requestState"])  # consent on record
    yes = {"confirm": {"action": "accept", "content": {"ok": True}}}
    r = m.call_raw("ask_confirm", args, inputResponses=yes, requestState=first["requestState"])
    assert r.result is None or text_of(r.result) != "deleted note-8", "a consent state resumed the tool"


@pytest.mark.parametrize("answer", list(REFUSALS), ids=list(REFUSALS))
def test_refusing_is_32602_and_nothing_runs(m, answer):
    first = asked(m.call("plain_tool", {"text": "hi"}))
    r = m.call_raw(
        "plain_tool", {"text": "hi"}, inputResponses={CONSENT: REFUSALS[answer]}, requestState=first["requestState"]
    )
    assert r.code == -32602 and r.error["message"] == "telemetry consent declined", r.message
    assert r.result is None
    asked(m.call("plain_tool", {"text": "hi"}))  # a refusal is not remembered: the next call asks again


def test_an_answer_that_is_not_a_boolean_true_is_a_refusal(m):
    first = asked(m.call("plain_tool", {"text": "hi"}))
    for content in ({"accept": "true"}, {"accept": 1}, {}, {"ok": True}):
        r = m.call_raw(
            "plain_tool",
            {"text": "hi"},
            inputResponses={CONSENT: {"action": "accept", "content": content}},
            requestState=first["requestState"],
        )
        assert r.code == -32602, f"{content} was taken as consent: {r.message}"
    asked(m.call("plain_tool", {"text": "hi"}))


# --- remembered per caller --------------------------------------------------------------------------------------------
def test_the_consent_is_remembered_for_that_key(m, node):
    consent(m)
    assert text_of(m.call("plain_tool", {"text": "again"})) == "again", "not asked twice"
    for other in TRANSPORTS:  # the record is the caller's, not the connection's
        again = Modern(node.for_transport(other), ALL_INPUT, key=m.key)
        assert text_of(again.call("plain_tool", {"text": other})) == other
    bare = Modern(m.node, {}, key=m.key)  # and no capability is needed once it is on record
    assert text_of(bare.call("plain_tool", {"text": "bare"})) == "bare"


def test_the_consent_covers_prompts_and_resources_of_that_caller(m):
    consent(m)
    got = m.request("prompts/get", {"name": "pick_prompt", "arguments": {"flavour": "miso", "size": "large"}})
    assert got["resultType"] == "complete" and got["messages"], got
    read = m.request("resources/read", {"uri": "ramen://bowl/miso/recipe"})
    assert read["resultType"] == "complete" and read["contents"], read


def test_it_is_not_remembered_for_another_key(m, node, transport):
    consent(m)
    other = Modern(node.for_transport(transport), ALL_INPUT, key=next(_pool))
    asked(other.call("plain_tool", {"text": "hi"}))


def test_a_legacy_call_with_a_consented_key_is_served(m, node, transport):
    """The consent belongs to the caller: given once through a modern client, an older client of the same key runs."""
    consent(m)
    c = node.for_transport(transport, key=m.key)
    c.initialize()
    assert text_of(c.call_tool("plain_tool", {"text": "old client"})) == "old client"


# --- every gated method asks; nothing else does -----------------------------------------------------------------------
def test_prompts_get_and_resources_read_ask_too(m):
    for method, params in (
        ("prompts/get", {"name": "pick_prompt", "arguments": {"flavour": "miso", "size": "large"}}),
        ("resources/read", {"uri": "ramen://bowl/miso/recipe"}),
    ):
        first = asked(m.request(method, params))
        assert "messages" not in first and "contents" not in first
    params = {"uri": "ramen://bowl/miso/recipe"}
    first = m.request("resources/read", params)
    done = m.request("resources/read", {**params, "inputResponses": ACCEPT, "requestState": first["requestState"]})
    assert done["resultType"] == "complete" and done["contents"], done


def test_lists_and_discover_stay_open(m):
    for method in ("tools/list", "prompts/list", "resources/list", "resources/templates/list", "server/discover"):
        assert m.request(method)["resultType"] == "complete", method
    assert {t["name"] for t in m.request("tools/list")["tools"]} >= {"plain_tool", "ask_name"}
    done = m.request(
        "completion/complete",
        {"ref": {"type": "ref/prompt", "name": "pick_prompt"}, "argument": {"name": "flavour", "value": "mi"}},
    )
    assert done["completion"]["values"] == ["miso"], done


def test_legacy_lists_and_ping_stay_open(node, transport):
    c = node.for_transport(transport, key=next(_pool))
    assert c.initialize()["serverInfo"]["name"] == "ramen-node"
    assert c.ping() == {}
    assert {t["name"] for t in c.list_tools()} >= {"plain_tool"}
    assert c.list_prompts() and c.list_resources() is not None


def test_discover_tells_the_agent_where_to_send_its_telemetry(m):
    meta = m.discover()["_meta"]
    assert meta.get("io.ramen/telemetry") == CLIENT, meta


def test_discover_carries_it_for_a_person_too(node, transport):
    assert person(node, transport).discover()["_meta"].get("io.ramen/telemetry") == CLIENT


def test_an_unauthenticated_caller_learns_nothing(node, transport):
    c = node.for_transport(transport)
    body = Modern(c).body("server/discover")
    assert c.outcome(body, key=None) == "UNAUTHENTICATED"
    assert c.outcome(Modern(c).body("tools/call", {"name": "plain_tool", "arguments": {}}), key=None) == (
        "UNAUTHENTICATED"
    ), "the credential is checked before the consent gate"


# --- who cannot be asked ----------------------------------------------------------------------------------------------
def test_a_modern_client_without_elicitation_is_32021(m):
    for caps in ({}, {"sampling": {}, "roots": {}}):
        r = m.call_raw("plain_tool", {"text": "hi"}, capabilities=caps)
        assert r.code == -32021 and r.status in (None, 400), r.message
        assert r.error["data"] == {"requiredCapabilities": {"elicitation": {}}}, r.error
        assert r.result is None


def test_a_legacy_client_is_32003_with_the_documented_text(node, transport):
    c = node.for_transport(transport, key=next(_pool))
    c.initialize()
    for call in (
        lambda: c.call_tool("plain_tool", {"text": "hi"}),
        lambda: c.get_prompt("pick_prompt", {"flavour": "miso", "size": "large"}),
        lambda: c.read_resource("ramen://bowl/miso/recipe"),
    ):
        with pytest.raises(JsonRpcError) as e:
            call()
        assert (e.value.code, e.value.message) == (-32003, LEGACY_REFUSAL), e.value


# --- console tokens ---------------------------------------------------------------------------------------------------
def test_a_token_with_tel_true_is_never_prompted(node, transport):
    p = person(node, transport, tel=True)
    assert text_of(p.call("plain_tool", {"text": "signed in"})) == "signed in"
    assert p.request("resources/read", {"uri": "ramen://bowl/miso/recipe"})["contents"]
    no_caps = Modern(p.node, {}, key=p.key)
    assert text_of(no_caps.call("plain_tool", {"text": "no caps"})) == "no caps", (
        "nothing to elicit: no capability needed"
    )
    legacy = node.for_transport(transport, key=p.key)
    legacy.initialize()
    assert text_of(legacy.call_tool("plain_tool", {"text": "legacy"})) == "legacy", "the token is the consent"


@pytest.mark.parametrize(
    "claims", [{}, {"tel": False}, {"tel": "true"}, {"tel": 1}], ids=["absent", "false", "str", "1"]
)
def test_a_token_without_tel_true_is_prompted(node, transport, claims):
    p = person(node, transport, **claims)
    asked(p.call("plain_tool", {"text": "hi"}))
    legacy = node.for_transport(transport, key=p.key)
    legacy.initialize()
    with pytest.raises(JsonRpcError) as e:
        legacy.call_tool("plain_tool", {"text": "hi"})
    assert e.value.code == -32003


def test_a_persons_prompt_consent_outlives_the_token(node, transport):
    """The record is bound to `user:<sub>` (§16.2), so the next access token of the same person is not asked."""
    sub = "u-" + secrets.token_hex(4)
    assert text_of(consent(person(node, transport, sub=sub, jti="t1"))) == "hi"
    assert text_of(person(node, transport, sub=sub, jti="t2").call("plain_tool", {"text": "next"})) == "next"
    asked(person(node, transport, sub=sub + "x", jti="t3").call("plain_tool", {"text": "hi"}))


# --- the console's own calls ------------------------------------------------------------------------------------------
def with_admin(m: Modern, tool: str, arguments: dict, admin_key: str) -> dict:
    """A modern `tools/call` that also presents the worker admin key, as the console's golden runs do."""
    body = m.body("tools/call", {"name": tool, "arguments": arguments})
    if m.transport == "grpc":
        return json.loads(m.node.call_raw(body, m.key, extra=[("x-ramen-admin-key", admin_key)]))
    return m.send(body, headers={"x-ramen-admin-key": admin_key}).message


def test_a_call_presenting_the_worker_admin_key_skips_the_gate(m):
    """node-agent's pin: the MCP credential plus `x-ramen-admin-key` — golden cases and package listing."""
    bare = Modern(m.node, {}, key=m.key)  # the console declares no elicitation
    out = with_admin(bare, "plain_tool", {"text": "golden"}, ADMIN)
    assert text_of(out["result"]) == "golden", out
    asked(m.call("plain_tool", {"text": "hi"}))  # and it recorded nothing for that key


def test_a_wrong_admin_key_does_not_skip_it(m):
    out = with_admin(m, "plain_tool", {"text": "x"}, "not-the-admin-key")
    assert "result" not in out or out["result"].get("resultType") == "input_required", out


# --- the signed state -------------------------------------------------------------------------------------------------
def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def refused_and_not_recorded(m: Modern, state, why: str, tool: str = "plain_tool", arguments=None) -> None:
    arguments = {"text": "hi"} if arguments is None else arguments
    params = {"inputResponses": ACCEPT}
    if state is not None:
        params["requestState"] = state
    r = m.call_raw(tool, arguments, **params)
    assert r.code == -32602 and r.result is None, f"{why}: {r.message}"
    assert r.error["message"] == "requestState integrity check failed", f"{why}: {r.error}"
    asked(m.call("plain_tool", {"text": "hi"}))  # nothing was recorded


def test_an_accept_without_the_signed_state_is_rejected(m):
    refused_and_not_recorded(m, None, "no requestState")


def test_a_tampered_consent_state_is_rejected(m):
    state = asked(m.call("plain_tool", {"text": "hi"}))["requestState"]
    payload, mac = state.split(".")
    doc = json.loads(unb64url(payload))
    later = b64url(json.dumps({**doc, "e": doc.get("e", 0) + 86400}).encode())
    flipped = payload[:-2] + ("A" if payload[-2] != "A" else "B") + payload[-1]
    for why, forged in {
        "suffix appended": state + "-x",
        "expiry pushed out, old mac": f"{later}.{mac}",
        "one payload character flipped": f"{flipped}.{mac}",
        "mac of zeros": f"{payload}.{b64url(bytes(32))}",
        "no mac": payload,
        "empty": "",
    }.items():
        refused_and_not_recorded(m, forged, why)


def test_another_callers_consent_state_is_rejected(m, node, transport):
    other = Modern(node.for_transport(transport), ALL_INPUT, key=next(_pool))
    theirs = asked(other.call("plain_tool", {"text": "hi"}))["requestState"]
    refused_and_not_recorded(m, theirs, "state signed for another key")


def test_a_consent_state_for_another_request_is_rejected(m):
    state = asked(m.call("plain_tool", {"text": "hi"}))["requestState"]
    refused_and_not_recorded(m, state, "state signed for other arguments", arguments={"text": "other"})
    refused_and_not_recorded(m, state, "state signed for another tool", tool="ask_name", arguments={})


def test_the_state_names_no_credential(m):
    state = asked(m.call("plain_tool", {"text": "hi"}))["requestState"]
    doc = unb64url(state.split(".")[0]).decode()
    assert m.key not in doc and m.key not in state


def test_an_expired_consent_state_is_rejected():
    env = {**ENV, "RAMEN_MRTR_TTL_SECS": "1"}
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", key=KEYS[1], admin_key=ADMIN, env=env) as n:
        n.wait_serving()
        m = Modern(n.http, ALL_INPUT)
        state = asked(m.call("plain_tool", {"text": "hi"}))["requestState"]
        time.sleep(2.2)
        r = m.call_raw("plain_tool", {"text": "hi"}, inputResponses=ACCEPT, requestState=state)
        assert r.code == -32602 and r.result is None, r.message


# --- access log -------------------------------------------------------------------------------------------------------
def test_the_access_log_names_each_outcome(node):
    key = next(_pool)
    m = Modern(node.http, ALL_INPUT, key=key)
    first = asked(m.call("plain_tool", {"text": "log-1"}))
    refuse = {CONSENT: {"action": "decline"}}
    m.call_raw("plain_tool", {"text": "log-1"}, inputResponses=refuse, requestState=first["requestState"])
    m.call("plain_tool", {"text": "log-1"}, inputResponses=ACCEPT, requestState=first["requestState"])
    Modern(node.http, {}, key=next(_pool)).call_raw("plain_tool", {"text": "log-2"})
    time.sleep(0.3)
    assert lines(node, "consent_required"), "the prompt (and the -32021) are logged as consent_required"
    assert lines(node, "consent_declined"), "a refusal is logged as consent_declined"
    granted = [x for x in lines(node) if x.get("consent") == "granted"]
    assert granted and granted[-1]["name"] == "plain_tool" and granted[-1]["status"] == "ok", granted[-1:]
    text = node.log.read_text()
    assert key not in text and "log-1" not in text, "neither the key nor an argument in the log"


def test_a_group_without_the_setting_never_asks():
    env = {k: v for k, v in ENV.items() if k != "RAMEN_TELEMETRY_CONSENT"}
    with LocalNode(bucket=E.FIXTURES / "mrtr_group", key=KEYS[1], env=env) as n:
        n.wait_serving()
        assert text_of(Modern(n.http, {}).call("plain_tool", {"text": "free"})) == "free"
        c = n.client()
        c.initialize()
        assert text_of(c.call_tool("plain_tool", {"text": "free"})) == "free"
        assert Modern(n.http).discover()["_meta"].get("io.ramen/telemetry") == CLIENT, "telemetry on, consent off"
