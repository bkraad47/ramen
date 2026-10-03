<p align="center"><img src="docs/img/logo.png" width="380" alt="Project Ramen"></p>

<p align="center">Multizone, highly available, enterprise-grade <b>MCP server</b> for GCP and AWS Kubernetes.<br>
Rust MCP node + Python 3.14 runtime workers, Streamable HTTP at the edge and gRPC inside, managed from a FastAPI console.</p>

<p align="center">
<a href="https://github.com/bkraad47/ramen/releases"><img height="20" src="https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release&style=flat" alt="release"></a>
<a href="https://github.com/bkraad47/ramen/actions/workflows/ci.yml"><img height="20" src="https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/ci.yml?branch=main&label=ci&style=flat" alt="ci"></a>
<a href="https://bkraad47.github.io/ramen/"><img height="20" src="https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/pages.yml?branch=main&label=docs&style=flat" alt="docs"></a>
<a href="LICENSE"><img height="20" src="https://img.shields.io/badge/license-BSD--3--Clause-F26B3A?style=flat" alt="license"></a>
<a href="https://modelcontextprotocol.io"><img height="20" src="https://img.shields.io/badge/MCP-Streamable%20HTTP%20%2B%20gRPC-2B2622?style=flat" alt="MCP"></a>
</p>

**Docs: https://bkraad47.github.io/ramen/** · [End to end, console → AI client](https://bkraad47.github.io/ramen/how-tos/end-to-end/) · [Add and deploy a tool](https://bkraad47.github.io/ramen/how-tos/add-a-tool/) · [How it works](https://bkraad47.github.io/ramen/how-it-works/) · [Transport and security](https://bkraad47.github.io/ramen/wiki/transport/) · [GCP guide](https://bkraad47.github.io/ramen/how-tos/gcp/) · [AWS guide (untested)](https://bkraad47.github.io/ramen/how-tos/aws/) · [Contracts](docs/CONTRACTS.md) · [Releases](https://github.com/bkraad47/ramen/releases)

> **Streamable HTTP is the front door.** Every worker serves `POST /mcp` — a URL and a bearer header, nothing to
> install — next to the gRPC service it has had since 0.3.1, on the same port, through the same guards. Phones,
> browsers and hosted agent platforms connect directly; the stdio bridge stays for clients that only speak stdio.
> Per-user access through OAuth (the console is the authorization server), live-verified on GKE since 0.5.1.

**What is true today, before the pitch.** The local stack and CI prove both transports on real node processes on
Linux and Windows. One GKE cluster has proved the gRPC path end to end (0.3.2, 0.4.0) and, separately, the HTTP
path + OAuth end to end through the same load balancer (0.5.1: harness 218 passed, 0 failed, 15 skipped with
stated reasons — publicly-trusted TLS via a Google-managed cert since 0.5.5, no self-signed warning). The AWS
path has **never** been applied to a real account. Everything below is written so those lines stay findable.

Ramen turns a **git repo of tools, resources and prompts** into a fleet of MCP workers behind a cloud load balancer.
Each worker pairs a **Rust MCP node** (Streamable HTTP and gRPC, bearer auth, IP allow-lists, health, logs) 1:1 with a
**Python 3.14 runtime** that pip-installs and runs your code. One **console** manages groups (tenants), environments,
zones, secrets, canary deploys, rebalancing, IP rules, logs, audit and backups — in the browser or through an API key.

- **Git → bucket → worker.** Deploy syncs the repo to a bucket; workers load by content hash. No git creds on pods.
- **Canary by default.** Roll a canary, smoke-test `tools/list`, then roll stable. Failure leaves stable untouched.
- **Multi-zone from day one.** Group → Environment → Zone → Worker; the LB routes on `ramen-group` / `ramen-zone`
  metadata, so one client config works for every zone.
- **Enterprise controls.** Super admin / group admin / viewer, `rmk_` MCP keys, `rmn_` API keys, IP rules (per
  zone at the node, one Cloud Armor policy per group at the edge), secrets that are never displayed, an audit
  line for every action.
- **Transport: Streamable HTTP at the edge, gRPC inside.** `POST /mcp` for any client that can make an HTTP
  request; `ramen.v1.Mcp/Call` for teams that want gRPC internally. One set of guards serves both — the same
  functions, spelled `401 / 403 / 429` on one and `UNAUTHENTICATED / PERMISSION_DENIED / RESOURCE_EXHAUSTED` on the
  other — so the two paths cannot drift.
- **Cheaper, and here is the number.** A worker is one Rust node plus one Python runtime that loads *every* tool of
  the group. A team with thirty small tools runs them on **one Deployment per zone** (two pods with a canary),
  behind one load balancer. Container-per-MCP-server designs run thirty. Autoscaling adds pods for load, not for
  tool count.
- **Secure, in one sentence.** User code never runs in the process that holds the keys and does the auth: the Rust
  node checks every call and hands the message to a separate Python process it can kill and respawn.

## Quickstart (local, 5 commands)

Needs Docker (compose v2), `uv`, `git`, `make`. First run ≈ 3–5 min (image builds).

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up       # Firestore emulator + console https://localhost:8443 + one worker (localhost:8080: Streamable HTTP + gRPC)
make demo     # zone → group `demo` (demo repo) → generate rmk_ key → canary deploy → tools/call over http://localhost:8080/mcp
#   … demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5      ← success line
open https://localhost:8443    # self-signed cert; login admin@ramen.local / changeme-ramen
make down     # stop and remove volumes
```

`make demo` is idempotent (existing zone/group/env answer `… exists`; a fresh key is generated each run).
Then connect your own client with an **`rmk_` MCP key** (Groups → demo → *Generate key* → Deploy). It is a URL and
a header — put the key in `RAMEN_MCP_KEY` and drop this into any `mcpServers` config:

```json
{"mcpServers": {"ramen-demo": {"url": "http://localhost:8080/mcp",
  "headers": {"Authorization": "Bearer ${RAMEN_MCP_KEY}", "ramen-group": "demo", "ramen-zone": "local"}}}}
```

Anything that can make an HTTP request is a client:
```sh
curl -s http://localhost:8080/mcp -H "Authorization: Bearer $RAMEN_MCP_KEY" -H 'Content-Type: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: local' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'
# {"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"5"}],"isError":false}}
```

Clients that only speak stdio use the bridge, which forwards to the same worker over gRPC:
```sh
pip install ramen-mcp-bridge                # its own package (github.com/bkraad47/ramen-mcp-bridge); also in the worker image
RAMEN_MCP_KEY=rmk_… ramen-mcp-bridge --target localhost:8080 --insecure --group demo --zone local
```

> `rmk_…` **MCP keys** go to workers (`Authorization: Bearer`, on HTTP or as gRPC metadata) and are generated on
> the **group page**. `rmn_…` **API keys** go to the console (`X-Ramen-Api-Key`) for automation and are generated
> on the **API keys** page. They are not interchangeable. For a token scoped to one *person* rather than a shared
> key, register an OAuth client on the API keys page: the worker's `401` tells an OAuth-capable client where to
> sign in. Full walkthrough with the `mcp` SDK and a raw `grpcurl` call:
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

## Transport, and what secures each hop

Workers speak **Streamable HTTP** (`POST /mcp`, one JSON-RPC 2.0 message per request, MCP spec 2025-06-18) **and
gRPC** (`ramen.v1.Mcp/Call`, one message as `bytes body`) on the same port ([contract §16](docs/CONTRACTS.md),
[§11](docs/CONTRACTS.md)). MCP itself is unchanged — your client and your tools see the standard messages. The two
transports share one implementation of every check: the HTTP handler turns the request headers into the same
metadata map and calls the same guard and dispatch functions the gRPC service calls, so a check added to one is on
both or on neither.

**Why the node is Rust.** A worker runs two processes with one job each. `ramen-node` (Rust + tonic) owns what must
not be slowed down or broken by user code: the gRPC surface, key checking, source-range checking, the blocked-name
filter, concurrency bounds, deadlines, health and the access log — a small static binary with no interpreter and no
user code in its address space. `ramen_runtime` (Python 3.14) owns what users write: `pip install`, validation,
secret substitution, the call. They talk over newline-delimited JSON-RPC on stdin/stdout ([§2](docs/CONTRACTS.md)),
so there is no extra socket to secure, and the runtime is killed after an idle timeout — a crash or leak in tool
code costs one respawn, not the process holding the keys.

| Hop | What protects it |
|---|---|
| client → edge (HTTP) | `https://<edge>/mcp`, the credential in `Authorization: Bearer` — an `rmk_` key, or a console-issued OAuth token for one user; a browser origin must be on the zone's `RAMEN_ALLOWED_ORIGINS` list (empty by default: every `Origin` refused, so a DNS-rebinding page cannot reach a worker); sessions are signed ids bound to the credential, so a stolen `Mcp-Session-Id` is useless under another key; the local compose worker is plain `http://` on a laptop, and nothing else should be |
| client → bridge → edge (stdio) | a child process on the client's own machine; the bridge speaks gRPC to the edge, **plaintext h2c unless you ask for TLS** (`--tls`, or `--ca <pem>` to pin the certificate); the key comes from `RAMEN_MCP_KEY` |
| edge → node | TLS terminates at the load balancer; LB → node is h2c unless the node runs its own TLS (`RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`, which also needs `RAMEN_WORKER_TLS=1` on the console). Cloud Armor IP rules apply here on GCP — **one policy per group**, not per zone; the AWS ALB and WAF equivalents are written but have never been applied |
| every `Mcp/Call` | bearer key compared byte-for-byte in constant time, folded over every configured key with no early exit (the length check in front of that compare is not constant time, so a key's length can leak), empty key set denies all (`UNAUTHENTICATED`); `RAMEN_ALLOWED_CIDRS` source check (`PERMISSION_DENIED`) against the `RAMEN_TRUST_PROXY_HOPS`-th `x-forwarded-for` entry counted from the right (GCP 2, AWS 1, `0` = the peer address), which is the entry a proxy appended and therefore not one a caller can choose — a wrong count falls back to the peer and denies; note the allowlist itself defaults to `0.0.0.0/0` + `::/0` when unset, so set IP rules to make it a real gate; blocked names filtered from `*/list` and answered `-32601`; 4 MiB cap; `RAMEN_MAX_INFLIGHT` cap |
| edge → node, without a key | `grpc.health.v1.Health` answers with no key and no source-range check — by design, so load balancers and Kubernetes can probe it. gRPC server reflection would also expose the service list, so it is switched off on deployed workers (`RAMEN_REFLECTION=0` in the chart and both renderers) and answers `UNIMPLEMENTED` there; it stays on locally |
| anything else → the pod | the worker `NetworkPolicy` (on by default) admits the node port only from the console namespace and the load-balancer / health-check ranges; with the hardened container context (non-root, no privilege escalation, all capabilities dropped, `RuntimeDefault` seccomp) this is what bounds direct access, and what closed SEC-04 |
| node → runtime | stdio inside the pod; no network surface |
| runtime → bucket | the zone's own cloud identity — on GCP a service account with `objectViewer` on that group's bucket prefix and `secretAccessor` on that group's secrets, via Workload Identity. IAM + IRSA on AWS is written but has never been applied |
| console → node | cluster-internal, straight to the pod IP, plaintext h2c unless the console has `RAMEN_WORKER_TLS=1`; `Admin/*` also needs `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` and is **not** routed through the load balancer. An IP lock must still include the console's own range, because a deploy smoke-tests `tools/list` as an ordinary `Mcp/Call` |

`grpc.health.v1.Health` is deliberately unauthenticated so load balancers and Kubernetes can probe it, and reports
`SERVING` only once the runtime has loaded the group's code.

**Verified in 0.5.0 on real node processes, and in 0.5.1 live on GKE through the load balancer** (Streamable HTTP with the key, the OAuth flow from consent to a tool call with the token, refresh rotation and reuse revocation, the RFC 9728 discovery; harness 218 passed, 0 failed, 15 skipped with stated reasons): both transports through every guard (key, source
range, blocked names, size and in-flight caps, protocol errors as JSON-RPC bodies), the HTTP-only checks (Origin,
sessions bound to the credential, content negotiation, protocol version, the RFC 9728 metadata), console-issued
OAuth tokens accepted by the node, and the official `mcp` SDK's Streamable HTTP client end to end — in CI on Linux
**and on a Windows runner that builds the node natively**, which is where fresh-machine client setups usually break.
**Verified live** on one GKE Autopilot cluster in `us-central1` (0.3.2), whole harness green through the load
balancer (126 passed, 0 failed): header routing on `ramen-group` / `ramen-zone` over h2c, gRPC health checks,
`Mcp/Call` with and without a key, `Admin/*` unreachable through the load balancer, the bridge end to end through
the official `mcp` SDK stdio client with `--tls --ca`, and — driven from the console against the real deployment —
tool blocking and unblocking, an IP lock and its restore, canary, rebalance, the per-zone service account's bucket
and secret IAM bindings, secrets and logs. Scope: one cluster, one zone, one group, one day, then deleted; nothing
has run in a cloud since. **Tested but not run in a cloud:** node TLS, the size and in-flight caps,
`x-forwarded-for` hop counting (including that a caller-supplied header cannot move the address the node checks).
The hop counts and `RAMEN_REFLECTION=0` landed after that cloud run, so neither has met a real load balancer yet.
**Not verified at all:** the entire AWS path — EKS, ALB gRPC target groups and WAF have never been applied to a real
account. **By design:** the runtime executes a group's code in a process that holds that group's secrets in its
environment; isolation between groups is the pod, the namespace and the per-zone identity, not the Python process.

Full write-up: [Transport and what secures each hop](https://bkraad47.github.io/ramen/wiki/transport/).

## Deploy to the cloud

| Target | Status | Guide |
|---|---|---|
| **GCP** — GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB (GKE Gateway, header-routed gRPC **and** Streamable HTTP, gRPC health checks), Cloud Armor | verified on throwaway projects: 0.3.0 infrastructure, 0.3.2 gRPC header routing over h2c end to end through the Gateway (harness 126 passed, 0 failed), 0.5.1 Streamable HTTP and OAuth through the same Gateway (harness 218 passed, 0 failed, 15 skipped with stated reasons), each on one cluster in `us-central1` | [docs](https://bkraad47.github.io/ramen/how-tos/gcp/) · [`deploy/README.md`](deploy/README.md) |
| **AWS** — EKS, DynamoDB, S3, Secrets Manager, ALB (gRPC target groups), WAF (Terraform or CloudFormation) | **built + unit-tested only, never applied to a real account** | [docs](https://bkraad47.github.io/ramen/how-tos/aws/) |
| **Local** — docker compose | CI e2e on every push | [`deploy/local/README.md`](deploy/local/README.md) |

Bring-up on GCP is `terraform apply` → `make push` → `helm upgrade --install` → add a zone and a group in the
console → Deploy. About 25 minutes, mostly waiting for GKE and the load balancer. The load balancer gets a
publicly-trusted certificate automatically (a free `sslip.io` hostname derived from the static IP — no domain to
buy, since 0.5.5). Clients then use `https://<public_hostname>/mcp` with `Authorization: Bearer rmk_…` and the
`ramen-group` / `ramen-zone` headers (the same address serves the console and, by those headers, every zone);
stdio-only clients point the bridge at `<public_hostname>:443 --tls` — no `--ca`, nothing to import.

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
| [`runtime-py/`](runtime-py/) | Python 3.14 runtime (loads protos, pip installs, runs calls, resolves secrets) |
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
