"""Executes loaded packages (CONTRACTS §2 result shapes; §23.3 content, §23.4 input rounds)."""

import json

import jsonschema

from . import context, envfile, guardrails, prompts, secrets
from .content import Block, b64, blocks, text
from .loader import Package, Registry
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

    def tool(self, name: str) -> Package:
        return self._find(self.reg.tools, lambda p: p.name, name)

    def call_tool(self, name: str, arguments: dict, *, responses=None, state=None, client=None, sink=None) -> dict:
        """One round (§23.4): the 0.8 result, or `input_required` / `missing_capability` when the tool raised it."""
        tool = self.tool(name)
        used: list[str] = []
        # §23.4: on a retry the engine sees the client's answers next to the arguments
        seen = arguments | {"$inputResponses": responses} if responses else arguments
        try:
            jsonschema.validate(arguments, tool.schema)
            args = secrets.substitute_args(arguments, used)
            # §21.2 pre hook: after validation and substitution, before the tool; it sees the arguments as written
            # (`{{$group.VAR}}` text), never the substituted values
            if (blocked := self.guard.pre(name, seen)) is not None:
                return blocked
            ctx = {"responses": responses, "state": state, "client": client, "sink": sink}
            result, special = _invoke(tool.func, args, used, ctx)
            if special is not None:  # no post hook on a round that is not the final result
                return special
            content = blocks(result)  # §23.3: blocks go into `content` as given, without structuredContent
            r = {"content": content or [{"type": "text", "text": _text(result)}], "isError": False}
            if content is None and tool.output is not None:  # B5: the declared output schema is enforced on the way out
                structured = {"result": result} if wraps_output(tool.proto["output"]) else result
                try:
                    jsonschema.validate(structured, tool.output)
                except jsonschema.ValidationError as e:
                    where = "/".join(str(p) for p in e.absolute_path) or "output"
                    raise _OutputError(f"invalid output: {where}: {e.message}") from None
                r["structuredContent"] = structured
            # §21.2 post hook: after output validation; a block never returns the tool's output. The engine sees the
            # result with every resolved secret value redacted (the client still gets the tool's own output).
            payload = r["structuredContent"] if "structuredContent" in r else content or r["content"][0]["text"]
            if (blocked := self.guard.post(name, seen, _redacted(payload, used))) is not None:
                return blocked
            return r
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "arguments"
            msg = f"invalid arguments: {where}: {e.message}"
        except (_OutputError, _Refused) as e:
            msg = str(e)
        except Exception as e:  # noqa: BLE001 - user code; message only, no traceback
            msg = secrets.redact(f"{type(e).__name__}: {e}", used + envfile.rendered_values())
        return {"content": [{"type": "text", "text": msg}], "isError": True}

    def read_resource(self, uri: str) -> dict:
        res, variables = self._resource(uri)
        try:
            out = res.func(**variables)
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(f"{type(e).__name__}: {e}") from None
        body = {"blob": b64(out)} if isinstance(out, bytes | bytearray) else {"text": _text(out)}  # §23.3
        return {"contents": [{"uri": uri, "mimeType": res.proto["mime_type"]} | body]}

    def _resource(self, uri: str) -> tuple[Package, dict]:
        """The resource for `uri` and its template variables: an exact URI wins, then templates in name order."""
        for r in self.reg.resources:
            if r.template is None and r.proto["uri"] == uri:
                return r, {}
        for r in self.reg.resources:
            if r.template is not None and (m := r.template.fullmatch(uri)):
                v = m.groupdict()
                if all(v[k] in s["enum"] for k, s in r.proto["input"].items() if "enum" in s):
                    return r, v
        raise KeyError(uri)

    def get_prompt(self, name: str, arguments: dict, *, responses=None, state=None, client=None, sink=None) -> dict:
        p = self._find(self.reg.prompts, lambda p: p.name, name)
        declared = p.proto.get("input", {})
        missing = [a for a in declared if a not in arguments]
        if missing:
            raise ValueError(f"missing prompt arguments: {missing}")
        if p.func is None:
            rendered = prompts.render(p.skill, arguments, p.settings)
            return {"messages": [{"role": "user", "content": {"type": "text", "text": rendered}}]}
        # §23.3 callable prompt: declared arguments as keywords, as written (a prompt's answer goes to the client,
        # so `{{$group.VAR}}` is not resolved here)
        ctx = {"responses": responses, "state": state, "client": client, "sink": sink}
        try:
            out, special = _invoke(p.func, {a: arguments[a] for a in declared}, [], ctx)
            return special if special is not None else {"messages": _messages(out)}
        except _Refused as e:
            raise ValueError(str(e)) from None
        except Exception as e:  # noqa: BLE001
            raise RuntimeError(secrets.redact(f"{type(e).__name__}: {e}", envfile.rendered_values())) from None


class _OutputError(Exception):
    pass


class _Refused(Exception):
    pass


def _invoke(func, kwargs: dict, used: list[str], ctx: dict):
    """Run user code inside its call context → `(result, None)`, or `(None, <§23.4 result>)` when it raised
    `InputRequired` / `MissingCapability`. A refused input request raises `_Refused`."""
    try:
        with context.call(used=used, **ctx):
            return func(**kwargs), None
    except context.InputRequired as e:
        if (why := context.refusal(e)) is not None:
            raise _Refused(why) from None
        return None, context.input_required(e, used)
    except context.MissingCapability as e:
        return None, context.missing_capability(e)


def _messages(out) -> list[dict]:
    """§23.3: a string, a block, or a list of `{"role", "content"}` messages / blocks / strings (role `user`)."""
    msgs = []
    for item in out if isinstance(out, list) else [out]:
        role, content = "user", item
        if isinstance(item, dict) and not isinstance(item, Block) and "role" in item and "content" in item:
            role, content = item["role"], item["content"]
        if isinstance(content, str):
            content = text(content)
        if not isinstance(content, dict):
            raise TypeError("a prompt callable returns a string, a content block, or a list of messages / blocks")
        msgs.append({"role": role, "content": content})
    return msgs


def _redacted(value, used: list[str]):
    """`value` with every string's secret values replaced by `***`, recursively; secrets never reach an engine."""
    return secrets.redact_deep(value, used) if used else value


def _text(value) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)
