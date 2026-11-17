# deploy/local — the compose stack

Firestore emulator + console + one worker (group `demo`, zone `local`). Run from the repo root.
Long version with screenshots: https://bkraad47.github.io/ramen/get-started/

```sh
make up        # writes .env with generated secrets once (make env); pulls docker.io/bkraad47/ramen-{console,worker}:$VERSION and starts them (BUILD=1 builds from source, 3–5 min)
make demo      # zone/group/env → mint rmk_ key → canary deploy → tools/call; last line "PASS: demo_calculator_tool(2,3,add) -> 5"
make logs      # follow containers
make down      # stop + delete volumes
```

| Service | URL | Login / key |
|---|---|---|
| Console | **https://localhost:8443** (the local CA in `.certs/ca/ca.crt` → `curl --cacert`, or accept the browser warning) | `admin@ramen.local` / the generated `RAMEN_ADMIN_PASSWORD` in `.env` |
| Worker MCP | **https://localhost:8080/mcp** (Streamable HTTP, the front door) and **localhost:8080** (`ramen.v1.Mcp/Call` over gRPC with TLS, for the stdio bridge and gRPC clients) | `Authorization: Bearer rmk_…` minted on the group page (or the generated `RAMEN_MCP_KEYS` in `.env`) |
| Firestore emulator | http://localhost:8081 | — |

D86: every hop of this stack is TLS. The `certs` service writes a local CA and one leaf per service into
`deploy/local/.certs` (git-ignored, kept across runs, re-issued when due; `rm -rf deploy/local/.certs` starts over);
the laptop trusts `deploy/local/.certs/ca/ca.crt` (`curl --cacert`, bridge `--ca`, `NODE_EXTRA_CA_CERTS` for Node
clients).

Locally you do **not** need to add a zone or a group by hand: `make demo` creates zone `local`, group `demo`
(repo https://github.com/bkraad47/ramen-demo-mcp-group) and environment `dev`, and is safe to re-run
(existing objects answer `… exists`; a new key is minted every time).

Before the first deploy of the demo, add an LLM key on **Config → LLM providers** (with an allowed model): the
demo's guardrails (`iris_report`, `iris_outliers`) and evals use it, and a deploy without one fails with `no LLM
provider`. Workers reach the model through the console's LLM proxy (`RAMEN_CONSOLE_INTERNAL_URL=https://console:8443`);
the key never leaves the console. Key-free try-out: `RAMEN_DEMO_REF=v0.8.0 make demo` deploys the demo's `v0.8.0`
tag (no LLM rails).

## Mint a worker key and call the tool
```sh
# Console: Groups → demo → MCP auth keys → name → Mint MCP key (shown once) → Deploy (canary).  Or:
curl -sk -c c.txt -X POST https://localhost:8443/login -d email=admin@ramen.local -d "password=$(sed -n 's/^RAMEN_ADMIN_PASSWORD=//p' deploy/local/.env)" -o /dev/null
CSRF="X-Ramen-CSRF: $(awk '$6=="ramen_csrf"{print $7}' c.txt)"   # cookie sessions echo the csrf cookie on mutations (API keys don't need it)
KEY=$(curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/mcp-keys -d '{"name":"laptop"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://localhost:8443/api/v1/groups/demo/environments/dev/deploy -d '{"canary":true}'
sleep 5
# Streamable HTTP (CONTRACTS §16): a URL and a header
curl -s --cacert deploy/local/.certs/ca/ca.crt https://localhost:8080/mcp -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' -H 'ramen-group: demo' -H 'ramen-zone: local' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
# Stdio-only clients: the bridge over gRPC (`uv tool install ramen-mcp-bridge`; 1.0.0 is the bridge for Ramen 1.0.0, TLS by default), key from the environment
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' \
  | RAMEN_MCP_KEY="$KEY" ramen-mcp-bridge --target localhost:8080 --ca deploy/local/.certs/ca/ca.crt --group demo --zone local
# Or raw gRPC with grpcurl (bytes are base64 in its JSON; the node serves reflection locally):
BODY=$(printf '%s' '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | base64)
grpcurl -cacert deploy/local/.certs/ca/ca.crt -H "authorization: Bearer $KEY" -d "{\"body\":\"$BODY\"}" localhost:8080 ramen.v1.Mcp/Call
```
No key → HTTP `401` / gRPC `UNAUTHENTICATED` (the bridge relays it as JSON-RPC `-32001`).
`mcp-client-config.example.json` is a ready `mcpServers` entry for Claude Desktop / Cursor: the HTTP form with the
key from `RAMEN_MCP_KEY`, plus the deployed and stdio forms spelled out.

**That example is local-only.** It trusts the local CA (`NODE_EXTRA_CA_CERTS`); for a real deployment use the
`_DEPLOYED` block in the same file: `--target <lb-host>:443`, plus `--ca <pem>` only if the load balancer's
certificate is not in your system trust store, and a group MCP key minted on the group page rather than the `.env`
dev fallback. The bridge is TLS by default (1.0.0); `--insecure` is for a bare node outside compose only.

## Optional engines (§34.1)
`make up ENGINES=phoenix,promptfoo` (or `docker compose --profile phoenix --profile promptfoo up -d` with
`RAMEN_OTEL_UI_URL` / `RAMEN_EVALS_UI_URL` / `RAMEN_EVALS_API_URL` exported) adds the same try-out services the chart
does: **Phoenix** at https://localhost:6006 behind an OpenTelemetry Collector on `https://phoenix-collector:4318` (set
that as the endpoint under **Config → Telemetry**), and the **promptfoo** server at https://localhost:3000 (behind its
TLS front). All TLS with the local CA. Both are
off by default and ask for a sign-in (D92): Phoenix as `admin@localhost`, promptfoo as `admin`, both with
`RAMEN_ENGINES_PASSWORD` from `.env` (`make env` generates it with the other secrets; the engine profiles refuse to
start without it). The local adapter has no Job runner, so evals and SkillSpector scans do not run on
compose (a deploy says `skipped, no evals engine`); `RAMEN_EVALS_IMAGE` and `RAMEN_SKILLSPECTOR_IMAGE` are accepted
with the chart's meaning and stay empty here. `make down` removes the engines too.

**`rmk_` vs `rmn_`:** `rmk_` MCP keys are per group and go to workers; `rmn_` API keys (API Keys page,
`X-Ramen-Api-Key`) are for scripting the console and never reach a worker. Not interchangeable.

## Files
| File | What |
|---|---|
| `docker-compose.yml` | services `firestore` (8081), `console` (8443, `RAMEN_TLS=self`), `worker` (8080); shared volume `buckets` (`/buckets/demo` = the group bucket, `/buckets/_logs` = worker logs the console tails); profiles `phoenix` (6006 + collector 4318) and `promptfoo` (3000) |
| `phoenix-collector.yaml` | the collector config of the `phoenix` profile, the chart's (`templates/phoenix.yaml`) |
| `.env.example` → `.env` (`make env`, secrets generated, never committed) | `VERSION` (image tags), admin email/password, `RAMEN_FERNET_KEY`, `RAMEN_MCP_KEYS`, `RAMEN_ADMIN_KEY`, `RAMEN_ALLOWED_CIDRS`, `RAMEN_VERBOSE`, auth: `RAMEN_PUBLIC_URL`, `RAMEN_SMTP_*` (default `file:///mail` → invite/reset/magic-link mails land as `.eml` in `deploy/local/.mail/`), `RAMEN_AUTH_MAGIC_LINK`, `RAMEN_ADMIN_FORCE_PASSWORD`. Rotate everything before exposing the stack |
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
| `make up` builds instead of pulling | the tag in `.env` is not published yet (a branch ahead of the release); that is the fallback, or set `VERSION` to a released tag |
| e2e from `tests/` | `RAMEN_CONSOLE_URL=https://localhost:8443 RAMEN_NODE_TARGET=localhost:8080 RAMEN_ADMIN_PASSWORD=$(sed -n 's/^RAMEN_ADMIN_PASSWORD=//p' ../deploy/local/.env) RAMEN_TLS_INSECURE=1 RAMEN_MAIL_DIR=$PWD/deploy/local/.mail uv run pytest -rs e2e conformance` in `tests/` |

Cloud bring-up: [`../README.md`](../README.md).
