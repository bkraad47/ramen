"""The group page's "How MCP users sign in" card (0.7.5): one labelled box per client and command, generated from the
live install — edge host, the group's zones, a registered OAuth client, the identity providers and their rules.

Group keys work over Streamable HTTP in every client (a bearer header). A person's OAuth sign-in needs a client the
console knows (pre-registered, PKCE): Claude Code takes the client id in its config; Cursor and VS Code / GitHub
Copilot register clients dynamically, which Ramen does not offer, so they reach Ramen through the stdio bridge."""

from __future__ import annotations

import json
from urllib.parse import urlsplit

PROVIDERS = {
    "entra": ("Microsoft Entra ID", "https://login.microsoftonline.com/<tenant id>/v2.0", "Microsoft Graph"),
    "google": ("Google Workspace", "https://accounts.google.com", "Cloud Identity"),
}
LOOPBACK = ("http://127.0.0.1", "http://localhost")
BRIDGE = "ramen-mcp-bridge@latest"  # uvx form: always the newest release (a cached uvx may otherwise run an old one)


def pick_client(clients: list[dict]) -> dict | None:
    """A client meant for local tools: a loopback redirect, named for the bridge or Claude if one is."""
    local = [c for c in clients if any(u.startswith(LOOPBACK) for u in c.get("redirect_uris") or [])]
    named = [c for c in local if any(w in (c.get("name") or "").lower() for w in ("bridge", "claude", "mcp"))]
    return (named or local or clients or [None])[0]


def _json(doc: dict) -> str:
    return json.dumps(doc, indent=2)


def key_boxes(url: str, host: str, group: str, zone: str) -> list[dict]:
    name = f"ramen-{group}-{zone}"
    headers = {"Authorization": "Bearer <rmk_ key>", "ramen-group": group, "ramen-zone": zone}
    return [
        {
            "label": "Claude Code",
            "text": f"claude mcp add --transport http {name} {url}/mcp \\\n"
            '  --header "Authorization: Bearer <rmk_ key>" \\\n'
            f'  --header "ramen-group: {group}" --header "ramen-zone: {zone}"',
        },
        {
            "label": "Cursor — ~/.cursor/mcp.json",
            "text": _json({"mcpServers": {name: {"url": f"{url}/mcp", "headers": headers}}}),
        },
        {
            "label": "VS Code / GitHub Copilot — .vscode/mcp.json",
            "text": _json({"servers": {name: {"type": "http", "url": f"{url}/mcp", "headers": headers}}}),
        },
        {
            "label": "Claude Code — through the bridge (stdio)",
            "text": f"claude mcp add {name} -- uvx ramen-mcp-bridge@latest --target {host}:443 --tls \\\n"
            f"  --key <rmk_ key> --group {group} --zone {zone}",
        },
        {
            "label": "MCP bridge — run it yourself (any stdio client)",
            "text": f"uvx ramen-mcp-bridge@latest --target {host}:443 --tls \\\n"
            f"  --key <rmk_ key> --group {group} --zone {zone}",
        },
    ]


def oauth_boxes(url: str, host: str, group: str, zone: str, client_id: str, issuer: str | None = None) -> list[dict]:
    """`url`/`host` reach the zone (a satellite's own, §28.1); `issuer` is the console that signs the person in."""
    name = f"ramen-{group}-{zone}"
    issuer = issuer or url
    bridge_args = [
        "--target",
        f"{host}:443",
        "--tls",
        "--oauth",
        issuer,
        "--client-id",
        client_id,
        "--group",
        group,
        "--zone",
        zone,
    ]
    return [
        {
            "label": "Claude Code — Streamable HTTP",
            "text": f"claude mcp add-json {name} '"
            + json.dumps(
                {
                    "type": "http",
                    "url": f"{url}/mcp",
                    "headers": {"ramen-group": group, "ramen-zone": zone},
                    "oauth": {"clientId": client_id, "scopes": f"mcp:{group}:{zone}"},
                }
            )
            + "'",
        },
        {
            "label": "Claude Code — through the bridge (stdio)",
            "text": f"claude mcp add {name} -- uvx ramen-mcp-bridge@latest --target {host}:443 --tls \\\n"
            f"  --oauth {issuer} --client-id {client_id} --group {group} --zone {zone}",
        },
        {
            "label": "Cursor, VS Code / GitHub Copilot, Claude Desktop — through the bridge",
            "text": _json({"mcpServers": {name: {"command": "uvx", "args": [BRIDGE, *bridge_args]}}}),
        },
        {
            "label": "MCP bridge — run it yourself (any stdio client)",
            "text": "uvx ramen-mcp-bridge@latest " + " ".join(bridge_args),
        },
    ]


def build(
    public_url: str,
    group: str,
    zones: list[str],
    clients: list[dict],
    auth,
    members: list[dict],
    configured: list[str],
    hosts: dict[str, str] | None = None,
) -> dict:
    """`hosts`: zone → the satellite host serving it (§28.1); every other zone is reached at the console's host.
    The OAuth issuer (`--oauth`, the token's scope) stays the console in every box."""
    host = urlsplit(public_url).hostname or "<host>"
    zones = zones or ["<zone>"]
    client = pick_client(clients)
    cid = client["client_id"] if client else "<client id>"
    per_zone = len(zones) > 1
    hosts = {z: (hosts or {}).get(z) or host for z in zones}

    def boxes(make, *extra):
        out = []
        for z in zones:
            url = public_url if hosts[z] == host else f"https://{hosts[z]}"
            for b in make(url, hosts[z], group, z, *extra):
                out.append({**b, "label": b["label"] + (f" · zone {z}" if per_zone else "")})
        return out

    providers = []
    for name in [*PROVIDERS, *[n for n in configured if n not in PROVIDERS]]:
        title, issuer, lookup = PROVIDERS.get(name, (name, "", ""))
        rules = [
            {"value": v, "role": r}
            for v, (r, gs) in sorted((auth.role_map.get(name) or {}).items())
            if group in gs or r == "super_admin"
        ]
        providers.append(
            {
                "name": name,
                "title": title,
                "issuer": issuer,
                "lookup": lookup,
                "configured": name in configured,
                "claim": auth.role_claim.get(name, ""),
                "source": auth.source_of(name),
                "rules": rules,
                "members": sum(1 for m in members if m["source"] == "idp" and m["provider"] == name),
                "boxes": boxes(oauth_boxes, cid, public_url) if name in configured else [],
            }
        )
    return {
        "host": host,
        "hosts": hosts,
        "public_url": public_url,
        "zones": zones,
        "client": client,
        "clients": clients,
        "password": auth.password_login,
        "magic": auth.magic_link,
        "manual_members": sum(1 for m in members if m["source"] == "manual"),
        "key_boxes": boxes(key_boxes),
        "account_boxes": boxes(oauth_boxes, cid, public_url),
        "providers": providers,
    }
