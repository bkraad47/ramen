# Architecture (current: v0.3.0)

Hierarchy: **Group → Environment → Zone → Worker** (decision D5).

```
                         ┌──────────────────────────── console (FastAPI, 1 node) ────────────────────────────┐
  browser / rmn_ key ──▶ │ auth (password, OAuth/OIDC, magic link) · RBAC · groups/envs/zones · secrets     │
                         │ deploy jobs · rebalance · IP rules · logs · audit · backups · config · policy      │
                         └──────┬──────────────────────┬──────────────────────────────┬─────────────────────┘
                                │ Store                │ Cloud adapter                │ Secrets backend
                                ▼                      ▼                              ▼
                     Firestore | DynamoDB | memory   local | gcp | aws           store | gcp | aws
                                                       │ namespaces, Deployments, Secrets, routes,
                                                       │ NEG/target groups, Cloud Armor / WAF, IAM
                                                       ▼
   MCP client ──▶ LB (GKE Gateway | ALB) ──▶ /mcp/<group>/<zone> ──▶ zone namespace ramen-<group>-<zone>
                                                                       ├─ worker (stable)   ─┐ ramen-node (Rust)
                                                                       └─ worker-canary     ─┘  └─ ramen_runtime (Python 3.14)
                                                                                                    ▲ sync on load
                                                                                    bucket gs:// | s3:// | /buckets/<group>
```

## Components
| Dir | What | Tech | Contract |
|---|---|---|---|
| `console/` | Manager UI + `/api/v1` | FastAPI, Jinja2, HTMX (no JS build), argon2, itsdangerous, Fernet | [§4, §4a](../CONTRACTS.md) |
| `node-rs/` | MCP server node: `POST /mcp`, bearer auth, CIDR allow-lists, metrics, `/admin/reload`, sidecar supervisor | Rust 1.98, axum, tokio | [§3](../CONTRACTS.md) |
| `runtime-py/` | Loads `mcp/` packages from the bucket, validates protos, pip-installs requirements, executes calls, resolves secrets | Python 3.14, jsonschema | [§1, §2](../CONTRACTS.md) |
| `deploy/local` | docker compose: Firestore emulator + console + one worker | | [§5](../CONTRACTS.md) |
| `deploy/terraform/gcp`, `deploy/helm/*` | GKE Autopilot, Firestore, GCS, Artifact Registry, static IP, GSA + Workload Identity, GKE Gateway | Terraform, Helm 4 | [§7](../CONTRACTS.md) |
| `deploy/terraform/aws`, `deploy/cloudformation` | EKS, DynamoDB, S3, Secrets Manager, ECR, IRSA, ALB controller, Fluent Bit (**untested**) | Terraform, CloudFormation | [§8](../CONTRACTS.md) |
| `tests/` | Black-box conformance + e2e + cloud suites that run against URLs | pytest, official `mcp` client | |
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
- **MCP call**: client → LB `/mcp/<group>/<zone>` → node checks CIDR + `rmk_` key → runtime (spawned on demand) → tool → JSON-RPC result. One log line per call (`ts, ip, group, method, name, status, ms, key_id`); `RAMEN_VERBOSE=1` logs bodies.
- **Deploy**: console job → clone repo → upload bucket → write zone Secret/env file (`RAMEN_MCP_KEYS`, `RAMEN_SECRET_*`, `RAMEN_BLOCKED`, `RAMEN_ALLOWED_CIDRS`) → canary restart → ready → `/admin/reload` → `tools/list` smoke → stable restart. See [Canary](../wiki/canary.md).
- **Rebalance / IP rules**: console → cloud API (backend capacity, Cloud Armor / WAF) + node env → worker roll. See [Rebalance](../wiki/rebalance.md).

## Decisions
| ID | Decision |
|---|---|
| D2 | State DB: Firestore on GCP, DynamoDB on AWS, one repository interface. No Postgres. |
| D3 | Worker pod = Rust node + Python runtime sidecar, 1:1, spawned on demand, idle-terminated. |
| D4 | Transport: JSON-RPC 2.0 over HTTP (MCP Streamable HTTP). Standard clients connect directly. |
| D5 | Hierarchy Group → Environment → Zone → Worker. |
| D6 | One zone first; every layer multi-zone capable. |
| D9 | Semver from 0.1.0; tags `v*` build releases with zips. |
| D11 | Console: FastAPI + Jinja2 + HTMX, server-rendered. |
| D14 | Bucket dir = group repo root; no MCP keys = deny all; node config precedence file < env < deploy file. |
| D17 | Console on GCP: static IP + self-signed cert on the global HTTPS LB; managed cert later. |
| D18 | v0.3.0 scope: AWS (untested), OAuth polish, SA policy engine, tool blocking, email auth, docs, launch. |

## Version history
[v0.1.0](v0.1.0.md) local core → [v0.2.0](v0.2.0.md) GCP → [v0.3.0](v0.3.0.md) AWS, auth & policy, docs.
Tracker: [Versions](../versions.md).
