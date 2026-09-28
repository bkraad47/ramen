"""Tiny MCP client (official `mcp` 2.x SDK) over the stdio bridge (CONTRACTS §11): list tools and call one.
Usage: python mcp_call.py TARGET KEY [tool] [json-args]     (run inside runtime-py's venv: `uv run --all-extras`)
Env: RAMEN_BRIDGE_GROUP / RAMEN_BRIDGE_ZONE / RAMEN_BRIDGE_TLS / RAMEN_BRIDGE_CA pass through to the bridge;
     RAMEN_BRIDGE=<command> replaces `python -m ramen_runtime.bridge`.
"""

import asyncio
import json
import os
import shlex
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(target: str, key: str, tool: str, args: dict) -> int:
    cmd = shlex.split(os.environ.get("RAMEN_BRIDGE") or f"{shlex.quote(sys.executable)} -m ramen_runtime.bridge")
    params = StdioServerParameters(
        command=cmd[0], args=[*cmd[1:], "--target", target, "--key", key], env=dict(os.environ)
    )
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            print(f"tools: {[t.name for t in (await s.list_tools()).tools]}")
            res = await s.call_tool(tool, args)
            text = " ".join(getattr(c, "text", "") for c in res.content)
            print(f"{tool}({json.dumps(args)}) -> {text}{'  [error]' if res.is_error else ''}")
            return 1 if res.is_error else 0


if __name__ == "__main__":
    target, key = sys.argv[1], sys.argv[2]
    tool = sys.argv[3] if len(sys.argv) > 3 else "demo_calculator_tool"
    args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {"var1": 2, "var2": 3, "func": "add"}
    sys.exit(asyncio.run(main(target, key, tool, args)))
