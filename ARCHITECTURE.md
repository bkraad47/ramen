# Ramen architecture (v0.5.5)

Full version on the docs site: https://bkraad47.github.io/ramen/architecture/ (with per-version pages).
Binding interface contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md) — §11 is gRPC, §16 is Streamable HTTP.
Diagram of one call end to end, and what protects every hop:
[Transport and what secures each hop](https://bkraad47.github.io/ramen/wiki/transport/).

Hierarchy: **Group → Environment → Zone → Worker** (D5). One zone first, every layer multi-zone capable (D6).
Transport: **Streamable HTTP at the edge, gRPC inside** (D31; gRPC since D19) — one port, one set of guards.

```
browser / rmn_ key ──▶ console (FastAPI, HTTP) ──▶ Store: Firestore | DynamoDB | Postgres | memory
                            │                       Cloud adapter: local | gcp | aws
                            │                       Secrets backend: store | gcp | aws
                            ▼ deploy: git → bucket, canary → stable; gRPC to pods: Admin/Reload, Admin/Metrics, Health/Check
MCP client ──▶ POST /mcp (Streamable HTTP, bearer key or OAuth token) ──────────────▶ namespace ramen-<group>-<zone>
stdio client ──▶ ramen-mcp-bridge (stdio ⇄ gRPC) ──▶ LB: GKE Gateway | ALB  ── metadata ramen-group, ramen-zone ──▶ (same pods, same guards)
grpcurl / gRPC client ─────────────────────────▶   gRPC health checks                                              worker + worker-canary pods:
                                                                                                                    ramen-node (Rust, tonic :8080) ⇄ ramen_runtime (Python 3.14)
                                                                                                                    bucket gs:// | s3:// | /buckets/<group>  (synced on load)
```

- **proto/ramen/v1/** `mcp.proto` (`ramen.v1.Mcp/Call`: one JSON-RPC 2.0 message as `bytes body`; `Session` stream
  reserved) and `admin.proto` (`Admin/Reload`, `Admin/Metrics`, JSON bytes). Single source: tonic-build for Rust,
  grpcio-tools for the Python stubs vendored in console, runtime-py and tests (`make proto`).
- **console/** FastAPI + Jinja2 + HTMX (D11). Auth (email/password, OAuth/OIDC with PKCE, magic link), RBAC (super
  admin, group admin, viewer), groups/environments/zones, secrets (names only), deploy jobs, rebalance, IP rules,
  logs, audit, `rmn_` API keys, `rmk_` MCP keys, config yaml, backups, SA policy engine, tool blocking, CSRF. The
  console is also the **OAuth 2.1 authorization server** for per-user access (D34; §16.3). `ramen_console.grpcclient`
  dials worker pods directly (`<pod-ip>:8080`, 10 s deadline).
- **node-rs/** Rust MCP node: one port (`RAMEN_NODE_PORT`, h2c/h2 or TLS with `RAMEN_TLS_CERT`/`KEY`) serving both
  `POST /mcp` (Streamable HTTP, §16) and `ramen.v1.Mcp`/`ramen.v1.Admin`/`grpc.health.v1.Health` (gRPC) through the
  same guard functions. Bearer auth on `authorization` (`RAMEN_MCP_KEYS`, union of env + config + deploy file; none
  = deny all; constant-time byte compare folded over every key, though the length check in front of it is not
  constant time) → `401`/`UNAUTHENTICATED`; CIDR allow-lists (`RAMEN_ALLOWED_CIDRS` for `Mcp`, `RAMEN_ADMIN_CIDRS` +
  `x-ramen-admin-key` for `Admin`) matched against the client address — the `RAMEN_TRUST_PROXY_HOPS`-th
  `x-forwarded-for` entry counted from the right (2 on GCP, 1 on AWS, 0 = the peer address) → `403`/`PERMISSION_DENIED`;
  stateless HMAC session ids for HTTP (no affinity, no store); server reflection behind `RAMEN_REFLECTION` (on by
  default, off on deployed workers); `RAMEN_MAX_INFLIGHT` → `429`/`RESOURCE_EXHAUSTED`; 4 MiB messages; blocked
  names (`-32601`); one JSON access-log line per call naming the transport. Spawns the Python runtime on demand
  over stdio JSON-RPC, 1:1, idle-terminated (D3).
- **runtime-py/** Loads `mcp/{tools,resources,prompts}` from the bucket (`gs://`/`s3://` sync on load), validates
  protos, pip-installs `requirements.txt`, executes callables, resolves `{{$group.NAME}}` from
  `RAMEN_SECRET_<GROUP>__<NAME>`, redacts secrets. **`ramen-mcp-bridge`** is its own package/repo (v0.5.7; also
  installed in the worker image): a stdio MCP server that forwards each message to `Mcp/Call` with the key and
  routing metadata — for clients that only speak stdio; standard clients (Claude Desktop, Cursor, the `mcp` SDK)
  connect straight to `POST /mcp` instead.
- **deploy/** compose (local), Helm `ramen` (console + Gateway/ALB Ingress + reduced ClusterRole) and
  `ramen-worker` (one zone namespace: Service `appProtocol: kubernetes.io/h2c`, header-matched HTTPRoute /
  gRPC-annotated Ingress, gRPC health policy, Role/RoleBinding), Terraform GCP (GKE Autopilot, Firestore, GCS, AR,
  static IP, Certificate Manager managed cert for a free `sslip.io` hostname, GSA with custom role + resource-level
  IAM) and AWS (EKS, DynamoDB, S3, Secrets Manager, ECR, IRSA with narrowed WAF/IAM, ALB controller, Fluent Bit —
  **untested**), CloudFormation equivalent.
- **State** Firestore (GCP default) / DynamoDB (AWS default) / Postgres (self-hosted, `RAMEN_STORE=postgres`,
  v0.5.5) behind one repository interface, Fernet-encrypted sensitive fields (D2, D37).
- **Exposure** GKE Gateway (global HTTPS LB, static IP, Google-managed cert via Certificate Manager — a free
  `sslip.io` hostname needs no domain, v0.5.5/D36; self-signed remains an opt-out) or AWS ALB; `/` → console
  (HTTP); `POST /mcp` and gRPC calls carrying `ramen-group` + `ramen-zone` metadata → that zone's workers
  (HTTPRoute header match on GCP, ALB listener rule + `backend-protocol-version: GRPC` on AWS; health checks
  both). No path rewrite, no path alias.
- **Deploy** sync repo (token via git header, never in the URL) → write zone Secret/env (keys, secrets, blocked,
  CIDRs) → canary → `Health/Check` SERVING → `Admin/Reload` → `tools/list` smoke → stable. Failure scales canary
  to 0, stable untouched.
- **Security** roles, two key kinds, Cloud Armor / WAF + node CIDRs, secrets never shown, audit of every mutation,
  per-group+zone service accounts with least privilege; extra permissions only via request → super-admin approval.
  Since 0.3.1: no `projectIamAdmin`, scoped WAF/IAM, namespaced Roles instead of cluster-wide secrets access, PKCE
  + nonce on OIDC, constant-time key compares.
- **Tests** unit suites ≥ 90% per component; `tests/` black-box conformance (gRPC and HTTP statuses, size limit,
  health states, the bridge via the `mcp` stdio client), e2e and cloud suites against `host:port`, `tests/kind/`
  against a real local multi-zone cluster (autoscale, rebalance, both transports); CI on push, release on `v*`
  tags (D9), docs on push to main.
