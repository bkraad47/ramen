"""Executes loaded packages (CONTRACTS §2 result shapes)."""

import json

import jsonschema

from . import envfile, guardrails, prompts, secrets
from .loader import Registry
from .proto import wraps_output


class Executor:
    def __init__(self, reg: Registry, guard: guardrails.Guard | None = None):
        self.reg = reg
        self.guard = guard or guardrails.Guard(guardrails.Config())

    def _find(self, items, key, value):
        for p in items:
            if key(p) == value:
                return p
        raise KeyError(value)

    def call_tool(self, name: str, arguments: dict) -> dict:
        tool = self._find(self.reg.tools, lambda p: p.name, name)
        used: list[str] = []
        try:
            jsonschema.validate(arguments, tool.schema)
            args = secrets.substitute_args(arguments, used)
            # §21.2 pre hook: after validation and substitution, before the tool; it sees the arguments as written
            # (`{{$group.VAR}}` text), never the substituted values
            if (blocked := self.guard.pre(name, arguments)) is not None:
                return blocked
            result = tool.func(**args)
            r = {"content": [{"type": "text", "text": _text(result)}], "isError": False}
            if tool.output is not None:  # B5: the declared output schema is enforced on the way out
                structured = {"result": result} if wraps_output(tool.proto["output"]) else result
                try:
                    jsonschema.validate(structured, tool.output)
                except jsonschema.ValidationError as e:
                    where = "/".join(str(p) for p in e.absolute_path) or "output"
                    raise _OutputError(f"invalid output: {where}: {e.message}") from None
                r["structuredContent"] = structured
            # §21.2 post hook: after output validation; a block never returns the tool's output. The engine sees the
            # result with every resolved secret value redacted (the client still gets the tool's own output).
            payload = r["structuredContent"] if "structuredContent" in r else r["content"][0]["text"]
            if (blocked := self.guard.post(name, arguments, _redacted(payload, used))) is not None:
                return blocked
            return r
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "arguments"
            msg = f"invalid arguments: {where}: {e.message}"
        except _OutputError as e:
            msg = str(e)
        except Exception as e:  # noqa: BLE001 - user code; message only, no traceback
            msg = secrets.redact(f"{type(e).__name__}: {e}", used + envfile.rendered_values())
        return {"content": [{"type": "text", "text": msg}], "isError": True}

    def read_resource(self, uri: str) -> dict:
        res = self._find(self.reg.resources, lambda p: p.proto["uri"], uri)
        try:
            text = _text(res.func())
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{type(e).__name__}: {e}") from None
        return {"contents": [{"uri": uri, "mimeType": res.proto["mime_type"], "text": text}]}

    def get_prompt(self, name: str, arguments: dict) -> dict:
        p = self._find(self.reg.prompts, lambda p: p.name, name)
        missing = [a for a in p.proto.get("input", {}) if a not in arguments]
        if missing:
            raise ValueError(f"missing prompt arguments: {missing}")
        text = prompts.render(p.skill, arguments, p.settings)
        return {"messages": [{"role": "user", "content": {"type": "text", "text": text}}]}


class _OutputError(Exception):
    pass


def _redacted(value, used: list[str]):
    """`value` with every string's secret values replaced by `***`, recursively; secrets never reach an engine."""
    if not used:
        return value
    if isinstance(value, str):
        return secrets.redact(value, used)
    if isinstance(value, dict):
        return {k: _redacted(v, used) for k, v in value.items()}
    if isinstance(value, list):
        return [_redacted(v, used) for v in value]
    return value


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)
