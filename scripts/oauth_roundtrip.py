#!/usr/bin/env python3
"""The OAuth flow of CONTRACTS §16.3 driven end to end against a live console and worker, as a client would do it.

    scripts/oauth_roundtrip.py https://<console> <email> <password> <mcp_url> <group> <zone> [--ca ramen-lb.pem]

Registers a loopback client (super admin), signs in, asks /oauth/authorize, approves the consent form, exchanges
the code with PKCE, calls the worker's /mcp with the access token, refreshes once, and checks that the worker refuses
the token for another zone. Prints one line per step and exits non-zero on the first failure. No secrets are
printed. Needs httpx (the console's venv has it: `cd console && uv run python ../scripts/oauth_roundtrip.py …`).
"""

import argparse
import base64
import hashlib
import json
import secrets
import sys
from urllib.parse import parse_qs, urlparse

import httpx

REDIRECT = "http://127.0.0.1:9999/callback"


def b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def step(name: str, ok: bool, detail: str = "") -> None:
    print(f"{'ok  ' if ok else 'FAIL'} {name}{' — ' + detail if detail else ''}")
    if not ok:
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("console")
    ap.add_argument("email")
    ap.add_argument("password")
    ap.add_argument("mcp_url")
    ap.add_argument("group")
    ap.add_argument("zone")
    ap.add_argument("--ca", help="PEM to verify the console and the worker against (default: system store)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS verification (laptop against a fresh cluster)")
    a = ap.parse_args()
    verify = False if a.insecure else (a.ca or True)
    console = a.console.rstrip("/")
    scope = f"mcp:{a.group}:{a.zone}"

    with httpx.Client(base_url=console, verify=verify, timeout=60, follow_redirects=False) as c:
        r = c.post("/login", data={"email": a.email, "password": a.password})
        step("sign in", r.status_code in (200, 303), f"HTTP {r.status_code}")
        csrf = {"X-Ramen-CSRF": c.cookies.get("ramen_csrf", "")}

        meta = c.get("/.well-known/oauth-authorization-server").json()
        step("AS metadata", meta.get("code_challenge_methods_supported") == ["S256"] and "scopes_supported" not in meta)

        r = c.post("/api/v1/oauth/clients", json={"name": "roundtrip", "redirect_uris": [REDIRECT]}, headers=csrf)
        step("register client", r.status_code == 201, f"HTTP {r.status_code}")
        client_id = r.json()["client_id"]

        verifier = b64(secrets.token_bytes(32))
        challenge = b64(hashlib.sha256(verifier.encode()).digest())
        q = {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "scope": scope,
            "state": "rt",
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "resource": scope,
        }
        r = c.get("/oauth/authorize", params=q)
        step("consent page", r.status_code == 200 and "Allow" in r.text, f"HTTP {r.status_code}")
        r = c.post("/oauth/authorize", data={**q, "decision": "allow", "csrf_token": c.cookies.get("ramen_csrf", "")})
        loc = r.headers.get("location", "")
        code = parse_qs(urlparse(loc).query).get("code", [""])[0]
        step(
            "authorize → code",
            r.status_code == 303 and loc.startswith(REDIRECT) and bool(code),
            f"HTTP {r.status_code}",
        )

        form = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": REDIRECT,
            "code_verifier": verifier,
        }
        r = c.post("/oauth/token", data=form)
        step(
            "token exchange",
            r.status_code == 200,
            f"HTTP {r.status_code} {r.text[:120] if r.status_code != 200 else ''}",
        )
        tok = r.json()
        r = c.post("/oauth/token", data=form)
        step("code is single use", r.status_code == 400)
        r = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id},
        )
        step("refresh rotates", r.status_code == 200 and r.json()["refresh_token"] != tok["refresh_token"])
        tok2 = r.json()
        r = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": tok["refresh_token"], "client_id": client_id},
        )
        step("reuse of the old refresh token revokes the grant", r.status_code == 400)
        r = c.post(
            "/oauth/token",
            data={"grant_type": "refresh_token", "refresh_token": tok2["refresh_token"], "client_id": client_id},
        )
        step("…the new one too", r.status_code == 400)

    def call(token: str, zone: str, req: dict) -> httpx.Response:
        h = {"Content-Type": "application/json", "ramen-group": a.group, "ramen-zone": zone}
        if token:
            h["Authorization"] = f"Bearer {token}"
        return httpx.post(a.mcp_url, headers=h, json=req, verify=verify, timeout=60)

    ping = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
    r = call("", a.zone, ping)
    www = r.headers.get("www-authenticate", "")
    step("worker challenge names the metadata", r.status_code == 401 and "resource_metadata" in www, www)
    meta_url = www.split('resource_metadata="')[1].split('"')[0]
    if meta_url.startswith("/"):
        meta_url = a.mcp_url.rsplit("/mcp", 1)[0] + meta_url
    r = httpx.get(meta_url, headers={"ramen-group": a.group, "ramen-zone": a.zone}, verify=verify, timeout=60)
    step(
        "protected-resource metadata names the console",
        r.status_code == 200 and r.json()["authorization_servers"] == [console],
        r.text[:120],
    )
    meta = r.json() if r.status_code == 200 else {}
    step(  # RFC 9728: what Claude Code compares with the URL it was given (0.5.92)
        "metadata resource is this MCP URL",
        meta.get("resource") == a.mcp_url and meta.get("scopes_supported") == [f"mcp:{a.group}:{a.zone}"],
        f"resource={meta.get('resource')!r}",
    )
    r = call(tok2["access_token"], a.zone, ping)
    step(
        "worker accepts the token",
        r.status_code == 200 and "result" in r.json(),
        f"HTTP {r.status_code} {r.text[:120]}",
    )
    tools = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": "demo_calculator_tool", "arguments": {"var1": 2, "var2": 3, "func": "add"}},
    }
    r = call(tok2["access_token"], a.zone, tools)
    text = r.json().get("result", {}).get("content", [{}])[0].get("text", "")
    step("tools/call with the token", r.status_code == 200 and text.strip().startswith("5"), text[:40])
    sid = r.headers.get("mcp-session-id")
    # a token whose zone is another zone: the balancer may have no such zone (404) or the node refuses it (401)
    r = call(tok2["access_token"], a.zone + "-other", ping)
    step("token for another zone is refused", r.status_code in (401, 404), f"HTTP {r.status_code}")
    print(json.dumps({"scope": scope, "session_header_seen": bool(sid), "expires_in": tok2["expires_in"]}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
