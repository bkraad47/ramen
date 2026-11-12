"""CONTRACTS §23.3: content blocks, binary and templated resources, callable prompts."""

import base64

import pytest
from conftest import write_pkg

import ramen_runtime as rr
from ramen_runtime.executor import Executor
from ramen_runtime.loader import load

TOOL = {"type": "tool", "name": "t", "description": "d", "callable": "f", "input": {}}
RES = {
    "type": "resource",
    "name": "r",
    "description": "d",
    "uri": "u://bin",
    "mime_type": "image/png",
    "callable": "f",
    "input": {},
}
PROMPT = {"type": "prompt", "name": "p", "description": "d", "input": {"who": {"type": "string"}}, "callable": "f"}
IMPORT = "from ramen_runtime import text, image, audio, resource\n"


def tool(tmp_path, body, proto=TOOL):
    write_pkg(tmp_path, "tools", "t", proto, IMPORT + body)
    return Executor(load(tmp_path))


def test_builders():
    assert rr.text("hi") == {"type": "text", "text": "hi"}
    assert rr.image(b"\x89PNG", "image/png") == {"type": "image", "data": "iVBORw==", "mimeType": "image/png"}
    assert rr.audio(b"RIFF", "audio/wav") == {"type": "audio", "data": "UklGRg==", "mimeType": "audio/wav"}
    assert rr.resource("u://x", text="t") == {
        "type": "resource",
        "resource": {"uri": "u://x", "mimeType": "text/plain", "text": "t"},
    }
    assert rr.resource("u://x", blob=b"ab", mime="a/b")["resource"] == {
        "uri": "u://x",
        "mimeType": "a/b",
        "blob": "YWI=",
    }
    with pytest.raises(ValueError, match="exactly one"):
        rr.resource("u://x")
    with pytest.raises(ValueError, match="exactly one"):
        rr.resource("u://x", text="a", blob=b"b")


def test_one_block_and_a_list_of_blocks_go_into_content_as_given(tmp_path):
    ex = tool(tmp_path, "def f():\n    return image(b'x', 'image/png')\n")
    assert ex.call_tool("t", {}) == {
        "content": [{"type": "image", "data": "eA==", "mimeType": "image/png"}],
        "isError": False,
    }
    body = "def f():\n    return [text('a'), audio(b'x', 'audio/wav'), resource('u://r', text='{}', mime='a/json')]\n"
    r = tool(tmp_path, body).call_tool("t", {})
    assert [b["type"] for b in r["content"]] == ["text", "audio", "resource"] and "structuredContent" not in r


def test_block_result_skips_the_output_schema_and_plain_dicts_stay_json(tmp_path):
    ex = tool(tmp_path, "def f():\n    return text('hi')\n", {**TOOL, "output": {"type": "number"}})
    assert ex.call_tool("t", {}) == {"content": [{"type": "text", "text": "hi"}], "isError": False}
    # 0.8 behaviour: a hand-written dict or a mixed list is a JSON value, not content
    ex = tool(tmp_path, "def f():\n    return {'type': 'text', 'text': 'x'}\n")
    assert ex.call_tool("t", {})["content"][0]["text"] == '{"type": "text", "text": "x"}'
    ex = tool(tmp_path, "def f():\n    return [text('a'), 1]\n")
    assert ex.call_tool("t", {})["content"][0]["text"] == '[{"type": "text", "text": "a"}, 1]'
    assert tool(tmp_path, "def f():\n    return []\n").call_tool("t", {})["content"][0]["text"] == "[]"


def test_bytes_resource_is_a_blob(tmp_path):
    write_pkg(tmp_path, "resources", "r", RES, "def f():\n    return b'\\x00\\x01'\n")
    r = Executor(load(tmp_path)).read_resource("u://bin")
    assert r == {
        "contents": [{"uri": "u://bin", "mimeType": "image/png", "blob": base64.b64encode(b"\x00\x01").decode()}]
    }


def test_template_resource_is_matched_on_read(tmp_path):
    proto = {
        **RES,
        "uri": "test://template/{id}/data",
        "mime_type": "application/json",
        "input": {"id": {"type": "string"}},
    }
    write_pkg(tmp_path, "resources", "r", proto, "def f(id):\n    return {'id': id, 'kind': type(id).__name__}\n")
    exact = {**RES, "name": "s", "uri": "test://template/static/data", "mime_type": "text/plain"}
    write_pkg(tmp_path, "resources", "s", exact, "def f():\n    return 'static wins'\n")
    kinds = {**RES, "name": "k", "uri": "k://{kind}", "input": {"kind": {"enum": ["a", "b"]}}}
    write_pkg(tmp_path, "resources", "k", kinds, "def f(kind):\n    return kind\n")
    ex = Executor(load(tmp_path))
    r = ex.read_resource("test://template/123/data")["contents"][0]
    assert r == {
        "uri": "test://template/123/data",
        "mimeType": "application/json",
        "text": '{"id": "123", "kind": "str"}',
    }
    assert ex.read_resource("test://template/static/data")["contents"][0]["text"] == "static wins"
    assert ex.read_resource("k://a")["contents"][0]["text"] == "a"
    for missing in ("test://template/1/2/data", "test://template//data", "test://template/1/dataX", "k://c"):
        with pytest.raises(KeyError):
            ex.read_resource(missing)
    with pytest.raises(KeyError):
        ex.read_resource("test://template/{id}/data")  # the template text itself is not a resource


def prompt(tmp_path, body, proto=PROMPT):
    write_pkg(tmp_path, "prompts", "p", proto, IMPORT + body)
    return Executor(load(tmp_path))


def test_callable_prompt_shapes(tmp_path):
    user = {"role": "user", "content": {"type": "text", "text": "hi bo"}}
    assert prompt(tmp_path, "def f(who):\n    return 'hi ' + who\n").get_prompt("p", {"who": "bo", "x": 1}) == {
        "messages": [user]
    }
    r = prompt(tmp_path, "def f(who):\n    return image(b'x', 'image/png')\n").get_prompt("p", {"who": "bo"})
    assert r["messages"] == [{"role": "user", "content": {"type": "image", "data": "eA==", "mimeType": "image/png"}}]
    body = (
        "def f(who):\n    return [resource(who, text='body'), {'role': 'assistant', 'content': 'ok'},"
        " {'role': 'user', 'content': text('go')}, 'plain']\n"
    )
    r = prompt(tmp_path, body).get_prompt("p", {"who": "u://x"})
    assert r["messages"] == [
        {
            "role": "user",
            "content": {"type": "resource", "resource": {"uri": "u://x", "mimeType": "text/plain", "text": "body"}},
        },
        {"role": "assistant", "content": {"type": "text", "text": "ok"}},
        {"role": "user", "content": {"type": "text", "text": "go"}},
        {"role": "user", "content": {"type": "text", "text": "plain"}},
    ]


def test_callable_prompt_errors(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="missing prompt arguments"):
        prompt(tmp_path, "def f(who):\n    return who\n").get_prompt("p", {})
    with pytest.raises(RuntimeError, match="TypeError: a prompt callable returns"):
        prompt(tmp_path, "def f(who):\n    return 5\n").get_prompt("p", {"who": "x"})
    with pytest.raises(RuntimeError, match="TypeError"):
        prompt(tmp_path, "def f(who):\n    return [5]\n").get_prompt("p", {"who": "x"})
    monkeypatch.setattr("ramen_runtime.envfile._VALUES", ["s3cr3t"], raising=False)
    with pytest.raises(RuntimeError, match=r"ValueError: no \*\*\*$"):
        prompt(tmp_path, "def f(who):\n    raise ValueError('no s3cr3t')\n").get_prompt("p", {"who": "x"})
