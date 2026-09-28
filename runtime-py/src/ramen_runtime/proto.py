"""Proto JSON validation (CONTRACTS §1) and MCP inputSchema derivation (§2)."""

import jsonschema

KINDS = {"tools": "tool", "resources": "resource", "prompts": "prompt"}
_TYPES = ["number", "string", "integer", "boolean"]
_PARAM = {
    "type": "object",
    "properties": {
        "type": {"enum": _TYPES},
        "enum": {"type": "array", "items": {"type": "string"}, "minItems": 1},
        "description": {"type": "string"},
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
        "properties": {**_COMMON, **_CALLABLE},
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
        "properties": {**_COMMON, "skill": {"type": "string", "minLength": 1}, "settings": {"type": "string"}},
        "required": ["type", "name", "input", "skill"],
    },
}


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


def input_schema(params: dict) -> dict:
    props = {}
    for pname, spec in params.items():
        prop = {"type": "string", "enum": spec["enum"]} if "enum" in spec else {"type": spec["type"]}
        if "description" in spec:
            prop["description"] = spec["description"]
        props[pname] = prop
    return {"type": "object", "properties": props, "required": list(params), "additionalProperties": False}
