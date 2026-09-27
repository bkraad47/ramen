<p align="center"><img src="docs/img/logo.png" width="200" alt="Project Ramen"></p>

<h1 align="center">Ramen</h1>
<p align="center">Multizone, highly available, enterprise-grade <b>MCP server</b> for GCP and AWS Kubernetes.<br>
Rust MCP node + Python 3.14 runtime workers, managed from a FastAPI console.</p>

<p align="center">
<a href="https://github.com/bkraad47/ramen/releases"><img src="https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release" alt="release"></a>
<a href="https://github.com/bkraad47/ramen/actions/workflows/ci.yml"><img src="https://github.com/bkraad47/ramen/actions/workflows/ci.yml/badge.svg" alt="ci"></a>
<a href="https://bkraad47.github.io/ramen/"><img src="https://github.com/bkraad47/ramen/actions/workflows/pages.yml/badge.svg" alt="docs"></a>
<a href="LICENSE"><img src="https://img.shields.io/badge/license-BSD--3--Clause-F26B3A" alt="license"></a>
<a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Streamable%20HTTP%202025--06--18-2B2622" alt="MCP"></a>
</p>

**Docs: https://bkraad47.github.io/ramen/** · [How it works](https://bkraad47.github.io/ramen/how-it-works/) · [GCP guide](https://bkraad47.github.io/ramen/how-tos/gcp/) · [AWS guide (untested)](https://bkraad47.github.io/ramen/how-tos/aws/) · [Contracts](docs/CONTRACTS.md) · [Releases](https://github.com/bkraad47/ramen/releases)

Ramen turns a **git repo of tools, resources and prompts** into a fleet of MCP workers behind a cloud load balancer.
Each worker pairs a **Rust MCP node** (protocol, bearer auth, IP allow-lists, metrics, logs) 1:1 with a **Python 3.14
runtime** that pip-installs and runs your code. One **console** manages groups (tenants), environments, zones,
secrets, canary deploys, rebalancing, IP rules, logs, audit and backups — in the browser or through an API key.

- **Git → bucket → worker.** Deploy syncs the repo to a bucket; workers load by content hash. No git creds on pods.
- **Canary by default.** Roll a canary, smoke-test `tools/list`, then roll stable. Failure leaves stable untouched.
- **Multi-zone from day one.** Group → Environment → Zone → Worker; `https://<lb>/mcp/<group>/<zone>`.
- **Enterprise controls.** Super admin / group admin / viewer, `rmk_` MCP keys, `rmn_` API keys, Cloud Armor / WAF
  IP rules, secrets that are never displayed, an audit line for every action.
- **Standard MCP.** Streamable HTTP, JSON-RPC 2.0, protocol `2025-06-18`. Claude Desktop, Cursor, any client.

## Quickstart (local, 5 commands)

Needs Docker (compose v2), `uv`, `git`, `make`. First run ≈ 3–5 min (image builds).

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up       # Firestore emulator + console https://localhost:8443 + one worker http://localhost:8080
make demo     # zone → group `demo` (demo repo) → mint rmk_ key → canary deploy → MCP tools/call
#   … demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5      ← success line
open https://localhost:8443    # self-signed cert; login admin@ramen.local / changeme-ramen
make down     # stop and remove volumes
```

`make demo` is idempotent (existing zone/group/env answer `… exists`; a fresh key is minted each run).
Then call the worker yourself with an **`rmk_` MCP key** (Groups → demo → *Mint MCP key* → Deploy):

```sh
curl -s http://localhost:8080/mcp -H "Authorization: Bearer rmk_…" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
```

> `rmk_…` **MCP keys** go to workers (`Authorization: Bearer`) and are minted on the **group page**.
> `rmn_…` **API keys** go to the console (`X-Ramen-Api-Key`) for automation and are minted on the **API Keys** page.
> They are not interchangeable. Full walkthrough: [local quickstart](https://bkraad47.github.io/ramen/how-tos/local-quickstart/)
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
| **GCP** — GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB (GKE Gateway), Cloud Armor | verified on a throwaway project; 89/89 cloud tests | [docs](https://bkraad47.github.io/ramen/how-tos/gcp/) · [`deploy/README.md`](deploy/README.md) |
| **AWS** — EKS, DynamoDB, S3, Secrets Manager, ALB, WAF (Terraform or CloudFormation) | **built + unit-tested only, never applied to a real account** | [docs](https://bkraad47.github.io/ramen/how-tos/aws/) |
| **Local** — docker compose | CI e2e on every push | [`deploy/local/README.md`](deploy/local/README.md) |

Bring-up on GCP is `terraform apply` → `make push` → `helm upgrade --install` → add a zone and a group in the
console → Deploy. About 25 minutes, mostly waiting for GKE and the load balancer.

## Write your own tools

A group repo is any git repo with `mcp/tools/<name>/<name>.py` + `<name>.json` (and `resources/`, `prompts/`,
`requirements.txt`). Start from [ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group); the
contract is in [Protos](https://bkraad47.github.io/ramen/wiki/protos/). Secrets are referenced as
`{{$group.NAME}}` and substituted by the runtime at call time.

## Repository

| Dir | What |
|---|---|
| [`console/`](console/) | FastAPI + Jinja2 + HTMX manager UI and `/api/v1` |
| [`node-rs/`](node-rs/) | Rust MCP server node (`POST /mcp`, auth, CIDRs, metrics, sidecar supervisor) |
| [`runtime-py/`](runtime-py/) | Python 3.14 runtime (loads protos, pip installs, runs calls, resolves secrets) |
| [`deploy/`](deploy/) | compose, Helm charts, Terraform (GCP, AWS), CloudFormation |
| [`skills/`](skills/) | Cloud-ops agent skills: deploy-gcp, deploy-aws, rotate-keys, backup-restore, scale-zone |
| [`tests/`](tests/) | Black-box conformance, e2e and cloud suites |
| [`docs/`](docs/) | This site's sources; [`docs/CONTRACTS.md`](docs/CONTRACTS.md) is binding for every component |

Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) · Changes: [CHANGELOG.md](CHANGELOG.md) · Versions: [tracker](https://bkraad47.github.io/ramen/versions/)

## Develop

```sh
make test            # runtime-py (pytest, ≥90 % cov) + node-rs (fmt, clippy, test) + console (pytest, ≥90 % cov)
make test-harness    # tests/: conformance + e2e (skips without a running stack)
make demo-worker     # node + runtime locally without Docker
uv run --project docs --group docs mkdocs serve   # docs at http://127.0.0.1:8000
```

## License
BSD-3-Clause © 2026 Raad. See [LICENSE](LICENSE).
