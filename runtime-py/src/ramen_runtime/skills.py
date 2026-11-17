"""`mcp/skills/<name>/SKILL.md` + files: Skills over MCP (CONTRACTS §33.2), read as `skill://<name>/<relative path>`."""

import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

from .apps import NAME
from .content import b64
from .proto import ProtoError

MAX_FILE = 256 * 1024
SCHEME = "skill://"
_FRONT = re.compile(r"\A---\n(.*?)\n---\n", re.S)
_MIME = {".md": "text/markdown", ".yaml": "application/yaml", ".yml": "application/yaml"}


@dataclass
class Skill:
    name: str
    description: str
    version: str
    dir: Path
    files: list[str]

    def describe(self) -> dict:
        return {"name": self.name, "description": self.description, "version": self.version, "files": self.files}

    def read(self, rel: str) -> dict:
        uri = f"{SCHEME}{self.name}/{rel}"
        if rel.startswith("/") or ".." in rel.split("/") or "\\" in rel:
            raise ProtoError(f"path refused: {uri}")
        if rel not in self.files:
            raise KeyError(uri)
        data = (self.dir / rel).read_bytes()
        if len(data) > MAX_FILE:
            raise ProtoError(f"{uri} is {len(data)} bytes, over the 256 KiB limit")
        mime = _MIME.get(Path(rel).suffix.lower()) or mimetypes.guess_type(rel, strict=False)[0]
        try:
            body = {"text": data.decode("utf-8")}
            mime = mime or "text/plain"
        except UnicodeDecodeError:
            body = {"blob": b64(data)}
            mime = mime or "application/octet-stream"
        return {"contents": [{"uri": uri, "mimeType": mime} | body]}


def split(uri: str) -> tuple[str, str]:
    """`skill://<name>/<rel>` → (name, rel); a malformed URI is a KeyError (not found)."""
    name, sep, rel = uri.removeprefix(SCHEME).partition("/")
    if not sep or not name or not rel:
        raise KeyError(uri)
    return name, rel


def load_skill(d: Path) -> Skill:
    if not NAME.match(d.name):
        raise ProtoError(f"skill name {d.name!r} must match {NAME.pattern}")
    if not (d / "SKILL.md").is_file():
        raise ProtoError("missing SKILL.md")
    files = sorted(
        p.relative_to(d).as_posix()
        for p in d.rglob("*")
        if p.is_file() and not p.is_symlink() and "__pycache__" not in p.parts
    )
    h = hashlib.sha256()
    for rel in files:
        data = (d / rel).read_bytes()
        if len(data) > MAX_FILE:
            raise ProtoError(f"{rel} is {len(data)} bytes, over the 256 KiB limit")
        h.update(f"{rel}\0{len(data)}\0".encode())
        h.update(data)
    front = _frontmatter((d / "SKILL.md").read_text(encoding="utf-8"))
    if front.get("name") != d.name:
        raise ProtoError(f"SKILL.md: frontmatter name {front.get('name')!r} must be {d.name!r}")
    if not isinstance(desc := front.get("description"), str) or not desc.strip():
        raise ProtoError("SKILL.md: frontmatter needs a description")
    return Skill(d.name, desc.strip(), h.hexdigest()[:12], d, files)


def _frontmatter(text: str) -> dict:
    if not (m := _FRONT.match(text)):
        raise ProtoError("SKILL.md: missing frontmatter (--- name/description ---)")
    try:
        front = yaml.safe_load(m.group(1))
    except yaml.YAMLError as e:
        raise ProtoError(f"SKILL.md: invalid frontmatter: {e}") from None
    if not isinstance(front, dict):
        raise ProtoError("SKILL.md: frontmatter must be a mapping")
    return front
