# Ramen architecture (v0.3.0)

Full version on the docs site: https://bkraad47.github.io/ramen/architecture/ (with per-version pages).
Binding interface contracts: [docs/CONTRACTS.md](docs/CONTRACTS.md).

Hierarchy: **Group → Environment → Zone → Worker** (D5). One zone first, every layer multi-zone capable (D6).

```
browser / rmn_ key ──▶ console (FastAPI) ──▶ Store: Firestore | DynamoDB | memory
                            │                Cloud adapter: local | gcp | aws
                            │                Secrets backend: store | gcp | aws
                            ▼ deploy: git → bucket, canary → stable, /admin/reload
MCP client ──▶ LB /mcp/<group>/<zone> ──▶ namespace ramen-<group>-<zone>
                                            worker + worker-canary pods: ramen-node (Rust) ⇄ ramen_runtime (Python 3.14)
                                            bucket gs:// | s3:// | /buckets/<group>  (synced on load)
```

- **console/** FastAPI + Jinja2 + HTMX (D11). Auth (email/password, OAuth/OIDC, magic link), RBAC (super admin,
  group admin, viewer), groups/environments/zones, secrets (names only), deploy jobs, rebalance, IP rules, logs,
  audit, `rmn_` API keys, `rmk_` MCP keys, config yaml, backups, SA policy engine, tool blocking, CSRF.
- **node-rs/** Rust MCP node: JSON-RPC 2.0 over MCP Streamable HTTP (`2025-06-18`, JSON responses), bearer auth
  (`RAMEN_MCP_KEYS`, union of env + config + deploy file; none = deny all), CIDR allow-lists (`/mcp` and `/admin`
  separately), `RAMEN_MAX_INFLIGHT`, `/metrics`, `/admin/reload`, structured logs, blocked-tool list. Spawns the
  Python runtime on demand over stdio JSON-RPC, 1:1, idle-terminated (D3).
- **runtime-py/** Loads `mcp/{tools,resources,prompts}` from the bucket (`gs://`/`s3://` sync on load), validates
  protos, pip-installs `requirements.txt`, executes callables, resolves `{{$group.NAME}}` from
  `RAMEN_SECRET_<GROUP>__<NAME>`, redacts secrets.
- **deploy/** compose (local), Helm `ramen` (console + Gateway/ALB Ingress + RBAC) and `ramen-worker` (one zone
  namespace), Terraform GCP (GKE Autopilot, Firestore, GCS, AR, static IP, GSA + WI) and AWS (EKS, DynamoDB, S3,
  Secrets Manager, ECR, IRSA, ALB controller, Fluent Bit — **untested**), CloudFormation equivalent.
- **State** Firestore (GCP) / DynamoDB (AWS) behind one repository interface, Fernet-encrypted sensitive fields, no
  relational DB (D2). Backups are versioned JSON without secrets.
- **Transport** MCP Streamable HTTP; standard clients connect directly (D4).
- **Exposure** GKE Gateway (global HTTPS LB, static IP, self-signed cert until a domain exists — D17) or AWS ALB;
  `/` → console, `/mcp/<group>/<zone>` → that zone's workers (prefix rewrite on GCP, path alias on AWS).
- **Deploy** sync repo → write zone Secret/env (keys, secrets, blocked, CIDRs) → canary → ready → reload →
  `tools/list` smoke → stable. Failure scales canary to 0, stable untouched.
- **Security** roles, two key kinds, Cloud Armor / WAF + node CIDRs, secrets never shown, audit of every mutation,
  per-group+zone service accounts with least privilege; extra permissions only via request → super-admin approval.
- **Tests** unit suites ≥ 90 % per component; `tests/` black-box conformance/e2e/cloud suites against URLs; CI on
  push, release on `v*` tags (D9), docs on push to main.
