# Architecture (current: v0.5.4)

Hierarchy: **Group → Environment → Zone → Worker** (decision D5). Transport: **Streamable HTTP at the edge, gRPC inside** (D31; gRPC since D19).

```
                         ┌──────────────────────────── console (FastAPI, 1 node) ────────────────────────────┐
  browser / rmn_ key ──▶ │ auth (password, OAuth/OIDC+PKCE, magic link) · RBAC · groups/envs/zones · secrets │
                         │ deploy jobs · rebalance · IP rules · logs · audit · backups · config · policy      │
                         └──────┬──────────────────────┬──────────────────────────────┬─────────────────────┘
                                │ Store                │ Cloud adapter                │ Secrets backend
                                ▼                      ▼                              ▼
                     Firestore | DynamoDB | memory   local | gcp | aws           store | gcp | aws
                                                       │ namespaces, Deployments, Secrets, Roles, routes,
                                                       │ NEG/target groups, Cloud Armor / WAF, IAM
                                                       │ gRPC to pods: Admin/Reload, Admin/Metrics, Health/Check
                                                       ▼
   MCP client ─▶ POST /mcp (Streamable HTTP, bearer key or OAuth token) ──────────────────────────────▶ namespace ramen-<group>-<zone>
   stdio client ─▶ ramen-mcp-bridge ─▶ LB (GKE Gateway | ALB) ─ headers ramen-group, ramen-zone ─▶ (same pods, same guards)
   (stdio)       (gRPC, TLS)         gRPC health check                                              ├─ worker (stable)   ─┐ ramen-node (Rust, tonic) :8080
   grpcurl / any gRPC client ────────────────────────────────────────────────────────────────────▶  └─ worker-canary     ─┘  └─ ramen_runtime (Python 3.14)
                                                                                                                                 ▲ sync on load
                                                                                                                 bucket gs:// | s3:// | /buckets/<group>
```

## Components
| Dir | What | Tech | Contract |
|---|---|---|---|
| `console/` | Manager UI + `/api/v1`; gRPC client to workers (`ramen_console.grpcclient`) | FastAPI, Jinja2, HTMX (no JS build), argon2, itsdangerous, Fernet, grpcio | [§4, §4a, §11](../CONTRACTS.md) |
| `node-rs/` | MCP server node: `ramen.v1.Mcp/Call`, `ramen.v1.Admin`, `grpc.health.v1.Health` on one h2c/TLS port; bearer auth, CIDR allow-lists, blocked names, in-flight limit, access log, sidecar supervisor | Rust 1.98, tonic, tokio | [§11](../CONTRACTS.md) |
| `runtime-py/` | Loads `mcp/` packages from the bucket, validates protos, pip-installs requirements, executes calls, resolves secrets; ships `ramen-mcp-bridge` (stdio ⇄ gRPC) | Python 3.14, jsonschema, grpcio (`[grpc]` extra) | [§1, §2, §11](../CONTRACTS.md) |
| `proto/ramen/v1/` | `mcp.proto`, `admin.proto` — single source for Rust (tonic-build) and Python (grpcio-tools) stubs | protobuf 3 | [§11](../CONTRACTS.md) |
| `deploy/local` | docker compose: Firestore emulator + console + one worker (gRPC h2c on 8080) | | [§5, §11](../CONTRACTS.md) |
| `deploy/terraform/gcp`, `deploy/helm/*` | GKE Autopilot, Firestore, GCS, Artifact Registry, static IP, GSA + Workload Identity (custom role, resource-level IAM), GKE Gateway with header routes + gRPC health | Terraform, Helm 4 | [§7, §11](../CONTRACTS.md) |
| `deploy/terraform/aws`, `deploy/cloudformation` | EKS, DynamoDB, S3, Secrets Manager, ECR, IRSA, ALB controller (gRPC target groups), Fluent Bit (**untested**) | Terraform, CloudFormation | [§8, §11](../CONTRACTS.md) |
| `tests/` | Black-box conformance + e2e + cloud suites over gRPC, plus the bridge via the official `mcp` stdio client | pytest, grpcio, `mcp` | [§11](../CONTRACTS.md) |
| `skills/` | Cloud-ops agent skills (deploy, rotate, backup, scale) | agentskills `SKILL.md` | [§10](../CONTRACTS.md) |

## Data model
| Collection | Key fields | Notes |
|---|---|---|
| `users` | email, role (`super_admin` / `group_admin` / `viewer`), groups | password hash Fernet-encrypted |
| `groups` | name, repo_url, ref, mcp_auth, sa_restrictions | one bucket prefix per group |
| `environments` | group, name, ref, zones[], verbose, blocked[], last_deploy | zone list drives namespaces |
| `zones` | name, provider (`local` / `gcp` / `aws`), region (cloud zone) | super admin only |
| `workers` | group, zone, count, size (`s`/`m`/`l`), allowed_sizes, cidrs, service_account | live pods come from the adapter |
| `secrets` | group, name, env?, zone?, kind (`secret` / `mcp_key`), value or ref | value never returned |
| `api_keys` | name, role, groups, hash | `rmn_<id>_<secret>`, shown once |
| `audit` / `activity` | ts, user, ip, action, target, ok, tags | every mutation; permission requests live in `activity` |
| `config`, `backups` | auth toggles, SA rules; backup metadata | backups exclude secrets |

## Request paths
- **MCP call**: client (`POST /mcp` over Streamable HTTP, a gRPC client, or the stdio bridge) → LB matches
  `ramen-group` + `ramen-zone` headers → node checks CIDR, Origin (HTTP), then the `rmk_` key or an OAuth token
  (constant-time) → runtime (spawned on demand) → tool → JSON-RPC result in the reply body. Transport failures are
  gRPC statuses (`UNAUTHENTICATED`, `PERMISSION_DENIED`, `RESOURCE_EXHAUSTED`) or their HTTP twins (401, 403,
  429); protocol failures stay JSON-RPC errors. One log line per call (`ts, ip, transport, group, method, name,
  status, grpc_code, ms, key_id`); `RAMEN_VERBOSE=1` logs bodies.
- **Deploy**: console job → clone repo (token via git header) → upload bucket → write zone Secret/env file
  (`RAMEN_MCP_KEYS`, `RAMEN_SECRET_*`, `RAMEN_BLOCKED`, `RAMEN_ALLOWED_CIDRS`) → canary restart → `Health/Check`
  `SERVING` → `Admin/Reload` → `tools/list` smoke → stable restart. See [Canary](../wiki/canary.md).
- **Rebalance / IP rules**: console → cloud API (backend capacity, Cloud Armor / WAF) + node env → worker roll.
  Load comes from `Admin/Metrics`. See [Rebalance](../wiki/rebalance.md).

## Decisions
| ID | Decision |
|---|---|
| D2 | State DB: Firestore on GCP, DynamoDB on AWS, one repository interface. No Postgres. |
| D3 | Worker pod = Rust node + Python runtime sidecar, 1:1, spawned on demand, idle-terminated. |
| D4 | *(superseded by D19)* Transport: JSON-RPC 2.0 over HTTP (MCP Streamable HTTP), 0.1.0–0.3.0. |
| D5 | Hierarchy Group → Environment → Zone → Worker. |
| D6 | One zone first; every layer multi-zone capable. |
| D9 | Semver from 0.1.0; tags `v*` build releases with zips. |
| D11 | Console: FastAPI + Jinja2 + HTMX, server-rendered. |
| D14 | Bucket dir = group repo root; no MCP keys = deny all; node config precedence file < env < deploy file. |
| D17 | Console on GCP: static IP + self-signed cert on the global HTTPS LB; managed cert later. |
| D18 | v0.3.0 scope: AWS (untested), OAuth polish, SA policy engine, tool blocking, email auth, docs, launch. |
| D19 | v0.3.1: MCP transport is JSON-RPC 2.0 over gRPC (`ramen.v1.Mcp/Call`); HTTP MCP endpoint removed; `ramen-mcp-bridge` (stdio) serves standard clients; full security parity on gRPC; LB routes on `ramen-group`/`ramen-zone` metadata; carried security mediums fixed; logo v2. |
| D21 | v0.4.0: API keys carry an enforced client type — an `agent` key is accepted only by workers over gRPC, a `devops` key only by the console API. |
| D22 | v0.4.0: the autoscale and rebalance stress test runs on a local kind cluster first, then once on a throwaway GKE project. |
| D23 | v0.4.0: independent-client evidence is a real MCP client driven against the bridge, plus an editor configuration the user captures. |
| D31 | v0.5.0: Streamable HTTP at the edge, gRPC inside — every worker serves `POST /mcp` on the gRPC port through the same guards; the bridge is the stdio-only compatibility path. |
| D32 | v0.5.0: the HTTP handler lives in the node on the same port; never a separate edge service. |
| D33 | v0.5.0: sessions are stateless signed ids (HMAC over nonce, expiry and the credential; per-group secret handed to every zone). |
| D34 | v0.5.0: the console is the OAuth 2.1 authorization server — PKCE S256, pre-registered clients, no dynamic registration. |
| D35 | v0.5.1: cloud runs get their permissions from a rule the human adds; the agent never writes its own permission file. |

## Version history
[v0.1.0](v0.1.0.md) local core → [v0.2.0](v0.2.0.md) GCP → [v0.3.0](v0.3.0.md) AWS, auth & policy, docs →
[v0.3.1](v0.3.1.md) gRPC transport → [v0.4.0](v0.4.0.md) console polish, docs, and proof → 0.5.x Streamable HTTP
at the edge, OAuth, the GKE proof, the end-to-end and add-a-tool guides (this page; the changelog has each release).
Tracker: [Versions](../versions.md). A drawing of one call end to end, and what protects every hop, is in
[Transport and what secures each hop](../wiki/transport.md).
