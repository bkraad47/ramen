# deploy/local — the compose stack

Firestore emulator + console + one worker (group `demo`, zone `local`). Run from the repo root.
Long version with screenshots: https://bkraad47.github.io/ramen/how-tos/local-quickstart/

```sh
make up        # copies .env.example → .env once; docker compose up -d --build (first run 3–5 min)
make demo      # zone/group/env → mint rmk_ key → canary deploy → tools/call; last line "PASS: demo_calculator_tool(2,3,add) -> 5"
make logs      # follow containers
make down      # stop + delete volumes
```

| Service | URL | Login / key |
|---|---|---|
| Console | **https://localhost:8443** (self-signed → `curl -k`, accept the browser warning) | `admin@ramen.local` / `changeme-ramen` (`.env`) |
| Worker MCP | **http://localhost:8080/mcp** (Streamable HTTP, the front door) and **localhost:8080** (`ramen.v1.Mcp/Call` over gRPC, for the stdio bridge and gRPC clients) | `Authorization: Bearer rmk_…` minted on the group page (or the `.env` dev fallback `RAMEN_MCP_KEYS=local-mcp-key`) |
| Firestore emulator | http://localhost:8081 | — |

Locally you do **not** need to add a zone or a group by hand: `make demo` creates zone `local`, group `demo`
(repo https://github.com/bkraad47/ramen-demo-mcp-group) and environment `dev`, and is safe to re-run
(existing objects answer `… exists`; a new key is minted every time).

## Mint a worker key and call the tool
```sh
# Console: Groups → demo → MCP auth keys → name → Mint MCP key (shown once) → Deploy (canary).  Or:
curl -sk -c c.txt -X POST https://localhost:8443/login -d email=admin@ramen.local -d password=changeme-ramen -o /dev/null
CSRF="X-Ramen-CSRF: $(awk '$6=="ramen_csrf"{print $7}' c.txt)"   # cookie sessions echo the csrf cookie on mutations (API keys don't need it)
KEY=$(curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/mcp-keys -d '{"name":"laptop"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/environments/dev/deploy -d '{"canary":true}'
sleep 5
# Streamable HTTP (CONTRACTS §16): a URL and a header
curl -s http://localhost:8080/mcp -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -H 'ramen-group: demo' -H 'ramen-zone: local' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
# Stdio-only clients: the bridge over gRPC (`pip install 'ramen-runtime[grpc]'` or use runtime-py/.venv), key from the environment
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' \
  | RAMEN_MCP_KEY="$KEY" ramen-mcp-bridge --target localhost:8080 --group demo --zone local
# Or raw gRPC with grpcurl (bytes are base64 in its JSON; the node serves reflection locally):
BODY=$(printf '%s' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | base64)
grpcurl -plaintext -H "authorization: Bearer $KEY" -d "{\"body\":\"$BODY\"}" localhost:8080 ramen.v1.Mcp/Call
```
No key → HTTP `401` / gRPC `UNAUTHENTICATED` (the bridge relays it as JSON-RPC `-32001`).
`mcp-client-config.example.json` is a ready `mcpServers` entry for Claude Desktop / Cursor: the HTTP form with the
key from `RAMEN_MCP_KEY`, plus the deployed and stdio forms spelled out.

**That example is local-only.** Its `args` end in `--insecure`, which forces a plaintext h2c channel and overrides
`--tls`/`--ca`, so a copy of it pointed at a deployment would keep talking plaintext without saying so. For a real
deployment use the `_DEPLOYED` block in the same file: drop `--insecure`, set `--target <lb-host>:443`, add `--tls`
and `--ca <pem>` (omit `--ca` only if the load balancer's certificate is already in your system trust store), and use
a group MCP key minted on the group page rather than the `.env` dev fallback.

**`rmk_` vs `rmn_`:** `rmk_` MCP keys are per group and go to workers; `rmn_` API keys (API Keys page,
`X-Ramen-Api-Key`) are for scripting the console and never reach a worker. Not interchangeable.

## Files
| File | What |
|---|---|
| `docker-compose.yml` | services `firestore` (8081), `console` (8443, `RAMEN_TLS=self`), `worker` (8080); shared volume `buckets` (`/buckets/demo` = the group bucket, `/buckets/_logs` = worker logs the console tails) |
| `.env.example` → `.env` | `VERSION` (image tags), admin email/password, `RAMEN_FERNET_KEY`, `RAMEN_MCP_KEYS`, `RAMEN_ADMIN_KEY`, `RAMEN_ALLOWED_CIDRS`, `RAMEN_VERBOSE`, auth: `RAMEN_PUBLIC_URL`, `RAMEN_SMTP_*` (default `file:///mail` → invite/reset/magic-link mails land as `.eml` in `deploy/local/.mail/`), `RAMEN_AUTH_MAGIC_LINK`, `RAMEN_ADMIN_FORCE_PASSWORD`. Rotate everything before exposing the stack |
| `demo.sh` | the `make demo` flow against the console API (`docs/CONTRACTS.md` §4a) |
| `demo-worker.sh` | `make demo-worker`: node + runtime on the host, no Docker |
| `mcp_call.py` | tiny client on the official `mcp` SDK: Streamable HTTP when given a URL, the stdio bridge when given `host:port`; used by the demos |
| `mcp-client-config.example.json` | `mcpServers` entry for desktop clients: HTTP first, the stdio bridge form under `_STDIO` |

## Troubleshooting
| Symptom | Fix |
|---|---|
| `curl: (60) SSL certificate problem` | `-k` (self-signed) |
| MCP call → HTTP `401` / gRPC `UNAUTHENTICATED` / bridge `-32001` | key not minted for `demo`, or minted but not deployed yet — run a deploy |
| `POST /mcp` → `403` from a browser | the page's `Origin` is not on `RAMEN_ALLOWED_ORIGINS` (empty by default); add it to the deploy file or `.env` |
| console API → 403 `csrf token missing` | cookie session without `X-Ramen-CSRF` (value of the `ramen_csrf` cookie); or use an `rmn_` API key |
| tool missing from `tools/list` / `-32601` | blocked on the group page (Loaded packages → block/unblock), applied on deploy |
| "forgot password?" mail | `deploy/local/.mail/*.eml` (file backend); set `RAMEN_SMTP_HOST` to a real server to send |
| `ramen-mcp-bridge --health` → `NOT_SERVING` | nothing deployed yet: `make demo` or click Deploy (`--health ramen.v1.Admin` is liveness and stays SERVING) |
| deploy job `error` with pip output | the group repo's `mcp/requirements.txt` failed; fix and redeploy |
| images still tagged an old version | `.env` pins `VERSION`; edit it, `make up` |
| e2e from `tests/` | `RAMEN_CONSOLE_URL=https://localhost:8443 RAMEN_NODE_TARGET=localhost:8080 RAMEN_ADMIN_PASSWORD=changeme-ramen RAMEN_TLS_INSECURE=1 RAMEN_MAIL_DIR=$PWD/deploy/local/.mail uv run pytest -rs e2e conformance` in `tests/` |

Cloud bring-up: [`../README.md`](../README.md) (GCP verified; AWS untested).
