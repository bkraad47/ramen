"""Policy for the MRTR and task fixtures (CONTRACTS 23.4: pre on every round, post on the final result only).
Records every payload to `$RAMEN_TEST_POLICY_LOG` so a test can count the hook runs and see what each one saw."""

import json
import os
from pathlib import Path


def _record(stage: str, tool: str, payload) -> None:
    if p := os.environ.get("RAMEN_TEST_POLICY_LOG"):
        with Path(p).open("a") as f:
            f.write(json.dumps({"stage": stage, "tool": tool, "payload": payload}) + "\n")


def pre(tool: str, arguments: dict) -> str | None:
    _record("pre", tool, arguments)
    responses = json.dumps(arguments.get("$inputResponses", {})).lower()
    if "boom" in responses:
        raise RuntimeError("policy exploded on a resumed call")
    if "under duress" in responses:
        return "the answer may not be used"
    if "drop table" in json.dumps(arguments).lower():
        return "arguments mention a blocked statement"
    return None


def post(tool: str, arguments: dict, result) -> str | None:
    _record("post", tool, result)
    if "classified" in json.dumps(result).lower():
        return "output mentions classified material"
    return None
