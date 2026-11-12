"""CONTRACTS §23.3/§23.4: InputRequired, MissingCapability, the call context, progress, guardrails per round."""

import io
import json
import threading

import pytest
from conftest import write_pkg

import ramen_runtime as rr
from ramen_runtime import guardrails
from ramen_runtime.executor import Executor
from ramen_runtime.loader import load
from ramen_runtime.rpc import Server

TOOL = {"type": "tool", "name": "t", "description": "d", "callable": "f", "input": {"a": {"type": "string"}}}
PROMPT = {"type": "prompt", "name": "p", "description": "d", "input": {}, "callable": "f"}
HEAD = "from ramen_runtime import *\n"
ASK = HEAD + (
    "def f(a):\n"
    "    r = input_responses()\n"
    "    if 'user_name' not in r:\n"
    "        raise InputRequired({'user_name': elicit('What is your name?', "
    "{'type': 'object', 'properties': {'name': {'type': 'string'}}, 'required': ['name']})})\n"
    "    return text('Hello, ' + r['user_name']['content']['name'] + '!')\n"
)
ELICIT = {
    "method": "elicitation/create",
    "params": {
        "mode": "form",
        "message": "What is your name?",
        "requestedSchema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]},
    },
}
ALICE = {"user_name": {"action": "accept", "content": {"name": "Alice"}}}


def tool(tmp_path, code, proto=TOOL):
    write_pkg(tmp_path, "tools", "t", proto, code)
    return Executor(load(tmp_path))


def test_package_root_exports():
    for name in (
        "text image audio resource InputRequired MissingCapability elicit elicit_url sample list_roots "
        "input_responses request_state client_capabilities progress"
    ).split():
        assert callable(getattr(rr, name)) and name in rr.__all__


def test_builders_and_defaults_outside_a_call():
    assert rr.elicit("m", {"type": "object"}) == {
        "method": "elicitation/create",
        "params": {"mode": "form", "message": "m", "requestedSchema": {"type": "object"}},
    }
    assert rr.elicit_url("m", "https://x/y") == {
        "method": "elicitation/create",
        "params": {"mode": "url", "message": "m", "url": "https://x/y"},
    }
    user = {"role": "user", "content": {"type": "text", "text": "hi"}}
    assert rr.sample("hi") == {"method": "sampling/createMessage", "params": {"messages": [user], "maxTokens": 100}}
    assert rr.sample([user], max_tokens=5, system_prompt="s", temperature=0)["params"] == {
        "messages": [user],
        "maxTokens": 5,
        "systemPrompt": "s",
        "temperature": 0,
    }
    assert rr.list_roots() == {"method": "roots/list", "params": {}}
    assert rr.input_responses() == {} and rr.request_state() is None and rr.client_capabilities() == {}
    assert rr.progress(1) is None


def test_first_round_asks_and_the_retry_completes(tmp_path):
    ex = tool(tmp_path, ASK)
    assert ex.call_tool("t", {"a": "x"}) == {"resultType": "input_required", "inputRequests": {"user_name": ELICIT}}
    # a wrong key asks again (spec rule 5); extra keys are the tool's to ignore
    assert ex.call_tool("t", {"a": "x"}, responses={"wrong": {}})["resultType"] == "input_required"
    r = ex.call_tool("t", {"a": "x"}, responses=ALICE | {"extra": {"action": "accept"}})
    assert r == {"content": [{"type": "text", "text": "Hello, Alice!"}], "isError": False}


def test_state_and_capabilities_reach_the_tool_and_do_not_leak_between_calls(tmp_path):
    code = HEAD + (
        "def f(a):\n"
        "    s = request_state()\n"
        "    if s is None:\n"
        "        raise InputRequired({'c': sample('q', max_tokens=50)} if 'sampling' in client_capabilities()"
        " else {'r': list_roots()}, state={'round': 1})\n"
        "    return {'state': s, 'caps': client_capabilities(), 'resp': input_responses()}\n"
    )
    ex = tool(tmp_path, code)
    r = ex.call_tool("t", {"a": "x"}, client={"sampling": {}})
    assert r["state"] == {"round": 1} and r["inputRequests"]["c"]["method"] == "sampling/createMessage"
    assert list(ex.call_tool("t", {"a": "x"})["inputRequests"]) == ["r"]
    r = ex.call_tool("t", {"a": "x"}, responses={"c": {"role": "assistant"}}, state={"round": 1}, client={"roots": {}})
    assert json.loads(r["content"][0]["text"]) == {
        "state": {"round": 1},
        "caps": {"roots": {}},
        "resp": {"c": {"role": "assistant"}},
    }
    assert rr.request_state() is None and rr.input_responses() == {}  # the context ends with the call


def test_state_only_and_empty_input_required(tmp_path):
    ex = tool(tmp_path, HEAD + "def f(a):\n    raise InputRequired(state='s1')\n")
    assert ex.call_tool("t", {"a": "x"}) == {"resultType": "input_required", "inputRequests": {}, "state": "s1"}
    r = tool(tmp_path, HEAD + "def f(a):\n    raise InputRequired({})\n").call_tool("t", {"a": "x"})
    assert r["isError"] and r["content"][0]["text"] == "input request invalid: needs requests or state"
    r = tool(tmp_path, HEAD + "def f(a):\n    raise InputRequired({'k': {'method': 'ping'}})\n").call_tool(
        "t", {"a": "x"}
    )
    assert (
        r["isError"] and r["content"][0]["text"] == "input request invalid: k is not an elicit, sample or roots request"
    )
    r = tool(tmp_path, HEAD + "def f(a):\n    raise InputRequired({'k': {1, 2}}, state={3})\n").call_tool(
        "t", {"a": "x"}
    )
    assert r["isError"]


@pytest.mark.parametrize("field", ["password", "API_Key", "api-key", "accessToken", "card_number", "client_secret"])
def test_secret_looking_form_field_is_refused(tmp_path, field):
    code = HEAD + (
        f"def f(a):\n    raise InputRequired({{'login': elicit('m', {{'type': 'object', 'properties': "
        f"{{'user': {{'type': 'string'}}, '{field}': {{'type': 'string'}}}}}})}})\n"
    )
    r = tool(tmp_path, code).call_tool("t", {"a": "x"})
    assert r == {
        "content": [
            {"type": "text", "text": f"input request refused: login.{field} looks like a secret; use elicit_url"}
        ],
        "isError": True,
    }


def test_url_mode_may_ask_for_anything(tmp_path):
    code = HEAD + "def f(a):\n    raise InputRequired({'password': elicit_url('Set your API key', 'https://x/key')})\n"
    r = tool(tmp_path, code).call_tool("t", {"a": "x"})
    assert r["inputRequests"]["password"]["params"]["mode"] == "url"


def test_missing_capability(tmp_path):
    code = HEAD + "def f(a):\n    raise MissingCapability({'sampling': {}})\n"
    assert tool(tmp_path, code).call_tool("t", {"a": "x"}) == {
        "resultType": "missing_capability",
        "requiredCapabilities": {"sampling": {}},
    }


def test_resolved_secrets_never_ride_out_in_requests_state_or_progress(tmp_path, monkeypatch):
    monkeypatch.setenv("RAMEN_SECRET_G__K", "hunter2")
    code = HEAD + (
        "def f(a):\n"
        "    progress(1, 2, 'working with ' + a)\n"
        "    q = elicit('token is ' + a, {'type': 'object', 'properties': {}})\n"
        "    raise InputRequired({'q': q}, state={'a': [a]})\n"
    )
    seen = []
    r = tool(tmp_path, code).call_tool("t", {"a": "{{$g.k}}"}, sink=seen.append)
    assert "hunter2" not in json.dumps([r, seen])
    assert r["state"] == {"a": ["***"]} and r["inputRequests"]["q"]["params"]["message"] == "token is ***"
    assert seen == [{"progress": 1, "total": 2, "message": "working with ***"}]


POLICY = """
SEEN = []
def pre(tool, arguments):
    SEEN.append(("pre", arguments))
    if arguments.get("$inputResponses", {}).get("user_name", {}).get("content", {}).get("name") == "Mallory":
        return "no"
def post(tool, arguments, result):
    SEEN.append(("post", arguments, result))
"""


def test_guardrails_pre_every_round_post_only_on_the_final_result(tmp_path):
    write_pkg(tmp_path, "tools", "t", TOOL, ASK)
    (tmp_path / "mcp" / "guardrails.yaml").write_text("engine: policy\ntools:\n  t: {pre: true, post: true}\n")
    (tmp_path / "mcp" / "guardrails").mkdir()
    (tmp_path / "mcp" / "guardrails" / "policy.py").write_text(POLICY)
    guard, errors = guardrails.load(tmp_path)
    assert errors == []
    ex = Executor(load(tmp_path), guard)
    seen = guard.engine._pre.__globals__["SEEN"]
    assert ex.call_tool("t", {"a": "x"})["resultType"] == "input_required"
    assert seen == [("pre", {"a": "x"})]  # no post on an input_required round
    assert ex.call_tool("t", {"a": "x"}, responses=ALICE)["isError"] is False
    assert seen[1] == ("pre", {"a": "x", "$inputResponses": ALICE})
    assert seen[2][0] == "post" and seen[2][2] == [{"type": "text", "text": "Hello, Alice!"}] and len(seen) == 3
    mallory = {"user_name": {"action": "accept", "content": {"name": "Mallory"}}}
    r = ex.call_tool("t", {"a": "x"}, responses=mallory)
    assert r["isError"] and r["_meta"]["ramen"]["guardrail"]["stage"] == "pre" and len(seen) == 4


def test_callable_prompt_rounds(tmp_path):
    code = HEAD + (
        "def f():\n"
        "    r = input_responses()\n"
        "    if 'user_context' not in r:\n"
        "        raise InputRequired({'user_context': elicit('ctx?', {'type': 'object', 'properties': "
        "{'context': {'type': 'string'}}})})\n"
        "    return 'Prompt with context: ' + r['user_context']['content']['context']\n"
    )
    write_pkg(tmp_path, "prompts", "p", PROMPT, code)
    ex = Executor(load(tmp_path))
    r = ex.get_prompt("p", {})
    assert r["resultType"] == "input_required" and list(r["inputRequests"]) == ["user_context"]
    r = ex.get_prompt("p", {}, responses={"user_context": {"action": "accept", "content": {"context": "c1"}}})
    assert r == {"messages": [{"role": "user", "content": {"type": "text", "text": "Prompt with context: c1"}}]}
    write_pkg(tmp_path, "prompts", "p", PROMPT, HEAD + "def f():\n    raise MissingCapability({'roots': {}})\n")
    assert Executor(load(tmp_path)).get_prompt("p", {})["resultType"] == "missing_capability"
    bad = HEAD + "def f():\n    raise InputRequired({'k': elicit('m', {'properties': {'ssn': {}}})})\n"
    write_pkg(tmp_path, "prompts", "p", PROMPT, bad)
    with pytest.raises(ValueError, match="input request refused: k.ssn looks like a secret; use elicit_url"):
        Executor(load(tmp_path)).get_prompt("p", {})


# ---- sidecar protocol -------------------------------------------------------------------------------------------
def serve(server, *msgs):
    out = io.StringIO()
    server.serve(io.StringIO("".join(json.dumps(m) + "\n" for m in msgs)), out)
    return [json.loads(x) for x in out.getvalue().splitlines()]


@pytest.fixture
def server(tmp_path, monkeypatch):
    monkeypatch.setattr("ramen_runtime.rpc.deps.install", lambda b: {"installed": False})
    code = HEAD + (
        "def f(a):\n"
        "    for i in (0, 50, 100):\n"
        "        progress(i, 100, 'step')\n"
        "    progress(100)\n"
        "    if not input_responses():\n"
        "        raise InputRequired({'k': list_roots()}, state=[1])\n"
        "    return {'caps': client_capabilities(), 'state': request_state(), 'resp': input_responses()}\n"
    )
    write_pkg(tmp_path, "tools", "t", TOOL, code)
    write_pkg(tmp_path, "prompts", "p", PROMPT, HEAD + "def f():\n    progress(1)\n    return str(input_responses())\n")
    return Server(tmp_path, load_on_start=True)


def call(rid, **params):
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "method": "runtime.call_tool",
        "params": {"name": "t", "arguments": {"a": "x"}} | params,
    }


def test_call_tool_params_and_progress_lines(server):
    roots = {"k": {"roots": []}}
    out = serve(
        server,
        call(1),
        call("r-2", progress=True, inputResponses=roots, state=[1], client={"capabilities": {"roots": {}}}),
        call(3, progress=False, inputResponses=None),
        {
            "jsonrpc": "2.0",
            "method": "runtime.call_tool",
            "params": {"name": "t", "arguments": {"a": "x"}, "progress": True},
        },
    )
    assert out[0] == {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "resultType": "input_required",
            "inputRequests": {"k": {"method": "roots/list", "params": {}}},
            "state": [1],
        },
    }
    step = {"id": "r-2", "progress": 50, "total": 100, "message": "step"}
    assert out[1] == {"jsonrpc": "2.0", "method": "runtime.progress", "params": step | {"progress": 0}}
    assert out[2]["params"] == step and out[3]["params"]["progress"] == 100
    assert out[4] == {"jsonrpc": "2.0", "method": "runtime.progress", "params": {"id": "r-2", "progress": 100}}
    assert json.loads(out[5]["result"]["content"][0]["text"]) == {"caps": {"roots": {}}, "state": [1], "resp": roots}
    assert out[6]["id"] == 3 and out[6]["result"]["resultType"] == "input_required" and len(out) == 7


def test_get_prompt_params_and_bad_params(server):
    def get(rid, **p):
        return {
            "jsonrpc": "2.0",
            "id": rid,
            "method": "runtime.get_prompt",
            "params": {"name": "p", "arguments": {}} | p,
        }

    out = serve(
        server,
        get(1, inputResponses={"a": {"b": 1}}, progress=True),
        call(2, inputResponses=[1]),
        call(3, inputResponses={"user_name": 12345}),
        call(4, client=[]),
        call(5, client={"capabilities": 1}),
    )
    assert out[0]["method"] == "runtime.progress" and out[0]["params"] == {"id": 1, "progress": 1}
    assert out[1]["result"]["messages"][0]["content"]["text"] == "{'a': {'b': 1}}"
    assert [o["error"]["code"] for o in out[2:]] == [-32602] * 4
    assert "inputResponses" in out[2]["error"]["message"] and "inputResponses.user_name" in out[3]["error"]["message"]


def test_progress_is_a_no_op_without_an_output_stream(server):
    r = server.handle_line(json.dumps(call(1, progress=True, inputResponses={"k": {}})))
    assert r["result"]["isError"] is False


def test_stdout_lines_are_never_interleaved(server):
    class Slow(io.StringIO):
        def write(self, s):
            for ch in s:  # a writer that yields between characters, like a pipe under load
                super().write(ch)
                threading.Event().wait(0)
            return len(s)

    server._out = out = Slow()
    threads = [
        threading.Thread(target=lambda i=i: [server._write({"id": i, "pad": "x" * 200}) for _ in range(20)])
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    lines = out.getvalue().splitlines()
    assert len(lines) == 80 and all(json.loads(x)["pad"] == "x" * 200 for x in lines)


@pytest.mark.parametrize(
    ("field", "refused"),
    [
        ("discard", False),
        ("lessons", False),
        ("card_number", True),
        ("cardNumber", True),
        ("SSN", True),
        ("apiKey", True),
    ],
)
def test_secret_like_fields_match_whole_words_for_card_and_ssn(field, refused):
    from ramen_runtime import context

    assert context._secret_like(field) is refused
