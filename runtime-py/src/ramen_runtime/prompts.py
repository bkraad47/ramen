"""Render a prompt package: SKILL.md with `{{param}}` substituted, settings appended."""

import json
import re

_FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)


def render(skill: str, args: dict, settings: dict) -> str:
    body = _FRONT.sub("", skill, count=1).strip()
    body = re.sub(r"\{\{\s*(\w+)\s*\}\}", lambda m: str(args.get(m.group(1), m.group(0))), body)
    if settings:
        body += "\n\n## Settings\n```json\n" + json.dumps(settings, indent=2) + "\n```"
    return body
