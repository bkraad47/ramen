import os
from pathlib import Path

import yaml

PREFIX = "RAMEN_"
_owned: set[str] = set()


def flatten(d: dict, prefix: str = "") -> dict[str, str]:
    out: dict[str, str] = {}
    for k, v in d.items():
        key = f"{prefix}_{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            name = key.upper()
            out[name if name.startswith(PREFIX) else PREFIX + name] = str(v)
    return out


def load_yaml(path: str | None) -> dict:
    if not path or not Path(path).exists():
        return {}
    return yaml.safe_load(Path(path).read_text()) or {}


def apply_config(path: str | None = None) -> dict[str, str]:
    """Map yaml keys to RAMEN_* env vars; real env wins. Returns keys applied."""
    applied = {}
    for k, v in flatten(load_yaml(path or os.environ.get("RAMEN_CONFIG"))).items():
        if k not in os.environ or k in _owned:
            os.environ[k] = v
            _owned.add(k)
            applied[k] = v
    return applied
