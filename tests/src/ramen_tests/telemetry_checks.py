"""CONTRACTS §26.3 through a deployed zone (kind and cloud share these): the report gate, `telemetry/report`, and the
client events as CLIENT spans in Jaeger. `m` is a consenting modern client whose node reports `version`."""

import json
import secrets
import time

import httpx

from .mcp_client import TELEMETRY_META, telemetry_event, telemetry_report
from .modern import Modern, strip_envelope

CONSENT = "ramen_telemetry_consent"
ACCEPT = {CONSENT: {"action": "accept", "content": {"accept": True}}}
STALE = "000000000000"


def consenting(m: Modern, tool: str, arguments: dict | None = None, **kw) -> dict:
    """`tools/call`, accepting the telemetry notice when the pod that answers has no record of this caller yet."""
    first = m.call(tool, arguments or {}, **kw)
    if first.get("resultType") == "input_required" and CONSENT in first.get("inputRequests", {}):
        return m.call(tool, arguments or {}, inputResponses=ACCEPT, requestState=first["requestState"], **kw)
    return first


def gated_without_report(m: Modern, tool: str, arguments: dict, telemetry: str | None, attempts: int = 6):
    """The reply to a gated call carrying `telemetry` (None: no report) once the pod that answers has this caller's
    consent — behind a load balancer the consent may have to be given to more than one pod first."""
    for _ in range(attempts):
        r = m.with_telemetry(telemetry).call_raw(tool, arguments)
        asked = (r.result or {}).get("resultType") == "input_required" and CONSENT in (r.result or {}).get(
            "inputRequests", {}
        )
        if not asked:
            return r
        m.call(tool, arguments, inputResponses=ACCEPT, requestState=r.result["requestState"])
    raise AssertionError("every attempt was answered with the consent prompt")


def check_gate(m: Modern, version: str, tool: str, arguments: dict) -> None:
    """Missing → -32024 (HTTP 428) naming the version; stale → -32025 (428); the right one served; lists open."""
    assert version and len(version) == 12, version
    consenting(m, tool, arguments)
    r = gated_without_report(m, tool, arguments, None)
    assert (r.code, r.error["message"]) == (-32024, "telemetry report required"), r.message
    assert r.error["data"] == {"version": version} and r.status in (None, 428), (r.status, r.error)
    r = gated_without_report(m, tool, arguments, STALE)
    assert (r.code, r.error["message"]) == (-32025, "telemetry config changed: sign in again"), r.message
    assert r.error["data"] == {"version": version} and r.status in (None, 428), (r.status, r.error)
    r = gated_without_report(m, tool, arguments, version)
    assert r.result and r.result.get("resultType") == "complete", r.message
    assert m.with_telemetry(None).request("tools/list")["tools"], "lists are never gated"


def check_report_method(m: Modern, version: str) -> None:
    got = m.request("telemetry/report", telemetry_report(version, [telemetry_event(kind="other", name="between")]))
    assert strip_envelope(got) == {"accepted": 1, "version": version}, got
    assert m.raw("telemetry/report", telemetry_report(STALE)).code == -32025


def call_with_events(m: Modern, tool: str, arguments: dict, version: str, reasoning: str) -> tuple[str, dict]:
    """One traced call carrying two client events; returns (trace id, the reply)."""
    trace_id, parent = secrets.token_hex(16), secrets.token_hex(8)
    events = [
        telemetry_event(kind="llm", name="test-model", ms=42, ok=True, reasoning=reasoning),
        telemetry_event(kind="tool", name="local_grep", ms=7, ok=False),
    ]
    consenting(m, tool, arguments)
    r = gated_without_report(m, tool, arguments, version)  # the pod that answers knows this caller
    assert r.result, r.message
    r = m.send(
        m.body(
            "tools/call",
            {
                "name": tool,
                "arguments": arguments,
                "_meta": {TELEMETRY_META: telemetry_report(version, events, seq=77)},
            },
        ),
        headers={"traceparent": f"00-{trace_id}-{parent}-01"},
    )
    assert r.result and r.result.get("resultType") == "complete", r.message
    return trace_id, r


def jaeger_trace(jaeger: str, trace_id: str, timeout: float = 120) -> dict:
    deadline, last = time.monotonic() + timeout, None
    while time.monotonic() < deadline:
        resp = httpx.get(f"{jaeger.rstrip('/')}/api/traces/{trace_id}", timeout=15)
        data = resp.json().get("data") if resp.status_code == 200 else None
        last = data[0] if data and data[0].get("spans") else None
        if last and any(s["operationName"].startswith("client ") for s in last["spans"]):
            return last
        time.sleep(5)
    raise AssertionError(f"trace {trace_id} with client spans not in Jaeger after {timeout}s (last={last!r})")


def tag(span: dict, key: str):
    return next((t["value"] for t in span.get("tags", []) if t["key"] == key), None)


def check_client_spans(trace: dict, group: str, zone: str, reasoning: str, user: str | None = None) -> None:
    server = [s for s in trace["spans"] if s["operationName"] == "mcp tools/call"]
    assert len(server) == 1, [s["operationName"] for s in trace["spans"]]
    clients = {s["operationName"]: s for s in trace["spans"] if s["operationName"].startswith("client ")}
    assert set(clients) == {"client llm test-model", "client tool local_grep"}, sorted(clients)
    llm = clients["client llm test-model"]
    assert any(r.get("spanID") == server[0]["spanID"] for r in llm.get("references", [])), "under the call's span"
    assert tag(llm, "span.kind") == "client", llm["tags"]
    assert (tag(llm, "ramen.group"), tag(llm, "ramen.zone")) == (group, zone)
    assert tag(llm, "ramen.reasoning") == reasoning and tag(llm, "ramen.event.kind") == "llm"
    assert str(tag(llm, "ramen.event.seq")) == "77"
    for key in ("ramen.client.ip", "ramen.user", "ramen.worker", "ramen.transport", "ramen.key_id"):
        assert tag(llm, key), f"{key} missing: {llm['tags']}"
    if user:
        assert tag(llm, "ramen.user") == user, tag(llm, "ramen.user")
    assert llm["duration"] == 42_000, llm["duration"]  # Jaeger: microseconds
    assert tag(clients["client tool local_grep"], "error") in (True, "true"), "ok: false → error"
    assert reasoning not in json.dumps(server[0]), "reasoning is on the client span only"
