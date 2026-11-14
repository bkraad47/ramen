"""C1 / C4 (0.7.0): golden cases from a group repo's `mcp/tests.yaml`, run against the canary before promotion.

    cases:
      - tool: demo_calculator_tool
        args: {operation: add, a: 40, b: 2}
        expect: {subset: {content: [{type: text, text: "42"}]}}   # or exact: {...} or text_contains: "42"

`args` may reference a group secret as `{{$group.VAR}}`; it is resolved by the console before the call.

§23.4 (1.0.0), a tool that asks the client for input (MRTR):

      - tool: demo_wipe_tool
        args: {path: /tmp/x}
        respond: {confirm: {action: accept, content: {ok: true}}}   # <key of the input request>: <the response>
        expect: {text_contains: "deleted"}                          # applies to the final result
      - tool: demo_wipe_tool
        name: wipe asks first
        args: {path: /tmp/x}
        expect: {input_required: [confirm]}                         # asserts the first round: exactly these keys

Such a case is called in the 2026-07-28 era, declaring elicitation (form + url), sampling and roots; on
`input_required` the call is retried once with `respond` and the returned `requestState`. Every other case is sent
exactly as before.
"""

import re
from collections.abc import Callable

import yaml

from . import __version__

PATH = "mcp/tests.yaml"
EXPECTS = ("subset", "exact", "text_contains", "input_required")
MODERN = "2026-07-28"
NS = "io.modelcontextprotocol/"
CAPABILITIES = {"elicitation": {"form": {}, "url": {}}, "sampling": {}, "roots": {}}
REF = re.compile(r"\{\{\$([a-z][a-z0-9-]*)\.([A-Z][A-Z0-9_]*)\}\}")


class GoldenError(RuntimeError):
    """A tests.yaml the console cannot run, or a secret it cannot resolve."""


def parse(text: str | None) -> list[dict]:
    """The cases, each with a `name` (its own or the tool's), `tool`, `args`, `expect`."""
    if not text or not text.strip():
        return []
    try:
        doc = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise GoldenError(f"tests.yaml is not valid YAML: {e}") from None
    if not isinstance(doc, dict):
        raise GoldenError("tests.yaml must be a mapping with `cases`")
    cases = doc.get("cases") or []
    if not isinstance(cases, list):
        raise GoldenError("`cases` must be a list")
    out = []
    for i, c in enumerate(cases, 1):
        if not isinstance(c, dict) or not c.get("tool"):
            raise GoldenError(f"case {i}: `tool` is required")
        name = str(c.get("name") or c["tool"])
        args = c.get("args") or {}
        if not isinstance(args, dict):
            raise GoldenError(f"case {name}: `args` must be a mapping")
        expect = c.get("expect") or {}
        keys = [k for k in EXPECTS if k in expect] if isinstance(expect, dict) else []
        if not keys:
            raise GoldenError(f"case {name}: `expect` needs subset, exact or text_contains")
        if len(keys) != 1:
            raise GoldenError(f"case {name}: `expect` needs exactly one of {', '.join(EXPECTS)}")
        want = expect[keys[0]]
        if keys[0] == "input_required" and not (
            isinstance(want, list) and want and all(isinstance(k, str) for k in want)
        ):
            raise GoldenError(f"case {name}: `input_required` must be a list of keys")
        case = {"name": name, "tool": str(c["tool"]), "args": args, "expect": {keys[0]: want}}
        if "respond" in c:
            respond = c["respond"]
            if not isinstance(respond, dict):
                raise GoldenError(f"case {name}: `respond` must be a mapping")
            for k, v in respond.items():
                if not isinstance(v, dict):
                    raise GoldenError(f"case {name}: `respond.{k}` must be a mapping")
            case["respond"] = {str(k): v for k, v in respond.items()}
        out.append(case)
    return out


def modern(case: dict) -> bool:
    """§23.4: only a case that answers or expects an input request is called in the 2026-07-28 era."""
    return "respond" in case or "input_required" in (case.get("expect") or {})


def resolve_args(value, lookup: Callable[[str], str | None]):
    """Replace every `{{$group.VAR}}` in strings (nested lists/mappings included) via `lookup("group.VAR")`."""
    if isinstance(value, dict):
        return {k: resolve_args(v, lookup) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_args(v, lookup) for v in value]
    if isinstance(value, str):

        def sub(m):
            v = lookup(f"{m.group(1)}.{m.group(2)}")
            if v is None:
                raise GoldenError(f"secret {m.group(1)}.{m.group(2)} is not set for this group")
            return v

        return REF.sub(sub, value)
    return value


def request(case: dict, rpc_id: int, responses: dict | None = None, state: str | None = None) -> dict:
    params = {"name": case["tool"], "arguments": case.get("args") or {}}
    if modern(case):
        params["_meta"] = {
            f"{NS}protocolVersion": MODERN,
            f"{NS}clientInfo": {"name": "ramen-console", "version": __version__},
            f"{NS}clientCapabilities": CAPABILITIES,
        }
    if responses is not None:
        params["inputResponses"] = responses
        if state is not None:
            params["requestState"] = state
    return {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call", "params": params}


def _asked(response: dict) -> list[str] | None:
    """The keys of the input requests when the result is `input_required`, else None."""
    result = response.get("result") if isinstance(response, dict) else None
    if "error" in response or not isinstance(result, dict) or result.get("resultType") != "input_required":
        return None
    return sorted(str(k) for k in result.get("inputRequests") or {})


async def run(case: dict, call, rpc_id: int) -> str | None:
    """Call the case's tool through `call(jsonrpc) -> response`; None when it passes, else why it failed."""
    response = await call(request(case, rpc_id))
    asked = _asked(response)
    if "input_required" in case["expect"]:
        if "error" in response or "result" not in response:
            return check_response(case, response)
        want = sorted(case["expect"]["input_required"])
        if asked == want:
            return None
        got = f"it asked for {', '.join(asked)}" if asked is not None else "it answered without asking"
        return f"input_required: expected the tool to ask for {', '.join(want)}; {got}"
    if asked is not None and "respond" not in case:
        return f"the tool asked for input ({', '.join(asked)}) and the case has no `respond`"
    if asked is not None:
        response = await call(request(case, rpc_id, case["respond"], response["result"].get("requestState")))
        if (asked := _asked(response)) is not None:
            return f"the tool asked for input again after the responses were sent ({', '.join(asked)})"
    if modern(case) and isinstance(response.get("result"), dict):
        response = {**response, "result": _plain(response["result"])}
    return check_response(case, response)


def _plain(result: dict) -> dict:
    """A modern result without its envelope (§23.1): `resultType` and the server info in `_meta`."""
    out = {k: v for k, v in result.items() if k != "resultType"}
    if isinstance(out.get("_meta"), dict):
        meta = {k: v for k, v in out["_meta"].items() if k != f"{NS}serverInfo"}
        out = {**out, "_meta": meta} if meta else {k: v for k, v in out.items() if k != "_meta"}
    return out


def check_response(case: dict, response: dict) -> str | None:
    """None when the JSON-RPC response satisfies the case's `expect`, else what differed."""
    if "error" in response:
        e = response["error"] or {}
        return f"error {e.get('code')}: {e.get('message')}"
    if "result" not in response:
        return "no result in the response"
    result = response["result"] or {}
    why = check(case["expect"], result)
    if why is None or not isinstance(result, dict) or not result.get("isError"):
        return why
    # §21.5: the tool answered an error — its text comes first (a rail's verdict reads `guardrail blocked: <stage>:
    # <message>`, §21.2), the expectation diff after it
    text = "".join(str(c.get("text", "")) for c in result.get("content", []) if isinstance(c, dict))
    rail = ((result.get("_meta") or {}).get("ramen") or {}).get("guardrail")
    where = f" (guardrail {rail.get('stage')}, {rail.get('engine')})" if isinstance(rail, dict) else ""
    return f"tool error: {text}{where}; {why}"


def check(expect: dict, got) -> str | None:
    if "exact" in expect:
        return None if expect["exact"] == got else f"exact: expected {expect['exact']!r}, got {got!r}"
    if "text_contains" in expect:
        text = "".join(str(c.get("text", "")) for c in (got or {}).get("content", []) if isinstance(c, dict))
        return (
            None if expect["text_contains"] in text else f"text_contains: {expect['text_contains']!r} not in {text!r}"
        )
    return _subset(expect["subset"], got, "")


def _subset(want, got, path) -> str | None:
    if isinstance(want, dict):
        if not isinstance(got, dict):
            return f"{path or '$'}: expected a mapping, got a {_kind(got)}"
        for k, v in want.items():
            p = f"{path}.{k}" if path else k
            if k not in got:
                return f"{p}: expected {v!r}, got nothing"
            if (r := _subset(v, got[k], p)) is not None:
                return r
        return None
    if isinstance(want, list):
        if not isinstance(got, list):
            return f"{path or '$'}: expected a list, got a {_kind(got)}"
        if len(got) < len(want):
            return f"{path or '$'}: expected at least {len(want)} items, got {len(got)}"
        for i, v in enumerate(want):
            if (r := _subset(v, got[i], f"{path}[{i}]")) is not None:
                return r
        return None
    return None if want == got else f"{path or '$'}: expected {want!r}, got {got!r}"


def _kind(v) -> str:
    return {dict: "mapping", list: "list", str: "string"}.get(type(v), type(v).__name__)
