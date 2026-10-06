"""C12 (0.7.2, drift): every `tools/call` access-log line carries `args` — the first 12 hex of sha256 over the
canonical JSON of `params.arguments` (`sort_keys`, no spaces), `""` when the call sent none — and never the arguments
themselves. On real node processes, both transports. The console's `drift.py` counts repeats on this field."""

import hashlib
import json
import time

import pytest

from ramen_tests.localnode import LocalNode
from ramen_tests.mcp_client import JsonRpcError

pytestmark = pytest.mark.conformance
TRANSPORTS = ("grpc", "http")
CALC = "demo_calculator_tool"
MARKER = 7319  # a value that should appear in no log line
A = {"var1": MARKER, "var2": 3, "func": "add"}
B = {"var1": 1, "var2": 1, "func": "multiply"}


def expected(arguments: dict | None) -> str:
    if arguments is None:
        return ""
    canon = json.dumps(arguments, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canon.encode()).hexdigest()[:12]


@pytest.fixture(scope="module")
def node():
    with LocalNode() as n:
        n.wait_serving()
        yield n


def call_lines(node, since: int, want: int, timeout: float = 5) -> list[dict]:
    deadline = time.monotonic() + timeout
    while True:
        lines = [x for x in node.log_lines() if x.get("msg") == "mcp" and x.get("method") == "tools/call"][since:]
        if len(lines) >= want or time.monotonic() > deadline:
            return lines
        time.sleep(0.2)


def call(client, params: dict):
    try:
        return client.request("tools/call", params)
    except JsonRpcError as e:  # a call without arguments is the runtime's business; the log line exists either way
        return e


@pytest.mark.parametrize("transport", TRANSPORTS)
def test_tools_call_lines_carry_a_hash_of_the_arguments_never_the_values(node, transport):
    c = node.for_transport(transport)
    c.initialize()
    before = len([x for x in node.log_lines() if x.get("msg") == "mcp" and x.get("method") == "tools/call"])
    for params in (
        {"name": CALC, "arguments": A},
        {"name": CALC, "arguments": {"func": "add", "var2": 3, "var1": MARKER}},  # same arguments, other key order
        {"name": CALC, "arguments": B},
        {"name": CALC},  # no `arguments` at all
        {"name": CALC, "arguments": {}},
    ):
        call(c, params)
    lines = call_lines(node, before, 5)
    assert len(lines) == 5, lines
    if "args" not in lines[0]:
        pytest.xfail("waiting for worker-agent (C12): no `args` field on tools/call log lines")
    got = [x["args"] for x in lines]
    assert got == [expected(A), expected(A), expected(B), "", expected({})], got
    assert got[0] != got[2] and got[0] == got[1]
    assert all(x["transport"] == transport and x["name"] == CALC for x in lines)
    text = node.log.read_text()
    assert str(MARKER) not in text and '"var1"' not in text, "argument values never reach the log"
