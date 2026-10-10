"""C1 / C4 (0.7.0): golden cases from a group repo's `mcp/tests.yaml`, run against the canary before promotion.

    cases:
      - tool: demo_calculator_tool
        args: {operation: add, a: 40, b: 2}
        expect: {subset: {content: [{type: text, text: "42"}]}}   # or exact: {...} or text_contains: "42"

`args` may reference a group secret as `{{$group.VAR}}`; it is resolved by the console before the call.
"""

import re
from collections.abc import Callable

import yaml

PATH = "mcp/tests.yaml"
EXPECTS = ("subset", "exact", "text_contains")
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
            raise GoldenError(f"case {name}: `expect` needs exactly one of subset, exact, text_contains")
        out.append({"name": name, "tool": str(c["tool"]), "args": args, "expect": {keys[0]: expect[keys[0]]}})
    return out


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


def request(case: dict, rpc_id: int) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": rpc_id,
        "method": "tools/call",
        "params": {"name": case["tool"], "arguments": case.get("args") or {}},
    }


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
