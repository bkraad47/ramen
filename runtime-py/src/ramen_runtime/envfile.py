"""`mcp/env.yaml` (or `mcp/.env`): the group repo's environment, rendered from Ramen secrets at load (0.6.0).

Values may reference secrets as `{{$group.VAR}}` exactly like call arguments; the worker resolves them from the
secrets the deploy handed it (`RAMEN_SECRET_<GROUP>__<VAR>`) and exports the result to the runtime process, so tool
code reads `os.environ["DB_URL"]` and never sees where the value came from. A reference with no secret behind it is
skipped with a warning (the tool that needs it fails at call time, the rest of the group still loads). Rendered
values are redacted from error messages like call-time secrets. The file is flat: `KEY: value` lines (yaml) or
`KEY=value` lines (.env); `#` starts a comment; quotes around a value are optional.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from . import secrets
from .log import log

FILES = ("env.yaml", "env.yml", ".env")
_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_VALUES: list[str] = []  # what was rendered this load, for redaction


def parse(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    sep = "=" if path.name == ".env" else ":"
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if sep not in line:
            raise ValueError(f"{path.name}:{n}: expected KEY{sep} value")
        key, value = (s.strip() for s in line.split(sep, 1))
        if not _KEY.match(key):
            raise ValueError(f"{path.name}:{n}: {key!r} is not an environment variable name")
        if value[:1] in ('"', "'"):  # quoted: everything up to the closing quote, a trailing comment ignored
            end = value.find(value[0], 1)
            value = value[1:end] if end > 0 else value[1:]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        out[key] = value
    return out


def find(bucket: Path) -> Path | None:
    for name in FILES:
        if (p := bucket / "mcp" / name).is_file():
            return p
    return None


def apply(bucket: Path) -> dict[str, str]:
    """Render the file (if any) into `os.environ`; returns what was set. Keys the deploy already set are overridden —
    the file is the group's own say."""
    _VALUES.clear()
    path = find(bucket)
    if path is None:
        return {}
    rendered: dict[str, str] = {}
    for key, value in parse(path).items():
        try:
            rendered[key] = secrets.resolve(value)
        except secrets.SecretError as e:
            log("warn", "envfile", key=key, skipped=str(e))
            continue
    for key, value in rendered.items():
        os.environ[key] = value
        if value and value != parse(path).get(key):  # a rendered secret, never a literal
            _VALUES.append(value)
    log("info", "envfile", file=path.name, keys=sorted(rendered))
    return rendered


def rendered_values() -> list[str]:
    return list(_VALUES)
