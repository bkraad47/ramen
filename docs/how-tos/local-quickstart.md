# Local quickstart

Runs the whole system on one machine with docker compose: a Firestore emulator, the console, and one worker
(group `demo`, zone `local`). Needs **Docker with compose v2**, **uv**, **git**, `make`. First run builds two
images (~3–5 min); later runs take seconds.

## 1. Start
```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up
```
`make up` copies `deploy/local/.env.example` to `.env` (once) and runs `docker compose up -d --build`.

| Service | Address | Notes |
|---|---|---|
| Console | **https://localhost:8443** | self-signed certificate — accept the browser warning (`curl -k`) |
| Worker (MCP) | **http://localhost:8080/mcp** (Streamable HTTP) and **localhost:8080** (gRPC, h2c) — one port, plain text locally | `tools/list` lists nothing until the first deploy loads code; `grpc.health.v1.Health/Check` reads `NOT_SERVING` until then |
| Firestore emulator | http://localhost:8081 | state persists in the compose volume until `make down` |

**Login**: `admin@ramen.local` / `changeme-ramen` (from `deploy/local/.env`; change them there before exposing the stack).

!!! note "Two transports, one port (0.5.0)"
    `POST http://localhost:8080/mcp` is the front door (Streamable HTTP, MCP spec 2025-06-18). The same port still
    serves JSON-RPC 2.0 over gRPC (`ramen.v1.Mcp/Call`) for the stdio bridge and any gRPC client. `/healthz`,
    `/readyz` and `/metrics` are not back: health is `grpc.health.v1.Health`, metrics are `Admin/Metrics`.

<figure markdown>
![Login](../img/login.png){ .ramen-shot width=720 }
</figure>

## 2. Run the demo (creates data and proves the path)
```sh
make demo
```
Expected tail of the output (about 20–40 s after the stack is up; the first deploy pip-installs the demo repo's requirements):
```
login: 303
{"id":"local","name":"local", ...}            # zone, group, environment created (or "... exists" on a re-run)
ready: http://localhost:8080/mcp lists demo_calculator_tool
tools: ['demo_calculator_tool']
demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5
PASS: demo_calculator_tool(2,3,add) -> 5
```
The `PASS:` line is the success marker. `make demo` is safe to re-run: existing zone/group/environment answer
`... exists` and a fresh MCP key is generated each time.

What it did, through the console API: `POST /api/v1/zones {local}` → `POST /api/v1/groups {demo, repo_url}`
→ `POST /api/v1/groups/demo/environments {dev, zones:[local]}` → `POST /api/v1/groups/demo/mcp-keys` (an
`rmk_` key) → `POST .../environments/dev/deploy` → poll the job → call the tool with the official `mcp` SDK's
Streamable HTTP client straight at `http://localhost:8080/mcp` (`deploy/local/mcp_call.py`;
`RAMEN_DEMO_TRANSPORT=bridge make demo` runs the same call through the stdio bridge over gRPC instead).

!!! note "You do not need to add zones or groups by hand locally"
    The GCP/AWS guides ask you to create a zone and a group in the console. Locally `make demo` has already done
    that; the console shows zone `local` and group `demo` on first login.

## 3. Generate a key
The worker only accepts **`rmk_` MCP keys** generated for the group (`RAMEN_MCP_KEYS` in `.env` is a dev fallback that
the compose stack also honours, default `local-mcp-key`). Generate one:

=== "Console"
    Groups → **demo** → *MCP auth keys* → enter a name → **Generate key**. The key is shown **once**. Then click
    **Deploy (canary)** on the environment so the key reaches the worker.

=== "API (cookie session)"
    ```sh
    curl -sk -c c.txt -X POST https://localhost:8443/login -d email=admin@ramen.local -d password=changeme-ramen -o /dev/null
    CSRF="X-Ramen-CSRF: $(awk '$6=="ramen_csrf"{print $7}' c.txt)"   # cookie sessions must echo the CSRF token
    KEY=$(curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/mcp-keys \
      -d '{"name":"laptop"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
    curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/environments/dev/deploy -d '{"canary":true}'
    echo "$KEY"    # rmk_<id>_<secret>
    ```

## 4. Connect a client (Claude Desktop, Cursor, the `mcp` SDK)
The worker is a URL and a header. Put the key in the environment so it never sits in a config file:

```sh
export RAMEN_MCP_KEY="$KEY"
```

=== "Claude Desktop / Cursor"
    `deploy/local/mcp-client-config.example.json` is the ready-made `mcpServers` entry:
    ```json
    {
      "mcpServers": {
        "ramen-demo": {
          "url": "http://localhost:8080/mcp",
          "headers": {"Authorization": "Bearer ${RAMEN_MCP_KEY}", "ramen-group": "demo", "ramen-zone": "local"}
        }
      }
    }
    ```
    Claude Desktop: *Settings → Developer → Edit Config* (`claude_desktop_config.json`), paste, restart; the
    `demo_calculator_tool` shows up under the server's tools. A client that cannot expand `${RAMEN_MCP_KEY}` takes
    the literal key in the header value.

=== "mcp SDK (Python)"
    ```python
    import asyncio, os
    import httpx2
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    headers = {"Authorization": f"Bearer {os.environ['RAMEN_MCP_KEY']}", "ramen-group": "demo", "ramen-zone": "local"}

    async def main():
        async with httpx2.AsyncClient(headers=headers) as http:
            async with streamable_http_client("http://localhost:8080/mcp", http_client=http) as (r, w, *_):
                async with ClientSession(r, w) as s:
                    await s.initialize()
                    print([t.name for t in (await s.list_tools()).tools])
                    res = await s.call_tool("demo_calculator_tool", {"var1": 2, "var2": 3, "func": "add"})
                    print(res.content[0].text)   # 5

    asyncio.run(main())
    ```
    This is what `deploy/local/mcp_call.py` does when given a URL.

=== "curl"
    ```sh
    curl -s http://localhost:8080/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
      -H 'ramen-group: demo' -H 'ramen-zone: local' \
      -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
    # {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
    ```
    Without a key the answer is `401` with `WWW-Authenticate: Bearer`; from a source outside `RAMEN_ALLOWED_CIDRS` it
    is `403`; a browser `Origin` that is not on `RAMEN_ALLOWED_ORIGINS` (empty by default) is `403` too; a blocked
    tool still answers `200` with JSON-RPC `-32601` in the body — the transport rejected nothing, the tool said no.
    `initialize` returns an `Mcp-Session-Id` header; send it back on later calls (the SDK does), or don't — sessions
    are optional.

The `ramen-group` / `ramen-zone` headers are ignored by a single local worker but are what the cloud load balancer
routes on, so keep them in every client config.

### Stdio-only clients: the bridge
A client that can only start a local process uses `ramen-mcp-bridge`, a stdio MCP server that forwards every
JSON-RPC message to the worker's `Mcp/Call` over gRPC and sends the key and the routing metadata for you. Install it
once (it is the console script of `ramen-runtime[grpc]`; the worker image has it too):

```sh
uv tool install './runtime-py[grpc]'          # → ~/.local/bin/ramen-mcp-bridge
RAMEN_MCP_KEY="$KEY" ramen-mcp-bridge --target localhost:8080 --insecure --group demo --zone local
# stdin/stdout is now an MCP server; Ctrl-C to stop. --insecure = plaintext h2c (local only); use --tls --ca <pem> against a load balancer.
```
```json
{"mcpServers": {"ramen-stdio": {"command": "ramen-mcp-bridge",
  "args": ["--target", "localhost:8080", "--insecure", "--group", "demo", "--zone", "local"],
  "env": {"RAMEN_MCP_KEY": "rmk_…"}}}}
```

## 5. Call the worker raw over gRPC with `grpcurl`
The gRPC service is the same worker on the same port. One JSON-RPC message per `ramen.v1.Mcp/Call`; the `body` field
is bytes, so `grpcurl` wants it base64-encoded (`brew install grpcurl` /
[releases](https://github.com/fullstorydev/grpcurl/releases)). Run from the repo root so `-proto` finds
`proto/ramen/v1/mcp.proto`:

```sh
REQ=$(printf '%s' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' | base64)
grpcurl -plaintext -import-path proto -proto ramen/v1/mcp.proto \
  -H "authorization: Bearer $KEY" -H 'ramen-group: demo' -H 'ramen-zone: local' \
  -d "{\"body\":\"$REQ\"}" localhost:8080 ramen.v1.Mcp/Call | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["body"]).decode())'
# {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
grpc_health_probe -addr localhost:8080          # status: SERVING   (github.com/grpc-ecosystem/grpc-health-probe)
```
Without a key the call fails with gRPC status `Unauthenticated` (16); from a source outside `RAMEN_ALLOWED_CIDRS`
with `PermissionDenied` (7) — the same two checks the HTTP path answers with 401 and 403, because they are the same
code.

## `rmk_` vs `rmn_` — two different keys
| | `rmk_…` MCP key | `rmn_…` API key |
|---|---|---|
| Generated at | group page → *MCP auth keys*, or `POST /api/v1/groups/{g}/mcp-keys` | *API Keys* page, or `POST /api/v1/api-keys` |
| Sent as | `Authorization: Bearer rmk_…` to a **worker** — an HTTP header on `/mcp`, gRPC metadata on `Mcp/Call`, `RAMEN_MCP_KEY` for the bridge | `X-Ramen-Api-Key: rmn_…` to the **console** `/api/v1/*` |
| Scope | one group; reaches every worker of that group on the next deploy | a console role (+ groups) never wider than the creator's |
| Use for | Claude Desktop, Cursor, agents, `curl` or `grpcurl` to tools | CI deploys, rotation, backups, scripting the console |

The *API Keys* page is **not** where worker keys come from — but it *is* where **OAuth clients** are registered
when one person, not a team, should hold the access: the client signs the user in through the console and gets a
token scoped to them ([Security](security.md#streamable-http-sessions-and-oauth-v050)). See
[DevOps with the API](devops-api.md).

## 6. Look around
- **Dashboard** `/` — load per zone × group (from `Admin/Metrics`); *Refresh discovery* re-reads the adapter.
- **Groups → demo** — repo/ref, environments with **Deploy (canary)**, deploy jobs with streamed logs,
  zones/workers (scale, IP rules, rebalance, logs), loaded packages, MCP keys, SA restrictions.
- **Secrets** — add `{"name","value","env?","zone?"}`; values are never shown again. Reference as `{{$demo.NAME}}`.
- **Logs** — one JSON line per MCP call (`ts, ip, group, method, name, status, grpc_code, ms, key_id`); *download* gets the file.
- **Audit** — every mutation with user, IP, action, target, ok.
- **Config** `/config` — auth toggles (password login, magic link), OAuth providers, SMTP, SA rules, permission catalogue.
- **API docs** — `https://localhost:8443/api/docs` (OpenAPI).

## 7. Try the v0.3.0 controls (API, cookie session + CSRF header from §3)
```sh
# block a tool for the environment, redeploy, and watch tools/list hide it (calls answer -32601); unblock the same way
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X PUT https://localhost:8443/api/v1/groups/demo/environments/dev/blocked -d '{"blocked":["demo_calculator_tool"]}'
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/environments/dev/deploy -d '{"canary":true}'
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X PUT https://localhost:8443/api/v1/groups/demo/environments/dev/blocked -d '{"blocked":[]}'
# auth settings (super admin). Disabling password login needs another way in first (magic link, OAuth, or RAMEN_ADMIN_FORCE_PASSWORD=1) or you get 422.
curl -sk -b c.txt https://localhost:8443/api/v1/config/auth
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X PUT https://localhost:8443/api/v1/config/auth -d '{"magic_link":true}'
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X PUT https://localhost:8443/api/v1/config/auth -d '{"password_login":false}'
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X PUT https://localhost:8443/api/v1/config/auth -d '{"password_login":true,"magic_link":false}'
```
MCP keys live in the store, so `make down` (which discards the Firestore emulator) invalidates every key; generate again after a fresh `make up`.

<figure markdown>
![Group page](../img/group.png){ .ramen-shot }
</figure>

## 8. Your own group repo
1. Copy the layout from [Protos](../wiki/protos.md) (or fork the demo repo) — the step-by-step is [Add and deploy a tool](add-a-tool.md).
2. Console → Groups → **Add group** with your repo URL and ref. Private repo: add a secret named `GITHUB_TOKEN`.
3. Add an environment with zone `local`, generate an MCP key, **Deploy**. Errors per package show up in the job.

## 9. Stop, reset, troubleshoot
```sh
make down                    # stop + delete volumes (Firestore data, buckets)
make logs                    # follow all containers
docker compose -f deploy/local/docker-compose.yml ps
```
| Symptom | Cause / fix |
|---|---|
| `curl: (60) SSL certificate problem` | self-signed cert on the console: use `-k` |
| MCP call → HTTP `401` / gRPC `Unauthenticated` (16) | key not generated, or generated but not deployed yet; run a deploy |
| MCP call → HTTP `403` / gRPC `PermissionDenied` (7) | source address outside `RAMEN_ALLOWED_CIDRS` (zone IP rules), or a browser `Origin` that is not on `RAMEN_ALLOWED_ORIGINS` |
| MCP call → HTTP `404 unknown session` | an `Mcp-Session-Id` from another key, an expired one, or one minted before the zone's secret changed; re-`initialize` without it |
| `Health/Check` → `NOT_SERVING` | no deploy has loaded code yet; `make demo` or click Deploy |
| Bridge exits at once / client shows no tools | wrong `--target`, missing `--insecure` against the plaintext local worker, or a `--tls` mismatch; run the bridge by hand in a terminal and read stderr |
| `curl http://localhost:8080/mcp` → `405` | a `GET`: the worker has no server-initiated messages, `POST` a JSON-RPC message |
| `curl http://localhost:8080/mcp` → `406` / `415` | `Accept` must include `application/json`; `Content-Type` must be `application/json` |
| Deploy job `error` with pip output | `mcp/requirements.txt` failed to install; fix and redeploy |
| Old image versions in `docker compose ps` | `deploy/local/.env` pins `VERSION`; update it and `make up` |

Next: [GCP](gcp.md) · [AWS (untested)](aws.md) · [Secrets](secrets.md) · [Security](security.md) · [Threat model](../threat-model.md)
