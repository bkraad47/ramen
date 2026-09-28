"""`{{$group.VAR}}` → env `RAMEN_SECRET_<GROUP>__<VAR>` (CONTRACTS §1). Values are never logged."""

import os
import re

_REF = re.compile(r"\{\{\s*\$([A-Za-z0-9_-]+)\.([A-Za-z0-9_-]+)\s*\}\}")


class SecretError(KeyError):
    def __str__(self) -> str:
        return self.args[0]


def env_name(group: str, var: str) -> str:
    return f"RAMEN_SECRET_{group}__{var}".replace("-", "_").upper()


def resolve(text: str, used: list[str] | None = None) -> str:
    def sub(m: re.Match) -> str:
        key = env_name(m.group(1), m.group(2))
        if key not in os.environ:
            raise SecretError(f"secret {m.group(1)}.{m.group(2).upper()} not set")
        if used is not None:
            used.append(os.environ[key])
        return os.environ[key]

    return _REF.sub(sub, text)


def substitute_args(args, used: list[str] | None = None):
    if isinstance(args, str):
        return resolve(args, used)
    if isinstance(args, dict):
        return {k: substitute_args(v, used) for k, v in args.items()}
    if isinstance(args, list):
        return [substitute_args(v, used) for v in args]
    return args


def redact(text: str, values: list[str]) -> str:
    for v in values:
        if v:
            text = text.replace(v, "***")
    return text
