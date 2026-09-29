# Ramen architecture (v0.3.1)

Full version on the docs site: https://bkraad47.github.io/ramen/architecture/ (with per-version pages).
Binding interface contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md) — §11 is the gRPC transport.
Diagram of one call end to end, and what protects every hop:
[Transport and what secures each hop](https://bkraad47.github.io/ramen/wiki/transport/).

Hierarchy: **Group → Environment → Zone → Worker** (D5). One zone first, every layer multi-zone capable (D6).
Transport: **JSON-RPC 2.0 over gRPC** (D19, supersedes D4).

```
browser / rmn_ key ──▶ console (FastAPI, HTTP) ──▶ Store: Firestore | DynamoDB | memory
                            │                       Cloud adapter: local | gcp | aws
                            │                       Secrets backend: store | gcp | aws
                            ▼ deploy: git → bucket, canary → stable; gRPC to pods: Admin/Reload, Admin/Metrics, Health/Check
MCP client ──▶ ramen-mcp-bridge (stdio ⇄ gRPC) ──▶ LB: GKE Gateway | ALB  ── metadata ramen-group, ramen-zone ──▶ namespace ramen-<group>-<zone>
grpcurl / gRPC client ─────────────────────────▶   gRPC health checks                                              worker + worker-canary pods:
                                                                                                                    ramen-node (Rust, tonic :8080) ⇄ ramen_runtime (Python 3.14)
                                                                                                                    bucket gs:// | s3:// | /buckets/<group>  (synced on load)
```

- **proto/ramen/v1/** `mcp.proto` (`ramen.v1.Mcp/Call`: one JSON-RPC 2.0 message as `bytes body`; `Session` stream
  reserved) and `admin.proto` (`Admin/Reload`, `Admin/Metrics`, JSON bytes). Single source: tonic-build for Rust,
  grpcio-tools for the Python stubs vendored in console, runtime-py and tests (`make proto`).
- **console/** FastAPI + Jinja2 + HTMX (D11). Auth (email/password, OAuth/OIDC with PKCE, magic link), RBAC (super
  admin, group admin, viewer), groups/environments/zones, secrets (names only), deploy jobs, rebalance, IP rules,
  logs, audit, `rmn_` API keys, `rmk_` MCP keys, config yaml, backups, SA policy engine, tool blocking, CSRF.
  `ramen_console.grpcclient` dials worker pods directly (`<pod-ip>:8080`, 10 s deadline).
- **node-rs/** Rust MCP node: one h2c port (`RAMEN_NODE_PORT`, TLS with `RAMEN_TLS_CERT`/`KEY`) serving
  `ramen.v1.Mcp`, `ramen.v1.Admin`, `grpc.health.v1.Health` (`SERVING` after `runtime.load`). Bearer auth on
  metadata `authorization` (`RAMEN_MCP_KEYS`, union of env + config + deploy file; none = deny all; constant-time
  byte compare folded over every key, though the length check in front of it is not constant time) → `UNAUTHENTICATED`; CIDR allow-lists (`RAMEN_ALLOWED_CIDRS` for `Mcp`, `RAMEN_ADMIN_CIDRS` +
  `x-ramen-admin-key` for `Admin`) matched against the client address — the `RAMEN_TRUST_PROXY_HOPS`-th
  `x-forwarded-for` entry counted from the right (2 on GCP, 1 on AWS, 0 = the peer address) → `PERMISSION_DENIED`;
  server reflection behind `RAMEN_REFLECTION` (on by default, off on deployed workers); `RAMEN_MAX_INFLIGHT` → `RESOURCE_EXHAUSTED`; 4 MiB
  messages; blocked names (`-32601`); one JSON access-log line per call with `grpc_code`. Spawns the Python
  runtime on demand over stdio JSON-RPC, 1:1, idle-terminated (D3).
- **runtime-py/** Loads `mcp/{tools,resources,prompts}` from the bucket (`gs://`/`s3://` sync on load), validates
  protos, pip-installs `requirements.txt`, executes callables, resolves `{{$group.NAME}}` from
  `RAMEN_SECRET_<GROUP>__<NAME>`, redacts secrets. Ships **`ramen-mcp-bridge`** (`[grpc]` extra, also in the
  worker image): a stdio MCP server that forwards each message to `Mcp/Call` with the key and routing metadata —
  the way Claude Desktop, Cursor and the `mcp` SDK connect.
- **deploy/** compose (local, worker 8080 h2c), Helm `ramen` (console + Gateway/ALB Ingress + reduced ClusterRole)
  and `ramen-worker` (one zone namespace: Service `appProtocol: kubernetes.io/h2c`, header-matched HTTPRoute /
  gRPC-annotated Ingress, gRPC health policy, Role/RoleBinding), Terraform GCP (GKE Autopilot, Firestore, GCS, AR,
  static IP, GSA with custom role + resource-level IAM) and AWS (EKS, DynamoDB, S3, Secrets Manager, ECR, IRSA with
  narrowed WAF/IAM, ALB controller, Fluent Bit — **untested**), CloudFormation equivalent.
- **State** Firestore (GCP) / DynamoDB (AWS) behind one repository interface, Fernet-encrypted sensitive fields, no
  relational DB (D2). Backups are versioned JSON without secrets.
- **Exposure** GKE Gateway (global HTTPS LB, static IP, self-signed cert until a domain exists — D17) or AWS ALB;
  `/` → console (HTTP); gRPC calls carrying `ramen-group` + `ramen-zone` metadata → that zone's workers (HTTPRoute
  header match on GCP, ALB listener rule + `backend-protocol-version: GRPC` on AWS; gRPC health checks both). No
  path rewrite, no path alias.
- **Deploy** sync repo (token via git header, never in the URL) → write zone Secret/env (keys, secrets, blocked,
  CIDRs) → canary → `Health/Check` SERVING → `Admin/Reload` → `tools/list` smoke → stable. Failure scales canary
  to 0, stable untouched.
- **Security** roles, two key kinds, Cloud Armor / WAF + node CIDRs, secrets never shown, audit of every mutation,
  per-group+zone service accounts with least privilege; extra permissions only via request → super-admin approval.
  0.3.1 narrows the console's own cloud/Kubernetes permissions (no `projectIamAdmin`, scoped WAF/IAM, namespaced
  Roles instead of cluster-wide secrets access), adds PKCE + nonce to OIDC and constant-time key compares.
- **Tests** unit suites ≥ 90 % per component; `tests/` black-box conformance (gRPC statuses, size limit, health
  states, bridge via the `mcp` stdio client), e2e and cloud suites against `host:port`; CI on push, release on
  `v*` tags (D9), docs on push to main.
