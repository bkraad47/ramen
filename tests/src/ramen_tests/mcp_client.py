"""Official `mcp` SDK client (Streamable HTTP) against a Ramen node."""
from contextlib import asynccontextmanager

import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from .env import strip, tls_verify


def endpoint(base: str) -> str:
    base = strip(base)
    return base if base.endswith("/mcp") else base + "/mcp"


def http_client(key: str | None, timeout: float = 30) -> httpx2.AsyncClient:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    kw = {"headers": headers, "timeout": httpx2.Timeout(timeout, read=timeout)}
    if not tls_verify():
        kw["verify"] = False
    return httpx2.AsyncClient(**kw)


@asynccontextmanager
async def session(base: str, key: str | None, timeout: float = 30):
    async with http_client(key, timeout) as http:
        async with streamable_http_client(endpoint(base), http_client=http) as (r, w):
            async with ClientSession(r, w, read_timeout_seconds=timeout) as s:
                await s.initialize()
                yield s


def text_of(result) -> str:
    return "".join(getattr(c, "text", "") for c in result.content)


INIT_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "ramen-tests", "version": "0.1.0"},
    },
}
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
