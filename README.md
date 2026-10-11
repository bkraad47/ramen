<p align="center"><img src="docs/img/logo.png" width="380" alt="Project Ramen"></p>

<p align="center">Build your MCP tools in Python, deploy them across zones on <b>GKE, EKS and AKS</b>.<br>
Rust MCP node + Python 3.12 runtime workers, Streamable HTTP at the edge and gRPC inside, guardrails per tool,<br>
Entra ID and Google Workspace sign-in, autoscaling — managed from one FastAPI console.</p>

<p align="center">
<a href="https://github.com/bkraad47/ramen/releases"><img height="20" src="https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release&style=flat" alt="release"></a>
<a href="https://github.com/bkraad47/ramen/actions/workflows/ci.yml"><img height="20" src="https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/ci.yml?branch=main&label=ci&style=flat" alt="ci"></a>
<a href="https://bkraad47.github.io/ramen/"><img height="20" src="https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/pages.yml?branch=main&label=docs&style=flat" alt="docs"></a>
<a href="LICENSE"><img height="20" src="https://img.shields.io/badge/license-BSD--3--Clause-F26B3A?style=flat" alt="license"></a>
<a href="https://modelcontextprotocol.io"><img height="20" src="https://img.shields.io/badge/MCP-Streamable%20HTTP%20%2B%20gRPC-2B2622?style=flat" alt="MCP"></a>
</p>

<p align="center"><a href="https://glama.ai/mcp/servers/bkraad47/ramen"><img width="380" height="200" src="https://glama.ai/mcp/servers/bkraad47/ramen/badge" alt="Ramen on Glama: score, tools and listing"></a></p>

**Docs: https://bkraad47.github.io/ramen/** · [Get started](https://bkraad47.github.io/ramen/get-started/) · [The MCP repo](https://bkraad47.github.io/ramen/wiki/mcp-repo/) · [Connect a client with OAuth](https://bkraad47.github.io/ramen/wiki/connect-oauth/) · [How it works](https://bkraad47.github.io/ramen/how-it-works/) · [Deploy on GCP](https://bkraad47.github.io/ramen/wiki/deploy-gcp/) · [Deploy on AWS](https://bkraad47.github.io/ramen/wiki/deploy-aws/) · [Deploy on Azure](https://bkraad47.github.io/ramen/wiki/deploy-azure/) · [Contracts](docs/CONTRACTS.md) · [Releases](https://github.com/bkraad47/ramen/releases)

> **Streamable HTTP is the front door.** Every worker serves `POST /mcp` — a URL and a bearer header, nothing to
> install — next to the gRPC service it has had since 0.3.1, on the same port, through the same guards. Phones,
> browsers and hosted agent platforms connect directly; the stdio bridge stays for clients that only speak stdio.
> Per-user access through OAuth (the console is the authorization server), live-verified on GKE since 0.5.1.

**What is true today, before the pitch.** Current release **0.7.5**. The local stack and CI prove both
transports on real node processes on Linux and Windows. 0.7.5 itself was deployed on **GKE, EKS and AKS** on
2026-10-10/11 and passed the same 33 checks on each: a canary deploy of the demo repo with its NeMo rails and golden
cases, custom roles and per-tool access, a person's token over Streamable HTTP and through the stdio bridge, and the
guardrail block on both. Microsoft Entra ID sign-in with group lookup ran live on all three (including removal from a
group and return), Google sign-in on GKE; Google Workspace *groups* are tested against a fake only. Autoscaling was
load-tested on all three (64 concurrent calls, up to the max and back to 1, no 5xx). Each environment was torn down
afterwards. Earlier releases did the same on GKE and AWS since 0.5.x. Everything below is written so those lines
stay findable.

**Build and deploy your own MCP tools across zones, on GKE, EKS or AKS.** Ramen turns a **git repo of tools, resources and prompts** into a fleet of MCP workers behind a cloud load balancer, and keeps who-can-call-what, secrets, canary gates and the audit trail in one console. It is a platform for *your* tools, not a gateway in front of someone else's.
Each worker pairs a **Rust MCP node** (Streamable HTTP and gRPC, bearer auth, IP allow-lists, health, logs) 1:1 with a
**Python 3.12 runtime** that pip-installs and runs your code. One **console** manages groups (tenants), environments,
zones, secrets, canary deploys, autoscaling, IP rules, logs, audit and backups — in the browser or through an API key.

- **Git → bucket → worker.** Deploy syncs the repo to a bucket; workers load by content hash. No git creds on pods.
- **Canary by default, gated.** Roll a canary, smoke-test `tools/list`, diff its tool schemas against stable
  (breaking changes stop unless you say `breaking: true`), run the repo's golden cases, then roll stable. Failure
  leaves stable untouched.
- **Multi-zone from day one.** Group → Environment → Zone → Worker; the LB routes on `ramen-group` / `ramen-zone`
  metadata, so one client config works for every zone.
- **Per-tool access and a drift signal (0.7.2).** Decide per environment who may see and who may call each tool (group
  keys, group admins, viewers, MCP users); clients learn about a rollout through `tools/list_changed`; the group page
  shows how often the same caller repeated the same call, with a warning threshold.
- **Guardrails inside the worker (0.7.5).** Opt a tool into a pre-call and post-call check that runs in the Python
  runtime next to it: [NVIDIA NeMo Guardrails](https://github.com/NVIDIA/NeMo-Guardrails) rails (deterministic or
  LLM-backed, your model and key) or a `policy.py` of your own. A blocked call is a readable tool error, the tool
  never ran or its output never left the pod, and the access log says which rail. Per tool, fail-closed by default.
- **Your identity provider decides, your admins override (0.7.5).** Add Microsoft Entra ID, Google Workspace or any
  OIDC provider on the Config page; read a person's groups from Graph or Cloud Identity at sign-in, map them to roles
  per group, and keep what admins set by hand. Any IdP on any cloud.
- **Custom roles (0.7.5).** Define `analyst` on the Viewer base, use it in memberships, provider rules and per-tool
  access; the person's token carries it to the worker. Mark a role **OAuth only** and its holders cannot use a password.
- **Autoscaling by Ramen (0.7.5).** Each zone runs between 1 worker and a max that super admins and group admins set.
  The console scales up fast under load and back down when quiet, the same on all three clouds, with no HPA and every
  step in the audit log. [How it decides](https://bkraad47.github.io/ramen/wiki/autoscaling/).
- **Three clouds (0.7.5 adds AKS).** Terraform and Helm for GKE (Gateway, Cloud Armor), EKS (ALB, WAF) and AKS (Envoy
  Gateway, Let's Encrypt, Table Storage, Key Vault, workload identity). The same console and guards on each.
- **Enterprise controls.** A role per group (Group Admin, Viewer, MCP User or one you define) plus global super admins,
  `rmk_` MCP keys and `rmn_` API keys stored as salted hashes, IP rules (per zone at the node, and at the edge: Cloud
  Armor, WAF or an Envoy policy), secrets that are never displayed, an audit line for every action.
- **Transport: Streamable HTTP at the edge, gRPC inside.** `POST /mcp` for any client that can make an HTTP
  request; `ramen.v1.Mcp/Call` for teams that want gRPC internally. One set of guards serves both — the same
  functions, spelled `401 / 403 / 429` on one and `UNAUTHENTICATED / PERMISSION_DENIED / RESOURCE_EXHAUSTED` on the
  other — so the two paths cannot drift.
- **Cheaper: one Deployment per zone, not one per tool.** A worker is one Rust node plus one Python runtime that loads *every* tool of
  the group. A team with thirty small tools runs them on **one Deployment per zone** (two pods with a canary),
  behind one load balancer. Container-per-MCP-server designs run thirty. Ramen's autoscaling adds pods for load, not
  for tool count.
- **Secure, in one sentence.** User code never runs in the process that holds the keys and does the auth: the Rust
  node checks every call and hands the message to a separate Python process it can kill and respawn.

## Built on how organizations work

Groups own tools in git, environments pin a ref and a set of zones, people hold a role per group, agents and
clients sign in as themselves, and every deploy is a canary, a smoke test and a rollout. Underneath it is gRPC,
JSON-RPC 2.0 and a Rust node; **you code in Python**. The longer argument is
[How it works and why](https://bkraad47.github.io/ramen/how-it-works/).

> **Start here** · the demo group repo **[ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group)**
> (point a group at it and press Deploy) · the stdio bridge **[ramen-mcp-bridge on PyPI](https://pypi.org/project/ramen-mcp-bridge/)**
> (`uvx ramen-mcp-bridge@latest`; signs you in with `--oauth` or carries a group key) · HTTP clients such as
> Claude Code, Cursor and VS Code need no bridge with a group key, and Claude Code can also
> [sign you in with OAuth](https://bkraad47.github.io/ramen/wiki/connect-oauth/). See [Connect a client](#connect-a-client).
> Every feature and where it is managed: [How it works](https://bkraad47.github.io/ramen/how-it-works/).

## Quickstart (local, 5 commands)

Needs Docker with compose v2, `uv`, `git` and `make`. The first run builds two images and takes three to five minutes.

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up       # Firestore emulator + console https://localhost:8443 + one worker (localhost:8080: Streamable HTTP + gRPC)
make demo     # zone, group `demo` from the demo repo, an rmk_ key, a canary deploy, then a tools/call
#   PASS: demo_calculator_tool(2,3,add) -> 5      <- the success line
open https://localhost:8443    # self-signed cert; login admin@ramen.local / REDACTED-PASSWORD
make down     # stop and remove volumes
```

`make demo` is safe to re-run: an existing zone, group or environment answers "exists" and a fresh key is
generated each time.
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
# its own package (github.com/bkraad47/ramen-mcp-bridge); uvx runs the newest release without installing it
RAMEN_MCP_KEY=<the key shown once> uvx ramen-mcp-bridge@latest --target localhost:8080 --insecure --group demo --zone local
```

> `rmk_` **MCP keys** go to workers (`Authorization: Bearer`, on HTTP or as gRPC metadata) and are generated on
> the **group page**. `rmn_` **API keys** go to the console (`X-Ramen-Api-Key`) for automation and are generated
> on the **API keys** page. They are not interchangeable. For a token scoped to one *person* rather than a shared
> key, register an OAuth client on the Config page: the worker's `401` tells an OAuth-capable client where to
> sign in. Full walkthrough with the `mcp` SDK and a raw `grpcurl` call:
> [Get started](https://bkraad47.github.io/ramen/get-started/)
> (also in [`deploy/local/README.md`](deploy/local/README.md)).

## Connect a client

Two ways in, through the same guards. **Streamable HTTP**: the client talks to `https://<edge>/mcp` itself. **The
bridge** (`ramen-mcp-bridge`): a small stdio program the client starts; it speaks gRPC to the edge and, for a person,
does the browser sign-in. Ramen has no dynamic client registration, so a person's OAuth sign-in works over HTTP in
Claude Code (it takes a client id) and through the bridge everywhere else. Admins do not have to type any of this:
the group page's **How MCP users sign in** card prints every command filled in for the install, zone by zone.

| Who signs in | Claude Code | Cursor, Claude Desktop | VS Code / GitHub Copilot |
|---|---|---|---|
| A group key (agents, CI) | HTTP: `claude mcp add --transport http … --header` | Cursor: HTTP, `url` + `headers`; Claude Desktop: bridge `--key` | HTTP: `type: http`, `url`, `headers` |
| A Ramen password account | HTTP with `oauth.clientId`, or the bridge `--oauth` | bridge `--oauth` | bridge `--oauth` |
| Microsoft Entra ID or Google Workspace | same as a password account: the console's login page hands off to the provider | bridge `--oauth` | bridge `--oauth` |

**Group key over Streamable HTTP**

```sh
claude mcp add --transport http ramen-demo-a https://<edge>/mcp \
  --header "Authorization: Bearer <rmk_ key>" --header "ramen-group: demo" --header "ramen-zone: a"
```
```json
{"mcpServers": {"ramen-demo-a": {"url": "https://<edge>/mcp",
  "headers": {"Authorization": "Bearer ${RAMEN_MCP_KEY}", "ramen-group": "demo", "ramen-zone": "a"}}}}
```
That is Cursor's `~/.cursor/mcp.json`; VS Code's `.vscode/mcp.json` takes the same entry under `"servers"` with
`"type": "http"`.

**A person, over Streamable HTTP (Claude Code).** A super admin registers a client on Config → OAuth clients
(redirect `http://localhost/callback`) and gives the person a role in the group; MCP User is enough.

```sh
claude mcp add-json ramen-demo-a '{"type":"http","url":"https://<edge>/mcp",
  "headers":{"ramen-group":"demo","ramen-zone":"a"},
  "oauth":{"clientId":"<client id>","scopes":"mcp:demo:a"}}'
```
Then `/mcp` in Claude Code and sign in: a Ramen password, or the Entra ID / Google button when a super admin has added
the provider; a role marked OAuth-only allows only the provider.

**Through the bridge, with a key or as a person**

```sh
claude mcp add ramen-demo-a -- uvx ramen-mcp-bridge@latest --target <edge>:443 --tls --key <rmk_ key> --group demo --zone a
claude mcp add ramen-demo-a -- uvx ramen-mcp-bridge@latest --target <edge>:443 --tls \
  --oauth https://<edge> --client-id <client id> --group demo --zone a
```
```json
{"mcpServers": {"ramen-demo-a": {"command": "uvx", "args": ["ramen-mcp-bridge@latest",
  "--target", "<edge>:443", "--tls", "--oauth", "https://<edge>", "--client-id", "<client id>",
  "--group", "demo", "--zone", "a"]}}}
```
The JSON is Cursor's and Claude Desktop's; in VS Code put it under `"servers"` with `"type": "stdio"`. Bridge 0.3.0
keeps a person's token in memory (`--keychain` for the OS keychain, `--token-file` for automation), forgets it after
30 idle minutes, and when a sign-in is needed mid-session answers the call with error `-32001` and the sign-in link,
so the AI client can hand it to the person. `@latest` keeps a cached uvx from running an old bridge. Details:
[Connect with OAuth](https://bkraad47.github.io/ramen/wiki/connect-oauth/) ·
[Entra ID and Google Workspace](https://bkraad47.github.io/ramen/wiki/sso/) ·
[Transport](https://bkraad47.github.io/ramen/wiki/transport/#connecting).

## Screenshots

| Login | Dashboard, load per zone and group |
|---|---|
| ![login](docs/img/login.png) | ![dashboard](docs/img/dashboard.png) |

| Group: environments, deploy jobs, zones/workers, MCP keys | Deploy job with streamed log |
|---|---|
| ![group](docs/img/group.png) | ![deploy job](docs/img/deploy-job.png) |

| Secrets (names only, values never shown) | Logs (one JSON line per MCP call, downloadable) |
|---|---|
| ![secrets](docs/img/secrets.png) | ![logs](docs/img/logs.png) |

| Identity providers: Entra ID with group lookup and role rules | Custom roles, one OAuth-only |
|---|---|
| ![identity providers](docs/img/config-identity.png) | ![roles](docs/img/config-roles.png) |

| Zone actions: count, max, last autoscale decision | Zones and workers with the max per group |
|---|---|
| ![autoscale](docs/img/group-autoscale.png) | ![zones](docs/img/zones.png) |

## Transport, and what secures each hop

Workers speak **Streamable HTTP** (`POST /mcp`, one JSON-RPC 2.0 message per request, MCP spec 2025-06-18) **and
gRPC** (`ramen.v1.Mcp/Call`, one message as `bytes body`) on the same port ([contract §16](docs/CONTRACTS.md),
[§11](docs/CONTRACTS.md)). MCP itself is unchanged — your client and your tools see the standard messages. The two
transports share one implementation of every check: the HTTP handler turns the request headers into the same
metadata map and calls the same guard and dispatch functions the gRPC service calls, so a check added to one is on
both or on neither.

**Why the node is Rust.** A worker runs two processes with one job each. `ramen-node` (Rust + tonic) owns what must
not be slowed down or broken by user code: the gRPC surface, key checking, source-range checking, the blocked-name
filter, concurrency bounds, deadlines, health and the access log. It is a small static binary with no interpreter
and no user code in its address space. `ramen_runtime` (Python 3.12) owns what users write: `pip install`, validation,
secret substitution, the call. They talk over newline-delimited JSON-RPC on stdin/stdout ([§2](docs/CONTRACTS.md)),
so there is no extra socket to secure, and the runtime is killed after an idle timeout — a crash or leak in tool
code costs one respawn, not the process holding the keys.

| Hop | What protects it |
|---|---|
| client → edge (HTTP) | TLS at the load balancer; the credential in `Authorization: Bearer` (an `rmk_` key or a per-user OAuth token); browser origins only from the zone's allowlist; session ids signed and bound to the credential |
| client → bridge → edge (stdio) | a child process on the client's own machine, speaking gRPC to the edge; plaintext unless `--tls` (`--ca <pem>` pins the certificate) |
| edge → node | TLS ends at the load balancer; h2c to the node unless the node has its own certificate; Cloud Armor (GCP) or WAF (AWS) IP rules, one policy per group; an Envoy `SecurityPolicy` per zone (AKS) |
| every `Mcp/Call` | constant-time key compare; source range against the right `x-forwarded-for` entry; blocked names; 4 MiB and in-flight caps |
| edge → node, without a key | only `grpc.health.v1.Health`; reflection is off on deployed workers |
| anything else → the pod | the worker `NetworkPolicy` plus a hardened container context |
| node → runtime | stdio inside the pod; no network surface |
| runtime → bucket | the zone's own cloud identity (GCP service account with Workload Identity, AWS IAM role with IRSA, Azure managed identity with workload identity), scoped to the group's prefix (on Azure the container) and secrets |
| runtime → your tool | opt-in guardrails per tool: NeMo rails or a `policy.py` before the call and after the result; secret values never reach a rail |
| console → node | cluster-internal, never through the load balancer; `Admin/*` needs an admin key and an admin CIDR |

Five details behind that table matter in practice. The origin allowlist is empty by default, so every browser
`Origin` is refused until you add one. The source-range check reads the `x-forwarded-for` entry a proxy appended
(hop count 2 on GCP, 1 on AWS and AKS), and a wrong count denies rather than admits. The allowlist itself defaults to
everything until you set IP rules. An IP lock must include the console's own range, because a deploy smoke-tests
`tools/list` as an ordinary call. `grpc.health.v1.Health` is deliberately unauthenticated so load balancers can
probe it, and reports `SERVING` only once the runtime has loaded the group's code.

**What is verified, in five lines.**
- Both transports through every guard, on real node processes, in CI on Linux and on a Windows runner that builds
  the node natively (0.5.0).
- 0.7.5 live on GKE, EKS and AKS: the 33-check run on each, Entra ID sign-in with group lookup on each, autoscaling
  load tests on each, every environment torn down afterwards.
- The GCP and AWS paths live every release since 0.5.x, with two zones, OAuth, the Redis throttle and real teardown
  in 0.6.0 and 0.6.1.
- Covered by tests only, never on a real load balancer: node TLS, and the size and in-flight caps.
- By design, a group's code runs in a process that holds that group's secrets. Isolation between groups is the
  pod, the namespace and the per-zone identity.

Full write-up: [Transport and what secures each hop](https://bkraad47.github.io/ramen/wiki/transport/).

## Deploy to the cloud

| Target | Status | Guide |
|---|---|---|
| **GCP** — GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB (GKE Gateway, header-routed gRPC **and** Streamable HTTP, gRPC health checks), Cloud Armor | verified on a throwaway project every release, most recently 0.7.5: 33 checks, Entra ID and Google sign-in, autoscaling load tests | [docs](https://bkraad47.github.io/ramen/wiki/deploy-gcp/) · [`deploy/README.md`](deploy/README.md) |
| **AWS** — EKS, DynamoDB, S3, Secrets Manager, ALB (gRPC and HTTP/1.1 target groups), WAF (Terraform or CloudFormation) | applied to a real account since 0.5.6, most recently 0.7.5: 33 checks, Entra ID sign-in, autoscaling load tests | [docs](https://bkraad47.github.io/ramen/wiki/deploy-aws/) |
| **Azure** — AKS (workload identity), Table Storage, Blob Storage, Key Vault, Envoy Gateway (Gateway API, header-routed gRPC and Streamable HTTP) with a Let's Encrypt certificate | new in 0.7.5, verified live in `westus2`: 33 checks, Entra ID sign-in, autoscaling load tests | [docs](https://bkraad47.github.io/ramen/wiki/deploy-azure/) |
| **Local** — docker compose | CI e2e on every push | [`deploy/local/README.md`](deploy/local/README.md) |

Bring-up is the same shape on each cloud. On GCP it is `terraform apply` → `make push` → `helm upgrade --install` → add a zone and a group in the
console → Deploy. About 25 minutes, mostly waiting for GKE and the load balancer. The load balancer gets a
publicly-trusted certificate automatically (a free `sslip.io` hostname derived from the static IP — no domain to
buy, since 0.5.5; on AKS from Let's Encrypt, since 0.7.5). Clients then use `https://<public_hostname>/mcp` with `Authorization: Bearer rmk_` and the
`ramen-group` / `ramen-zone` headers (the same address serves the console and, by those headers, every zone);
stdio-only clients point the bridge at `<public_hostname>:443 --tls` — no `--ca`, nothing to import.

## Write your own tools

Full guide with the demo repo, `env.yaml` and local development: [The MCP repo, structure and local development](https://bkraad47.github.io/ramen/wiki/mcp-repo/).

A group repo is any git repo with `mcp/tools/<name>/<name>.py` + `<name>.json` (and `resources/`, `prompts/`,
`requirements.txt`). Start from [ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group); the
contract is in [the MCP repo page](https://bkraad47.github.io/ramen/wiki/mcp-repo/). Secrets are referenced as
`{{$group.NAME}}` and substituted by the runtime at call time. Nothing about the transport leaks into tool code.
Declare `output` and the worker publishes it as `outputSchema` and validates every result; add `mcp/tests.yaml` and
your cases gate every deploy (0.7.0). Add `mcp/guardrails.yaml` and NeMo rails or a `policy.py` check the arguments
before a tool runs and its result before it leaves the worker (0.7.5, [Guardrails](https://bkraad47.github.io/ramen/wiki/guardrails/)).

## Repository

| Dir | What |
|---|---|
| [`console/`](console/) | FastAPI + Jinja2 + HTMX manager UI and `/api/v1`; gRPC client to workers |
| [`node-rs/`](node-rs/) | Rust MCP server node (tonic: `ramen.v1.Mcp`, `ramen.v1.Admin`, `grpc.health.v1.Health`; auth, CIDRs, sidecar supervisor) |
| [`runtime-py/`](runtime-py/) | Python 3.12 runtime (loads protos, pip installs, runs calls, resolves secrets, runs guardrails) |
| [`proto/`](proto/) | `ramen/v1/mcp.proto`, `admin.proto` — the transport contract, single source for Rust and Python stubs |
| [`deploy/`](deploy/) | compose, Helm charts, Terraform (GCP, AWS, Azure), CloudFormation |
| [`skills/`](skills/) | Cloud-ops agent skills: deploy-gcp, deploy-aws, rotate-keys, backup-restore, scale-zone |
| [`tests/`](tests/) | Black-box conformance (gRPC + bridge), e2e and cloud suites |
| [`docs/`](docs/) | This site's sources; [`docs/CONTRACTS.md`](docs/CONTRACTS.md) is binding for every component (§11 = transport) |

Architecture: [ARCHITECTURE.md](ARCHITECTURE.md) · Changes: [CHANGELOG.md](CHANGELOG.md) · Versions: [tracker](https://bkraad47.github.io/ramen/versions/)

## Develop

```sh
make test            # runtime-py and console (pytest, 90% coverage gate) plus node-rs (fmt, clippy, test)
make test-harness    # tests/: conformance + e2e (skips without a running stack)
make proto           # regenerate Python stubs from proto/ (Rust stubs build via tonic-build)
make demo-worker     # node + runtime locally without Docker
uv run --project docs --group docs mkdocs serve   # docs at http://127.0.0.1:8000
```

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) — use it, fork it, change it, with attribution; renaming it as a new commercial product of your own is not acceptable. Related repositories and which versions go together: [Releases](https://bkraad47.github.io/ramen/release/).

## License
BSD-3-Clause © 2026 Raad. See [LICENSE](LICENSE).
