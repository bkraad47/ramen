"""`mcp/apps/<name>/index.html` (+ `app.yaml`): an MCP App, served as the resource `ui://<group>/<name>` (§33.1)."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .proto import ProtoError

MIME = "text/html;profile=mcp-app"
MAX_HTML = 512 * 1024
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
_META = {"title": str, "description": str, "csp": dict}


@dataclass
class App:
    name: str
    uri: str
    html: str
    title: str = ""
    description: str = ""
    csp: dict | None = None

    def describe(self) -> dict:
        """The `resources/list` entry."""
        d = {"uri": self.uri, "name": self.name, "description": self.description, "mimeType": MIME}
        if self.title:
            d["title"] = self.title
        if self.csp is not None:
            d["_meta"] = {"ui/csp": self.csp}
        return d

    def summary(self) -> dict:
        return {"name": self.name, "uri": self.uri, "title": self.title, "description": self.description}

    def read(self) -> dict:
        return {"contents": [{"uri": self.uri, "mimeType": MIME, "text": self.html}]}


def group() -> str:
    return os.environ.get("RAMEN_GROUP", "default")


def load_app(d: Path) -> App:
    if not NAME.match(d.name):
        raise ProtoError(f"app name {d.name!r} must match {NAME.pattern}")
    html = d / "index.html"
    if not html.is_file():
        raise ProtoError("missing index.html")
    if (n := html.stat().st_size) > MAX_HTML:
        raise ProtoError(f"index.html is {n} bytes, over the 512 KiB limit")
    meta = _meta(d / "app.yaml")
    if (csp := meta.get("csp")) is not None and not all(
        isinstance(v, list) and all(isinstance(s, str) for s in v) for v in csp.values()
    ):
        raise ProtoError("app.yaml: csp values must be lists of strings")
    return App(
        d.name,
        f"ui://{group()}/{d.name}",
        html.read_text(encoding="utf-8"),
        meta.get("title", ""),
        meta.get("description", ""),
        csp,
    )


def _meta(path: Path) -> dict:
    if not path.is_file():
        return {}
    try:
        meta = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise ProtoError(f"app.yaml: invalid yaml: {e}") from None
    if not isinstance(meta, dict):
        raise ProtoError("app.yaml must be a mapping")
    for k, v in meta.items():
        if k not in _META or not isinstance(v, _META[k]):
            raise ProtoError(
                f"app.yaml: {k} must be a {_META[k].__name__}" if k in _META else f"app.yaml: unknown key {k}"
            )
    return meta
