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
            "label": "MCP bridge — stdio clients",
            "text": f"ramen-mcp-bridge --target {host}:443 --tls --key <rmk_ key> --group {group} --zone {zone}",
        },
    ]


def oauth_boxes(url: str, host: str, group: str, zone: str, client_id: str) -> list[dict]:
    name = f"ramen-{group}-{zone}"
    bridge_args = [
        "--target",
        f"{host}:443",
        "--tls",
        "--oauth",
        url,
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
        {"label": "MCP bridge — command", "text": "ramen-mcp-bridge " + " ".join(bridge_args)},
        {
            "label": "Cursor, VS Code / GitHub Copilot, Claude Desktop — through the bridge",
            "text": _json({"mcpServers": {name: {"command": "ramen-mcp-bridge", "args": bridge_args}}}),
        },
    ]


def build(
    public_url: str, group: str, zones: list[str], clients: list[dict], auth, members: list[dict], configured: list[str]
) -> dict:
    host = urlsplit(public_url).hostname or "<host>"
    zones = zones or ["<zone>"]
    client = pick_client(clients)
    cid = client["client_id"] if client else "<client id>"
    per_zone = len(zones) > 1

    def boxes(make, *extra):
        out = []
        for z in zones:
            for b in make(public_url, host, group, z, *extra):
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
                "boxes": boxes(oauth_boxes, cid) if name in configured else [],
            }
        )
    return {
        "host": host,
        "public_url": public_url,
        "zones": zones,
        "client": client,
        "clients": clients,
        "password": auth.password_login,
        "magic": auth.magic_link,
        "manual_members": sum(1 for m in members if m["source"] == "manual"),
        "key_boxes": boxes(key_boxes),
        "account_boxes": boxes(oauth_boxes, cid),
        "providers": providers,
    }
