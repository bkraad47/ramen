# Changelog
All notable changes. Versions follow semver; 0.x is pre-stable.

## [Unreleased]
## [0.3.1] — gRPC transport
- (in progress) **Breaking for HTTP MCP clients**: the worker speaks JSON-RPC 2.0 over gRPC (`ramen.v1.Mcp`); use `ramen-mcp-bridge` (stdio) from Claude Desktop, Cursor or the mcp SDK. Security parity on gRPC, carried security mediums fixed, logo v2.
## [0.3.0] — AWS, auth & policy, docs
- AWS path (**untested on a real account**, D18): Terraform `deploy/terraform/aws` (EKS, DynamoDB, S3, ECR, IRSA roles, AWS Load Balancer Controller + Fluent Bit via Helm, self-signed cert in ACM), CloudFormation `deploy/cloudformation/ramen.yaml`, Helm `provider: aws` (ALB IngressGroup, weighted stable/canary target groups, IRSA), console `aws` cloud adapter (S3 repo sync, CloudWatch Logs Insights, ALB weights, WAFv2 IP rules, IAM role per group+zone, `apply_sa_permissions`) and `aws` secrets backend (Secrets Manager `ramen/<group>/<env|all>/<zone|all>/<NAME>`); runtime syncs `s3://` buckets; node `RAMEN_MCP_PATH_PREFIX` serves `/mcp/<group>/<zone>` behind an ALB.
- Auth: OAuth/OIDC providers (`RAMEN_OAUTH_<NAME>_*`, role mapping by claim), email via SMTP or the `file://` dev backend (invite on user create, password reset, magic-link login), super-admin toggles `GET|PUT /api/v1/config/auth` with break-glass `RAMEN_ADMIN_FORCE_PASSWORD`, CSRF double-submit token for cookie sessions (API keys exempt).
- SA policy engine: permission catalogue `GET /api/v1/policy/permissions`, group-admin requests `POST /api/v1/requests`, super-admin approval → `Cloud.apply_sa_permissions` (gcp IAM roles with prefix conditions, aws inline policy, local recorded); denied-by-rule → 409, audited.
- Tool blocking per environment: `PUT …/environments/{env}/blocked` → `RAMEN_BLOCKED` on deploy; the node hides blocked names from `*/list` and answers `-32601`; block/unblock toggle on the group page.
- Docs: MkDocs Material site on GitHub Pages (architecture per version, how-tos for local/GCP/AWS/security/secrets/DevOps API, wiki, generated version tracker, `llms.txt` + JSON-LD), README with screenshots and a 5-command quickstart, `skills/` cloud-ops agent skills, launch drafts.
- Tooling: `ruff` lint/format config shared via `ruff.toml`, `make lint`, CI lint job; Dockerfiles pin `uv` and carry OCI labels.
- Hardening after the security audit: no constant signing secret, security headers, failure-only login rate limit, token redaction in logs, same-origin login redirect, strict names in log queries, OIDC email verification, CSV formula escaping, worker NetworkPolicy + container securityContext, pinned CI actions. Standards pass: ruff across the repo, `make lint`, CI lint job.

## [0.2.0] — GCP
- Console GCP adapter: zone = namespace, canary deploy flow, workers/metrics, Cloud Logging, rebalance via backend capacity, Cloud Armor IP rules, per-group+zone service accounts with Workload Identity, refresh, group destruction.
- Secrets backend `store|gcp` (Secret Manager). Runtime syncs the group bucket from GCS on load.
- Deploy: Terraform (GKE Autopilot, Firestore, Artifact Registry, static IP, groups bucket, console GSA), Helm `ramen` (console, GKE Gateway, RBAC, backend policy) and `ramen-worker` (zone namespace, canary, HTTPRoute `/mcp/<group>/<zone>`), `make push`, GCP how-to.
- Node: separate `RAMEN_ADMIN_CIDRS`; console: IPv6 CIDRs, zone validation on rebalance, drain-aware deploys, thread-safe Google API transport.
- Tests: cloud suites (canary, rebalance, IP rules, logs, service accounts, secrets), cloud smoke and cost-check scripts, manual `cloud-e2e` workflow. Verified on a throwaway GKE project: 89 passed, 0 failed.
## [0.1.0] — local core
- Python runtime sidecar (`ramen_runtime`): loads group repos, validates protos, executes tools/resources/prompts, resolves `{{$group.VAR}}` secrets.
- Rust MCP node (`ramen-node`): JSON-RPC 2.0 over MCP Streamable HTTP, bearer auth, CIDR allowlist, metrics, admin reload, sidecar supervisor.
- FastAPI console: auth, RBAC (super admin / group admin / viewer), groups, environments, zones, secrets (names only), deploy jobs, rebalance, logs, audit, API keys, backups, config; Firestore, DynamoDB and memory stores with Fernet encryption.
- Local stack: docker-compose (Firestore emulator + console + worker), `make demo`; Helm chart; GCP Terraform skeleton (Phase 2).
- Test harness: conformance (node, sidecar, console API), e2e demo flow, coverage report, version check, CI with e2e job, tag-driven releases.
