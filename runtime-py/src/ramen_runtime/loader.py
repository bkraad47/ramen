"""Walks <bucket>/mcp/{tools,resources,prompts} and builds a Registry; errors are per package."""

import hashlib
import importlib
import json
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from .log import log
from .proto import KINDS, ProtoError, input_schema, output_schema, validate_proto


@dataclass
class Package:
    kind: str
    name: str
    dir: Path
    proto: dict
    func: Callable | None = None
    skill: str = ""
    settings: dict = field(default_factory=dict)

    @property
    def schema(self) -> dict:
        return input_schema(self.proto.get("input", {}))

    @property
    def output(self) -> dict | None:
        return output_schema(self.proto["output"]) if "output" in self.proto else None


@dataclass
class Registry:
    bucket: Path
    tools: list[Package] = field(default_factory=list)
    resources: list[Package] = field(default_factory=list)
    prompts: list[Package] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def describe(self) -> dict:
        d = {
            "tools": [
                {"name": t.name, "description": t.proto.get("description", ""), "inputSchema": t.schema}
                | ({"outputSchema": t.output} if t.output else {})
                for t in self.tools
            ],
            "resources": [
                {
                    "uri": r.proto["uri"],
                    "name": r.name,
                    "description": r.proto.get("description", ""),
                    "mimeType": r.proto["mime_type"],
                }
                for r in self.resources
            ],
            "prompts": [
                {
                    "name": p.name,
                    "description": p.proto.get("description", ""),
                    "arguments": [
                        {"name": a, "description": s.get("description", ""), "required": True}
                        for a, s in p.proto.get("input", {}).items()
                    ],
                    "_meta": {"settings": p.settings},
                }
                for p in self.prompts
            ],
        }
        return d | {"hash": manifest_hash(d), "errors": self.errors}


def manifest_hash(d: dict) -> str:
    """C1 (0.7.0): sha256 of the canonical JSON of tools+resources+prompts, prompts without `_meta`."""
    body = {
        "tools": d["tools"],
        "resources": d["resources"],
        "prompts": [{k: v for k, v in p.items() if k != "_meta"} for p in d["prompts"]],
    }
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(bucket: Path) -> Registry:
    reg = Registry(bucket)
    root = bucket / "mcp"
    if not root.is_dir():
        reg.errors.append({"package": "mcp", "reason": "missing mcp/ directory"})
        return reg
    for folder in KINDS:
        for d in sorted(p for p in (root / folder).glob("*") if p.is_dir()):
            try:
                pkg = _load_package(folder, d)
                getattr(reg, folder).append(pkg)
            except Exception as e:  # noqa: BLE001 - every failure is reported, never fatal
                reg.errors.append(
                    {
                        "package": f"{folder}/{d.name}",
                        "reason": f"{type(e).__name__}: {e}" if not isinstance(e, ProtoError) else str(e),
                    }
                )
    log(
        "info",
        "loaded",
        bucket=str(bucket),
        tools=len(reg.tools),
        resources=len(reg.resources),
        prompts=len(reg.prompts),
        errors=len(reg.errors),
    )
    return reg


def _load_package(folder: str, d: Path) -> Package:
    jsons = list(d.glob("*.json"))
    if folder == "prompts":
        jsons = [j for j in jsons if j.name != "settings.json"]
    if len(jsons) != 1:
        raise ProtoError(f"expected exactly one proto json named {d.name}.json, found {[j.name for j in jsons]}")
    try:
        proto = json.loads(jsons[0].read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ProtoError(f"invalid json in {jsons[0].name}: {e}") from None
    validate_proto(proto, folder, d.name, jsons[0].stem)
    pkg = Package(kind=proto["type"], name=d.name, dir=d, proto=proto)
    if folder == "prompts":
        skill = d / proto["skill"]
        if not skill.is_file():
            raise ProtoError(f"skill file {proto['skill']} not found")
        pkg.skill = skill.read_text(encoding="utf-8")
        settings = d / proto.get("settings", "settings.json")
        if settings.is_file():
            pkg.settings = json.loads(settings.read_text(encoding="utf-8"))
        return pkg
    pkg.func = _bind(d, proto["callable"])
    return pkg


def _bind(d: Path, name: str) -> Callable:
    py = d / f"{d.name}.py"
    if not py.is_file():
        raise ProtoError(f"missing {py.name}")
    utils = str(d / "utils")
    if utils not in sys.path:
        sys.path.insert(0, utils)
    for mod in [m for m, v in list(sys.modules.items()) if str(getattr(v, "__file__", "") or "").startswith(str(d))]:
        del sys.modules[mod]
    importlib.invalidate_caches()
    module = types.ModuleType(f"ramen_pkg_{d.parent.name}_{d.name}")
    module.__file__ = str(py)
    exec(compile(py.read_text(encoding="utf-8"), str(py), "exec"), module.__dict__)  # no pycache: reload-safe
    func = getattr(module, name, None)
    if not callable(func):
        raise ProtoError(f"callable {name!r} not found in {py.name}")
    return func
