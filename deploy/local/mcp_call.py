"""Tiny MCP client (official `mcp` 2.x SDK): list tools and call one over Streamable HTTP.
Usage: uv run --with 'mcp>=2,<3' python mcp_call.py URL KEY [tool] [json-args]
"""
import asyncio
import json
import sys

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(url: str, key: str, tool: str, args: dict) -> int:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {key}"}, timeout=60) as http:
        async with streamable_http_client(url, http_client=http) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                print(f"tools: {[t.name for t in (await s.list_tools()).tools]}")
                res = await s.call_tool(tool, args)
                text = " ".join(getattr(c, "text", "") for c in res.content)
                print(f"{tool}({json.dumps(args)}) -> {text}{'  [error]' if res.is_error else ''}")
                return 1 if res.is_error else 0


if __name__ == "__main__":
    url, key = sys.argv[1], sys.argv[2]
    tool = sys.argv[3] if len(sys.argv) > 3 else "demo_calculator_tool"
    args = json.loads(sys.argv[4]) if len(sys.argv) > 4 else {"var1": 2, "var2": 3, "func": "add"}
    sys.exit(asyncio.run(main(url, key, tool, args)))
