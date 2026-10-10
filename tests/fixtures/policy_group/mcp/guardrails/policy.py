"""A group's own guardrail policy (CONTRACTS §21.2, engine `policy`). Records what it saw so tests can assert that the
hook ran (or did not) and that no secret value ever reached it."""

import json
import os
from pathlib import Path


def _record(stage: str, tool: str, payload) -> None:
    if p := os.environ.get("RAMEN_TEST_POLICY_LOG"):
        with Path(p).open("a") as f:
            f.write(json.dumps({"stage": stage, "tool": tool, "payload": payload}) + "\n")


def pre(tool: str, arguments: dict) -> str | None:
    _record("pre", tool, arguments)
    text = json.dumps(arguments).lower()
    if "boom" in text:
        raise RuntimeError("policy exploded")
    if "drop table" in text:
        return "arguments mention a blocked statement"
    return None


def post(tool: str, arguments: dict, result) -> str | None:
    _record("post", tool, result)
    text = json.dumps(result).lower()
    if "kaboom" in text:
        raise RuntimeError("policy exploded on output")
    if "secret" in text:
        return "output mentions a secret"
    return None
