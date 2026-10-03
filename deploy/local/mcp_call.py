"""Tiny MCP client (official `mcp` 2.x SDK): list tools and call one, over Streamable HTTP by default (CONTRACTS §16).

Usage: python mcp_call.py URL_OR_TARGET [KEY] [tool] [json-args]
  (run inside runtime-py's venv: `uv run --all-extras` — the `mcp` SDK is a dev extra there)
  http://host:port/mcp   → Streamable HTTP straight at the worker or its load balancer (the front door)
  host:port              → the stdio bridge over gRPC (compatibility path; needs `ramen-mcp-bridge` on PATH:
                           `uv tool install ramen-mcp-bridge` — its own package since 0.5.7)
  KEY                    → the rmk_ key; `-` or omitted reads RAMEN_MCP_KEY, so the key never sits in a shell history
Env: RAMEN_MCP_GROUP / RAMEN_MCP_ZONE (routing headers; default demo / local); for the bridge path
     RAMEN_BRIDGE_TLS / RAMEN_BRIDGE_CA pass through and RAMEN_BRIDGE=<command> replaces `ramen-mcp-bridge`.
"""

import asyncio
import json
import os
import shlex
import sys

from mcp import ClientSession


def _routing() -> dict:
    return {
        "ramen-group": os.environ.get("RAMEN_MCP_GROUP", "demo"),
        "ramen-zone": os.environ.get("RAMEN_MCP_ZONE", "local"),
    }


async def _run(session: ClientSession, tool: str, args: dict) -> int:
    await session.initialize()
    print(f"tools: {[t.name for t in (await session.list_tools()).tools]}")
    res = await session.call_tool(tool, args)
    text = " ".join(getattr(c, "text", "") for c in res.content)
    print(f"{tool}({json.dumps(args)}) -> {text}{'  [error]' if res.is_error else ''}")
    return 1 if res.is_error else 0


async def over_http(url: str, key: str, tool: str, args: dict) -> int:
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {key}", **_routing()}
    async with httpx2.AsyncClient(headers=headers, timeout=60) as client:
        async with streamable_http_client(url, http_client=client) as streams:
            async with ClientSession(streams[0], streams[1]) as s:
                return await _run(s, tool, args)


async def over_bridge(target: str, key: str, tool: str, args: dict) -> int:
    from mcp import StdioServerParameters
    from mcp.client.stdio import stdio_client

    cmd = shlex.split(os.environ.get("RAMEN_BRIDGE") or "ramen-mcp-bridge")
    routing = _routing()
    env = {
        **os.environ,
        "RAMEN_MCP_KEY": key,
        "RAMEN_BRIDGE_GROUP": routing["ramen-group"],
        "RAMEN_BRIDGE_ZONE": routing["ramen-zone"],
    }
    params = StdioServerParameters(command=cmd[0], args=[*cmd[1:], "--target", target], env=env)
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            return await _run(s, tool, args)


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    target = sys.argv[1]
    key = (sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != "-" else "") or os.environ.get("RAMEN_MCP_KEY", "")
    if not key:
        print("no key: pass it as the second argument or set RAMEN_MCP_KEY", file=sys.stderr)
        return 2
    tool = sys.argv[3] if len(sys.argv) > 3 else "demo_calculator_tool"
    args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {"var1": 2, "var2": 3, "func": "add"}
    if target.startswith(("http://", "https://")):
        return asyncio.run(over_http(target, key, tool, args))
    return asyncio.run(over_bridge(target, key, tool, args))


if __name__ == "__main__":
    sys.exit(main())
