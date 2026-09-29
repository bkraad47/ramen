"""U4 / CONTRACTS §12.1: the Logs page needs parsed entries — newest first, consumer, timestamp and outcome."""

import json

from ramen_console.keyid import key_id
from ramen_console.logview import EAGER, parse_log

OK = json.dumps(
    {
        "ts": "2026-09-29T01:00:00Z",
        "level": "info",
        "msg": "mcp",
        "ip": "10.0.0.1",
        "group": "demo",
        "zone": "local",
        "env": "dev",
        "method": "tools/call",
        "name": "demo_calculator_tool",
        "status": "ok",
        "grpc_code": "OK",
        "ms": 3,
        "key_id": key_id("rmk_known"),
    }
)
DENIED = json.dumps(
    {
        "ts": "2026-09-29T01:00:01Z",
        "level": "info",
        "msg": "mcp",
        "method": "tools/list",
        "name": None,
        "status": "denied",
        "grpc_code": "UNAUTHENTICATED",
        "key_id": None,
    }
)
ERROR = json.dumps({"ts": "2026-09-29T01:00:02Z", "msg": "mcp", "method": "tools/call", "status": "error"})


def test_newest_entry_comes_first_and_is_selected():
    entries = parse_log("\n".join([OK, DENIED, ERROR]))
    assert [e["ts"] for e in entries] == ["2026-09-29T01:00:02Z", "2026-09-29T01:00:01Z", "2026-09-29T01:00:00Z"]
    assert entries[0]["selected"] is True
    assert not any(e["selected"] for e in entries[1:])


def test_every_row_has_consumer_timestamp_method_and_outcome():
    e = parse_log(OK)[0]
    assert e["ts"] == "2026-09-29T01:00:00Z"
    assert e["method"] == "tools/call"
    assert e["name"] == "demo_calculator_tool"
    assert e["outcome"] == "success"
    assert e["key_id"] == key_id("rmk_known")


def test_outcome_is_success_only_for_status_ok():
    assert parse_log(OK)[0]["outcome"] == "success"
    assert parse_log(DENIED)[0]["outcome"] == "failure"
    assert parse_log(ERROR)[0]["outcome"] == "failure"


def test_consumer_resolves_to_a_key_name_the_store_knows():
    names = {key_id("rmk_known"): "llm-agent"}
    assert parse_log(OK, names)[0]["consumer"] == "llm-agent"


def test_unknown_key_id_falls_back_to_the_id_itself():
    assert parse_log(OK)[0]["consumer"] == key_id("rmk_known")


def test_a_call_with_no_key_reads_as_anonymous():
    assert parse_log(DENIED)[0]["consumer"] == "anonymous"


def test_non_json_lines_are_kept_verbatim_and_never_crash():
    entries = parse_log("plain worker startup line\n" + OK + "\n{not json")
    assert len(entries) == 3
    other = [e for e in entries if e["method"] is None]
    assert {e["raw"] for e in other} == {"plain worker startup line", "{not json"}
    assert all(e["outcome"] == "unknown" for e in other)


def test_a_line_that_is_not_a_call_is_neither_success_nor_failure():
    """Worker startup and sidecar lines carry no `status`; calling them failures would be a lie."""
    loaded = json.dumps({"ts": "2026-09-29T00:00:00Z", "level": "info", "msg": "loaded", "tools": 1})
    e = parse_log(loaded)[0]
    assert e["outcome"] == "unknown"
    assert e["consumer"] == "loaded"
    assert e["consumer"] != "anonymous"


def test_blank_lines_are_dropped():
    assert parse_log("\n\n  \n" + OK) == parse_log(OK)


def test_body_is_pretty_printed_json_for_structured_lines():
    body = parse_log(OK)[0]["body"]
    assert "\n" in body
    assert json.loads(body)["name"] == "demo_calculator_tool"


def test_split_puts_the_newest_fifteen_first():
    lines = [json.dumps({"ts": f"t{i:03d}", "msg": "mcp", "status": "ok"}) for i in range(40)]
    entries = parse_log("\n".join(lines))
    assert len(entries[:EAGER]) == 15
    assert entries[0]["ts"] == "t039"
    assert entries[EAGER]["ts"] == "t024"


def test_empty_log_parses_to_nothing():
    assert parse_log("") == []
    assert parse_log(None) == []
