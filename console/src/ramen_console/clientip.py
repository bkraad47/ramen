"""The caller's address behind the console's proxies (0.8.0 SC6). `x-forwarded-for` is counted from the right with
`RAMEN_TRUST_PROXY_HOPS` (default 1: one load balancer), exactly as the node does (CONTRACTS §11): the left-most
entry is whatever the client wrote and must never become an audit or rate-limit key."""

import os

from starlette.requests import Request


def trusted_hops() -> int:
    try:
        return max(0, int(os.environ.get("RAMEN_TRUST_PROXY_HOPS", "1")))
    except ValueError:
        return 1


def client_ip(request: Request) -> str:
    peer = request.client.host if request.client else "-"
    fwd = request.headers.get("x-forwarded-for")
    hops = trusted_hops()
    if not fwd or hops == 0:
        return peer
    entries = [x.strip() for x in fwd.split(",") if x.strip()]
    return entries[-hops] if len(entries) >= hops else peer
