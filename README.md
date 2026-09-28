<p align="center"><img src="docs/img/logo.png" width="380" alt="Project Ramen"></p>

<h1 align="center">Ramen</h1>
<p align="center">Multizone, highly available, enterprise-grade <b>MCP server</b> for GCP and AWS Kubernetes.<br>
Rust MCP node + Python 3.14 runtime workers, JSON-RPC 2.0 over gRPC, managed from a FastAPI console.</p>

<p align="center">
<a href="https://github.com/bkraad47/ramen/releases"><img src="https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release" alt="release"></a>
<a href="https://github.com/bkraad47/ramen/actions/workflows/ci.yml"><img src="https://github.com/bkraad47/ramen/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
<a href="https://bkraad47.github.io/ramen/"><img src="https://github.com/bkraad47/ramen/actions/workflows/pages.yml/badge.svg" alt="docs"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-BSD--3--Clause-F26B3A" alt="license"></a>
<a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-JSON--RPC%202.0%20over%20gRPC-2B2622" alt="MCP"></a>
</p>

**Docs: https://bkraad47.github.io/ramen/** · [How it works](https://bkraad47.github.io/ramen/how-it-works/) · [GCP guide](https://bkraad47.github.io/ramen/how-tos/gcp/) · [AWS guide (untested)](https://bkraad47.github.io/ramen/how-tos/aws/) · [Contracts](docs/CONTRACTS.md) · [Releases](https://github.com/bkraad47/ramen/releases)

> **0.3.1 is a transport change.** The worker's HTTP `/mcp` endpoint is gone; workers speak JSON-RPC 2.0 over
> **gRPC** (`ramen.v1.Mcp/Call`) and standard MCP clients connect through the **`ramen-mcp-bridge`** stdio bridge.
> Coming from 0.3.0: [migration note](https://bkraad47.github.io/ramen/how-tos/migrate-0.3.1/).

Ramen turns a **git repo of tools, resources and prompts** into a fleet of MCP workers behind a cloud load balancer.
Each worker pairs a **Rust MCP node** (gRPC transport, bearer auth, IP allow-lists, health, logs) 1:1 with a
**Python 3.14 runtime** that pip-installs and runs your code. One **console** manages groups (tenants), environments,
zones, secrets, canary deploys, rebalancing, IP rules, logs, audit and backups — in the browser or through an API key.

- **Git → bucket → worker.** Deploy syncs the repo to a bucket; workers load by content hash. No git creds on pods.
- **Canary by default.** Roll a canary, smoke-test `tools/list`, then roll stable. Failure leaves stable untouched.
- **Multi-zone from day one.** Group → Environment → Zone → Worker; the LB routes on `ramen-group` / `ramen-zone`
  metadata, so one client config works for every zone.
- **Enterprise controls.** Super admin / group admin / viewer, `rmk_` MCP keys, `rmn_` API keys, Cloud Armor / WAF
  IP rules, secrets that are never displayed, an audit line for every action.
- **Transport: JSON-RPC 2.0 over gRPC.** Binary framing, HTTP/2 multiplexing, first-class health and deadlines;
  the same security checks as before (`UNAUTHENTICATED` / `PERMISSION_DENIED` instead of 401 / 403). Claude
  Desktop, Cursor and the `mcp` SDK use `ramen-mcp-bridge`; anything else uses any gRPC client.

## Quickstart (local, 5 commands)

Needs Docker (compose v2), `uv`, `git`, `make`. First run ≈ 3–5 min (image builds).

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up       # Firestore emulator + console https://localhost:8443 + one worker (gRPC localhost:8080, h2c)
make demo     # zone → group `demo` (demo repo) → mint rmk_ key → canary deploy → tools/call through the bridge
#   … demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5      ← success line
open https://localhost:8443    # self-signed cert; login admin@ramen.local / changeme-ramen
make down     # stop and remove volumes
```

`make demo` is idempotent (existing zone/group/env answer `… exists`; a fresh key is minted each run).
Then connect your own client with an **`rmk_` MCP key** (Groups → demo → *Mint MCP key* → Deploy) through the
bridge — it is a stdio MCP server, so it drops into any `mcpServers` config:

```sh
uv tool install './runtime-py[grpc]'        # installs ramen-mcp-bridge (also present in the worker image)
ramen-mcp-bridge --target localhost:8080 --insecure --key rmk_… --group demo --zone local
```
```json
{"mcpServers": {"ramen-demo": {"command": "ramen-mcp-bridge",
  "args": ["--target", "localhost:8080", "--insecure", "--key", "rmk_…", "--group", "demo", "--zone", "local"]}}}
```

Raw gRPC works too (`grpcurl`, the body is base64 because it is `bytes`):
```sh
REQ=$(printf '%s' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' | base64)
grpcurl -plaintext -import-path proto -proto ramen/v1/mcp.proto -H "authorization: Bearer rmk_…" \
  -d "{\"body\":\"$REQ\"}" localhost:8080 ramen.v1.Mcp/Call
# {"body": "<base64 of {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}>"}
```

> `rmk_…` **MCP keys** go to workers (gRPC metadata `authorization: Bearer`, the bridge's `--key`) and are minted on
> the **group page**. `rmn_…` **API keys** go to the console (`X-Ramen-Api-Key`, HTTP) for automation and are minted
> on the **API Keys** page. They are not interchangeable. Full walkthrough with the `mcp` SDK snippet:
> [local quickstart](https://bkraad47.github.io/ramen/how-tos/local-quickstart/)
> (also in [`deploy/local/README.md`](deploy/local/README.md)).

## Screenshots

| Login | Dashboard (load per zone × group) |
|---|---|
| ![login](docs/img/login.png) | ![dashboard](docs/img/dashboard.png) |

| Group: environments, deploy jobs, zones/workers, MCP keys | Deploy job with streamed log |
|---|---|
| ![group](docs/img/group.png) | ![deploy job](docs/img/deploy-job.png) |

| Secrets (names only, values never shown) | Logs (one JSON line per MCP call, downloadable) |
|---|---|
| ![secrets](docs/img/secrets.png) | ![logs](docs/img/logs.png) |

## Deploy to the cloud

| Target | Status | Guide |
|---|---|---|
| **GCP** — GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB (GKE Gateway, header-routed gRPC, gRPC health checks), Cloud Armor | verified on a throwaway project in 0.3.0 (89/89 cloud tests); 0.3.1 routing change verified locally, cloud re-run pending | [docs](https://bkraad47.github.io/ramen/how-tos/gcp/) · [`deploy/README.md`](deploy/README.md) |
| **AWS** — EKS, DynamoDB, S3, Secrets Manager, ALB (gRPC target groups), WAF (Terraform or CloudFormation) | **built + unit-tested only, never applied to a real account** | [docs](https://bkraad47.github.io/ramen/how-tos/aws/) |
| **Local** — docker compose | CI e2e on every push | [`deploy/local/README.md`](deploy/local/README.md) |

Bring-up on GCP is `terraform apply` → `make push` → `helm upgrade --install` → add a zone and a group in the
console → Deploy. About 25 minutes, mostly waiting for GKE and the load balancer. Clients then point the bridge at
`<console_ip>:443 --tls --ca <pem> --group <g> --zone <z>`.

## Write your own tools

A group repo is any git repo with `mcp/tools/<name>/<name>.py` + `<name>.json` (and `resources/`, `prompts/`,
`requirements.txt`). Start from [ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group); the
contract is in [Protos](https://bkraad47.github.io/ramen/wiki/protos/). Secrets are referenced as
`{{$group.NAME}}` and substituted by the runtime at call time. Nothing about the transport leaks into tool code.

## Repository

| Dir | What |
|---|---|
| [`console/`](console/) | FastAPI + Jinja2 + HTMX manager UI and `/api/v1`; gRPC client to workers |
| [`node-rs/`](node-rs/) | Rust MCP server node (tonic: `ramen.v1.Mcp`, `ramen.v1.Admin`, `grpc.health.v1.Health`; auth, CIDRs, sidecar supervisor) |
| [`runtime-py/`](runtime-py/) | Python 3.14 runtime (loads protos, pip installs, runs calls, resolves secrets) and `ramen-mcp-bridge` |
| [`proto/`](proto/) | `ramen/v1/mcp.proto`, `admin.proto` — the transport contract, single source for Rust and Python stubs |
| [`deploy/`](deploy/) | compose, Helm charts, Terraform (GCP, AWS), CloudFormation |
| [`skills/`](skills/) | Cloud-ops agent skills: deploy-gcp, deploy-aws, rotate-keys, backup-restore, scale-zone |
| [`tests/`](tests/) | Black-box conformance (gRPC + bridge), e2e and cloud suites |
| [`docs/`](docs/) | This site's sources; [`docs/CONTRACTS.md`](docs/CONTRACTS.md) is binding for every component (§11 = transport) |

Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) · Changes: [CHANGELOG.md](CHANGELOG.md) · Versions: [tracker](https://bkraad47.github.io/ramen/versions/)

## Develop

```sh
make test            # runtime-py (pytest, ≥90 % cov) + node-rs (fmt, clippy, test) + console (pytest, ≥90 % cov)
make test-harness    # tests/: conformance + e2e (skips without a running stack)
make proto           # regenerate Python stubs from proto/ (Rust stubs build via tonic-build)
make demo-worker     # node + runtime locally without Docker
uv run --project docs --group docs mkdocs serve   # docs at http://127.0.0.1:8000
```

## License
BSD-3-Clause © 2026 Raad. See [LICENSE](LICENSE).
