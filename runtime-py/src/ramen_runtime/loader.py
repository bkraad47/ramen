"""Walks <bucket>/mcp/{apps,tools,resources,prompts,skills} and builds a Registry; errors are per package."""

import hashlib
import importlib
import json
import re
import sys
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path

from jsonschema.exceptions import best_match
from jsonschema.protocols import Validator
from jsonschema.validators import validator_for

from .apps import App, load_app
from .log import log
from .proto import KINDS, ProtoError, input_schema, output_schema, template_regex, validate_proto
from .skills import Skill, load_skill


@dataclass
class Package:
    kind: str
    name: str
    dir: Path
    proto: dict
    func: Callable | None = None
    skill: str = ""
    settings: dict = field(default_factory=dict)
    template: re.Pattern | None = None  # §23.3: a resource whose uri has `{var}`
    ui: str = ""  # §33.1: the `ui://` resource of the app a tool declares with `ui`

    @property
    def schema(self) -> dict:
        return input_schema(self.proto.get("input", {}))

    @property
    def output(self) -> dict | None:
        return output_schema(self.proto["output"]) if "output" in self.proto else None

    # §35.1 (D80 perf): built once per load — `jsonschema.validate` checked the schema against the metaschema on every
    # call. A schema that fails the check raises here each time (nothing is cached), as before.
    @cached_property
    def input_validator(self) -> Validator:
        return compiled(self.schema)

    @cached_property
    def output_validator(self) -> Validator | None:
        return None if self.output is None else compiled(self.output)


def compiled(schema: dict) -> Validator:
    """What `jsonschema.validate` builds per call: the schema's draft, checked once."""
    cls = validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def check(validator: Validator, instance) -> None:
    """`jsonschema.validate`'s error choice (the best match) on a compiled validator."""
    if (error := best_match(validator.iter_errors(instance))) is not None:
        raise error


@dataclass
class Registry:
    bucket: Path
    tools: list[Package] = field(default_factory=list)
    resources: list[Package] = field(default_factory=list)
    prompts: list[Package] = field(default_factory=list)
    apps: list[App] = field(default_factory=list)
    skills: list[Skill] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def describe(self) -> dict:
        d = {
            "tools": [
                {"name": t.name, "description": t.proto.get("description", ""), "inputSchema": t.schema}
                | ({"outputSchema": t.output} if t.output else {})
                | ({"task": True} if t.proto.get("task") else {})  # §23.5
                | ({"_meta": {"ui/resourceUri": t.ui}} if t.ui else {})  # §33.1
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
                if r.template is None
            ]
            + [a.describe() for a in self.apps],
            "prompts": [
                {
                    "name": p.name,
                    "description": p.proto.get("description", ""),
                    "arguments": [
                        {"name": a, "description": s.get("description", ""), "required": True} | _enum(s)
                        for a, s in p.proto.get("input", {}).items()
                    ],
                    "_meta": {"settings": p.settings},
                }
                for p in self.prompts
            ],
        }
        # §23.3: the keys below exist only when a package uses them, so a 0.8 group describes (and hashes) as before
        if templates := [r for r in self.resources if r.template is not None]:
            d["resourceTemplates"] = [
                {
                    "name": r.name,
                    "uriTemplate": r.proto["uri"],
                    "description": r.proto.get("description", ""),
                    "mimeType": r.proto["mime_type"],
                }
                | ({"variables": v} if (v := {a: _enum(s) for a, s in r.proto["input"].items() if "enum" in s}) else {})
                for r in templates
            ]
        if self.apps:  # §33.1 / §33.2: listed for the node (discover extensions) and the console (Packages card)
            d["apps"] = [a.summary() for a in self.apps]
        if self.skills:
            d["skills"] = [s.describe() for s in self.skills]
        return d | {"hash": manifest_hash(d), "errors": self.errors}


def _enum(spec: dict) -> dict:
    """§23.1 completion: the node answers `completion/complete` from the declared `enum`."""
    return {"enum": spec["enum"]} if "enum" in spec else {}


def manifest_hash(d: dict) -> str:
    """C1 (0.7.0): sha256 of the canonical JSON of tools+resources+prompts, prompts without `_meta`; plus
    `resourceTemplates` (§23.3), `apps` and `skills` (§33) when the group has any."""
    body = {
        "tools": d["tools"],
        "resources": d["resources"],
        "prompts": [{k: v for k, v in p.items() if k != "_meta"} for p in d["prompts"]],
    } | {k: d[k] for k in ("resourceTemplates", "apps", "skills") if k in d}
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def load(bucket: Path) -> Registry:
    reg = Registry(bucket)
    root = bucket / "mcp"
    if not root.is_dir():
        reg.errors.append({"package": "mcp", "reason": "missing mcp/ directory"})
        return reg
    apps = {a.name: a for a in reg.apps}
    loaders = {"apps": load_app, "skills": load_skill} | {f: lambda d, f=f: _load_package(f, d, apps) for f in KINDS}
    for folder in ("apps", *KINDS, "skills"):  # apps first: a tool's `ui` must name a loaded app
        for d in sorted(p for p in (root / folder).glob("*") if p.is_dir()):
            try:
                pkg = loaders[folder](d)
                getattr(reg, folder).append(pkg)
                if folder == "apps":
                    apps[pkg.name] = pkg
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
        apps=len(reg.apps),
        skills=len(reg.skills),
        errors=len(reg.errors),
    )
    return reg


def _load_package(folder: str, d: Path, apps: dict[str, App]) -> Package:
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
        if "callable" in proto:  # §23.3: a callable prompt, bound like a tool
            pkg.func = _bind(d, proto["callable"])
        else:
            skill = d / proto["skill"]
            if not skill.is_file():
                raise ProtoError(f"skill file {proto['skill']} not found")
            pkg.skill = skill.read_text(encoding="utf-8")
        settings = d / proto.get("settings", "settings.json")
        if settings.is_file():
            pkg.settings = json.loads(settings.read_text(encoding="utf-8"))
        return pkg
    pkg.func = _bind(d, proto["callable"])
    if folder == "resources":
        pkg.template = template_regex(proto["uri"])
    if ui := proto.get("ui"):  # §33.1
        if ui not in apps:
            raise ProtoError(f"ui: app {ui!r} not found under mcp/apps")
        pkg.ui = apps[ui].uri
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
