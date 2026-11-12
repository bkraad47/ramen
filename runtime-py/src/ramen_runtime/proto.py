"""Proto JSON validation (CONTRACTS §1, §23.2, §23.3) and MCP inputSchema / outputSchema derivation (§2)."""

import re

import jsonschema
from jsonschema import Draft202012Validator

KINDS = {"tools": "tool", "resources": "resource", "prompts": "prompt"}
_TYPES = ["number", "string", "integer", "boolean"]
_PARAM = {
    "type": "object",
    "properties": {
        "type": {"enum": _TYPES},
        "enum": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "description": {"type": "string"},
        "header": {"type": "string", "minLength": 1},
    },
    "oneOf": [{"required": ["type"]}, {"required": ["enum"]}],
    "additionalProperties": False,
}
_COMMON = {
    "type": {"enum": list(KINDS.values())},
    "name": {"type": "string", "pattern": r"^[A-Za-z_][A-Za-z0-9_]*$"},
    "description": {"type": "string"},
    "input": {"type": "object", "additionalProperties": _PARAM},
}
_CALLABLE = {"callable": {"type": "string", "minLength": 1}, "output": {"type": "object"}, "error": {"type": "object"}}
SCHEMAS = {
    "tool": {
        "type": "object",
        "properties": {**_COMMON, **_CALLABLE, "task": {"type": "boolean"}},
        "required": ["type", "name", "callable", "input"],
    },
    "resource": {
        "type": "object",
        "properties": {
            **_COMMON,
            **_CALLABLE,
            "uri": {"type": "string", "minLength": 1},
            "mime_type": {"type": "string"},
        },
        "required": ["type", "name", "callable", "input", "uri", "mime_type"],
    },
    "prompt": {
        "type": "object",
        "properties": {
            **_COMMON,
            "skill": {"type": "string", "minLength": 1},
            "callable": {"type": "string", "minLength": 1},
            "settings": {"type": "string"},
        },
        "required": ["type", "name", "input"],
    },
}


_TOKEN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")  # RFC 9110 token
_VAR = re.compile(r"\{([^{}]*)\}")
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class ProtoError(ValueError):
    pass


def validate_proto(proto: dict, folder: str, name: str, stem: str) -> None:
    kind = proto.get("type") if isinstance(proto, dict) else None
    if kind not in SCHEMAS:
        raise ProtoError(f"type must be one of {sorted(SCHEMAS)}, got {kind!r}")
    if kind != KINDS[folder]:
        raise ProtoError(f"type {kind!r} does not match folder {folder!r}")
    if proto.get("name") != name or stem != name:
        raise ProtoError(f"folder {name!r}, json name {proto.get('name')!r} and file stem {stem!r} must all match")
    try:
        jsonschema.validate(proto, SCHEMAS[kind])
    except jsonschema.ValidationError as e:
        where = "/".join(str(p) for p in e.absolute_path) or "proto"
        raise ProtoError(f"{where}: {e.message}") from None
    if kind == "prompt" and ("skill" in proto) == ("callable" in proto):
        raise ProtoError("proto: a prompt needs exactly one of 'skill' or 'callable'")
    _check_headers(kind, proto["input"])
    if kind == "resource":
        _check_template(proto["uri"], proto["input"])
    if kind == "tool" and "output" in proto:
        try:
            Draft202012Validator.check_schema(proto["output"])
        except jsonschema.SchemaError as e:
            raise ProtoError(f"output: not a valid JSON Schema: {e.message}") from None


def _check_headers(kind: str, params: dict) -> None:
    """§23.2: `header` on a tool parameter → `Mcp-Param-<Name>`; a bad one fails the package."""
    seen: set[str] = set()
    for pname, spec in params.items():
        if (h := spec.get("header")) is None:
            continue
        if kind != "tool":
            raise ProtoError(f"input/{pname}: header is only for tool parameters")
        if spec.get("type") == "number":
            raise ProtoError(f"input/{pname}: header needs type string, integer, boolean or an enum")
        if not _TOKEN.match(h):
            raise ProtoError(f"input/{pname}: header {h!r} is not an RFC 9110 token")
        if h.lower() in seen:
            raise ProtoError(f"input/{pname}: header {h!r} is declared twice (names are case-insensitive)")
        seen.add(h.lower())


def _check_template(uri: str, params: dict) -> None:
    """§23.3: a `uri` with `{var}` is a template; its variables are exactly the declared `input`, as strings."""
    names = _VAR.findall(uri)
    if not names:
        return
    for n in names:
        if not _IDENT.match(n):
            raise ProtoError(f"uri: {{{n}}} is not a variable name")
        if names.count(n) > 1:
            raise ProtoError(f"uri: variable {{{n}}} appears twice")
    if set(names) != set(params):
        raise ProtoError(f"uri: template variables {sorted(names)} must match input {sorted(params)}")
    for n in names:
        if "enum" not in params[n] and params[n]["type"] != "string":
            raise ProtoError(f"input/{n}: a template variable is a string or an enum")


def template_regex(uri: str) -> re.Pattern | None:
    """The matcher of a template `uri` (`{var}` = one non-empty path segment), None for a plain URI."""
    if not _VAR.search(uri):
        return None
    parts = _VAR.split(uri)  # literal, var, literal, ...
    return re.compile("".join(re.escape(p) if i % 2 == 0 else f"(?P<{p}>[^/{{}}]+)" for i, p in enumerate(parts)))


def input_schema(params: dict) -> dict:
    props = {}
    for pname, spec in params.items():
        prop = {"type": "string", "enum": spec["enum"]} if "enum" in spec else {"type": spec["type"]}
        if "description" in spec:
            prop["description"] = spec["description"]
        if "header" in spec:
            prop["x-mcp-header"] = spec["header"]
        props[pname] = prop
    return {"type": "object", "properties": props, "required": list(params), "additionalProperties": False}


def output_schema(declared: dict) -> dict:
    """MCP `structuredContent` must be an object: a non-object `output` is wrapped under `result` (0.7.0, B5)."""
    if declared.get("type") == "object":
        return declared
    return {"type": "object", "properties": {"result": declared}, "required": ["result"]}


def wraps_output(declared: dict) -> bool:
    return declared.get("type") != "object"
