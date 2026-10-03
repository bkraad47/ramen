# Connect an MCP client: keys, OAuth, password accounts, the bridge

A Ramen worker is a URL. Which credential a client presents decides whose name the call runs under:

| You are… | Use | The worker logs |
|---|---|---|
| An automation, an agent platform, a CI job | an **MCP key** `rmk_…` of the group (shared, revocable) | the key id |
| A person with a Ramen account (password, magic link or your company IdP) | **OAuth** — the client signs you in through the console and gets a token for one group and zone | `user:<your id>` |
| A client that can only start a local process (stdio) | the **bridge** `ramen-mcp-bridge`, with either of the above | the same as above |

Everything below uses the demo group, zone `a`, and a cloud deployment at `https://<edge>`. On GCP and AWS the
same hostname serves the console and, by the `ramen-group` / `ramen-zone` headers, every zone's workers. For a
local stack the console is `https://localhost:8443` and the worker is `http://localhost:8080`.

!!! tip "Where the pieces come from"
    - The group's tools come from the [demo MCP repo](https://github.com/bkraad47/ramen-demo-mcp-group) — set it as
      the group's repo and deploy; the group page shows the repo and the packages each zone reported.
    - The bridge is the [`ramen-mcp-bridge` package on PyPI](https://pypi.org/project/ramen-mcp-bridge/):
      `pip install ramen-mcp-bridge` or `uv tool install ramen-mcp-bridge`.

<figure markdown>
![The group page: repo URL, environments, packages per zone](../img/group.png){ .ramen-shot }
<figcaption>The group page is where the repo link, the deploy and the per-zone package list live.</figcaption>
</figure>

## 1. With an MCP key (HTTP, no bridge)
A group admin generates a key on the group page (*MCP auth keys → Generate key*); it is shown once. Export it as
`RAMEN_MCP_KEY` and point any Streamable HTTP client at the worker:

```json
{"mcpServers": {"ramen-demo": {"url": "https://<edge>/mcp",
  "headers": {"Authorization": "Bearer ${RAMEN_MCP_KEY}", "ramen-group": "demo", "ramen-zone": "a"}}}}
```

Claude Desktop (*Settings → Developer → Edit Config*), Cursor and the `mcp` SDK all take that shape; the
`ramen-group` / `ramen-zone` headers are what the load balancer routes on. `curl` works the same way
([local quickstart §4](local-quickstart.md#4-connect-a-client-claude-desktop-cursor-the-mcp-sdk)).

<figure markdown>
![A key shown once](../img/key-shown.png){ .ramen-shot }
</figure>

## 2. As yourself, with OAuth (Claude Code, Claude Desktop, Cursor)
No shared key: the client sends you to the console, you sign in the way you always do, approve the client once
for that group and zone, and every call is yours.

1. A super admin registers the client on *Config → OAuth clients*: a name and the redirect URI
   `http://localhost/callback` (any loopback name and port are accepted — RFC 8252). The client id is shown in the
   table.
2. Your account needs a role in the group — an **MCP User** is enough (*Users* page, or an IdP rule).
3. Add the server with the client id. Claude Code:
   ```sh
   claude mcp add-json ramen-demo '{"type":"http","url":"https://<edge>/mcp",
     "headers":{"ramen-group":"demo","ramen-zone":"a"},
     "oauth":{"clientId":"<client id>","scopes":"mcp:demo:a"}}'
   ```
   then `/mcp` in Claude Code → *ramen-demo* → sign in. Claude Desktop and Cursor discover the flow by themselves
   from the worker's `401` and `/.well-known/oauth-protected-resource`.
4. The browser opens the console's login page if you are signed out — password, magic link or your provider — and
   then the consent page. Approve. Tokens last an hour and renew themselves for thirty days. Removing your role, or
   disabling your account, ends them immediately.

<figure markdown>
![The consent page](../img/oauth-consent.png){ .ramen-shot }
<figcaption>What a person sees once: the client, the group and the zone it asks for.</figcaption>
</figure>

**A password-based account works exactly like this.** There is nothing to configure for it: the authorize request
redirects to the login page, and the login page returns you to the consent page.

## 3. The bridge (stdio clients)
```sh
pip install ramen-mcp-bridge
# with the group key
ramen-mcp-bridge --target <edge>:443 --tls --key "$RAMEN_MCP_KEY" --group demo --zone a
# as yourself (bridge 0.2.0): the first run opens the browser on the console's sign-in page
ramen-mcp-bridge --target <edge>:443 --tls --oauth https://<edge> --client-id <client id> --group demo --zone a
```
```json
{"mcpServers": {"ramen-stdio": {"command": "ramen-mcp-bridge",
  "args": ["--target", "<edge>:443", "--tls", "--oauth", "https://<edge>", "--client-id", "<client id>", "--group", "demo", "--zone", "a"]}}}
```
The refresh token is kept in `~/.config/ramen-mcp-bridge/` (mode 0600); later runs need no browser. Against the
local stack use `--target localhost:8080 --insecure`. Every flag has a `RAMEN_BRIDGE_*` variable.

## 4. What the worker answers
| Situation | Answer |
|---|---|
| No credential | `401` with `WWW-Authenticate: Bearer resource_metadata="…/.well-known/oauth-protected-resource", scope="mcp:demo:a"` |
| Wrong key, token for another zone, revoked user | `401` |
| Source outside the zone's IP rules | `403` |
| A blocked tool | `200` with JSON-RPC `-32601` — the transport accepted you, the tool said no |
| Too many calls (throttle) | `429` |

The same table holds on gRPC: `UNAUTHENTICATED`, `PERMISSION_DENIED`, `OUT_OF_RANGE` (oversized) and
`RESOURCE_EXHAUSTED`; a JSON-RPC error stays a JSON-RPC error on both.
[Transport and security](../wiki/transport.md) has the whole guard table.
