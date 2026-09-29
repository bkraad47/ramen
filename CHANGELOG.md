# Changelog
All notable changes. Versions follow semver; 0.x is pre-stable.

## [Unreleased]
## [0.4.0] — console, docs, and evidence
**Breaking / behaviour**
- API keys carry a client type and it is enforced: an `agent` key (`rmk_`) is accepted only by workers, a `devops` key (`rmn_`) only by the console API, each refused by the other side. Keys made before 0.4.0 keep working, typed by their prefix.
- `RAMEN_TRUST_PROXY` is replaced by `RAMEN_TRUST_PROXY_HOPS=N`, which reads the Nth `x-forwarded-for` entry **counted from the right**. The old spelling still works and means one hop. Deployed values: GCP 2, AWS 1 — **measured on a live GKE Gateway**, not assumed. Any failure falls back to the peer address, so a wrong count denies rather than admits.
- Server reflection is gated by `RAMEN_REFLECTION` and is **off on deployed workers**: `grpcurl` against a deployed worker now needs `-import-path proto -proto ramen/v1/mcp.proto`.
- A super admin changing the authentication configuration signs out every other session, their own other clients included.

**Security**
- The worker address allowlist could be bypassed: proxy trust shipped enabled and the node read the leftmost `x-forwarded-for` entry, which a caller controls. Found by an independent review of the documentation's own claims.
- Setting IP rules wrote the cloud edge policy before the worker allowlist on both clouds, so a cloud failure left the zone unlocked while reporting an error. The worker is now configured first; the edge policy is best effort.
- A `canary:false` deploy left the previous canary pod serving the zone with the old configuration, so a disabled tool still ran, a revoked key still worked and a tightened allowlist did not apply. Stale canaries are now removed before the stable roll.
- Sessions are revoked on password, role, group and authentication-configuration changes, and on delete.
- Passwords and generated keys are at least 12 characters across four character classes.

**Console**
- Per-zone enable and disable for tools, resources and prompts, on top of the environment-wide list.
- Two-pane logs with the consumer, timestamp, method and outcome per entry, and a worker filter.
- One equal-width Actions group per zone; identity and role in the sidebar with the role named and coloured; health described by load rather than by colour; sentence case throughout; group pickers instead of typed lists.
- `GET` for a single zone and a single environment. `refresh` survives a namespace that is terminating or unreadable.

**Docs and repository**
- Dark-only site, aligned badges, no edit button, no heading permalink symbols, `llms.txt` served but unlinked.
- A hand-drawn architecture diagram that names its own plaintext hops, and a transport page whose every claim was checked against the code by a reviewer that did not write it.
- Operator facts stated plainly: which hops are plaintext, what key rotation actually costs, what the deploy file can and cannot change, and that the AWS path has never been applied.
- Repository description, homepage and topics set.

**Verification**
- `deploy/kind/` brings up a local two-zone cluster: `make kind-up`, `kind-test`, `kind-down`, and a CI job off the pull-request path.
- Proven live: two zones serving independently, autoscaling one to two replicas under real load with the neighbouring zone untouched, rebalance, per-zone tool isolation, a 48-case role and group security matrix, both key types, and Claude Code driven as a real MCP client.
- Proven on GKE: the forwarded-for hop count measured position by position, spoofed headers refused, reflection unreachable through the load balancer, and none of the eight defects from the 0.3.2 run recurring.

## [0.3.2] — verified on GKE
- gRPC header routing verified on a live GKE Gateway over cleartext HTTP/2 (h2c); no TLS fallback needed. Live harness: 126 passed, 0 failed.
- Fixes from the live run: zone identity (GSA + Workload Identity + baseline grants) is ensured when a zone is attached, not only by an explicit service-account call; IAM bindings on new service accounts wait for propagation; all worker services (Mcp, Health, reflection) are routed through the Gateway; the node retries its initial load instead of waiting for an admin reload; the bridge pins the server certificate in insecure-TLS mode.
- API: `GET /api/v1/zones/{zone}` and `GET /api/v1/groups/{group}/environments/{env}`. `make env` warns when `.env` is older than the example. Terraform lock files are committed.
## [0.3.1] — gRPC transport
- **Breaking for HTTP MCP clients** (D19, contract §11): the worker's HTTP surface (`POST /mcp`, `/healthz`, `/readyz`, `/metrics`, `/admin/reload`, `RAMEN_MCP_PATH_PREFIX`) is removed. Workers speak JSON-RPC 2.0 over gRPC: `ramen.v1.Mcp/Call` carries one JSON-RPC message as `bytes body`, `ramen.v1.Admin/{Reload,Metrics}` replace the admin routes, `grpc.health.v1.Health` reports `SERVING` once code is loaded; one h2c port `RAMEN_NODE_PORT` (8080), optional node TLS via `RAMEN_TLS_CERT`/`RAMEN_TLS_KEY`. Standard MCP clients (Claude Desktop, Cursor, the mcp SDK) connect through **`ramen-mcp-bridge`** (stdio; `ramen-runtime[grpc]` console script, also in the worker image): `--target <host:port> --key <rmk_> --group <g> --zone <z> [--tls|--insecure] [--ca <pem>]`. Migration: docs *Migrate 0.3.0 → 0.3.1*.
- Security parity on gRPC: metadata `authorization: Bearer <rmk_key>` with constant-time compare (`UNAUTHENTICATED`; empty key set = deny all), `RAMEN_ALLOWED_CIDRS` / `x-forwarded-for` with `RAMEN_TRUST_PROXY=1` (`PERMISSION_DENIED`), `x-ramen-admin-key` + `RAMEN_ADMIN_CIDRS` on `Admin/*`, blocked names hidden from `*/list` and answered `-32601`, 4 MiB message limit, `RAMEN_MAX_INFLIGHT` → `RESOURCE_EXHAUSTED`, unauthenticated health, access log gains `grpc_code`.
- Edge routing by metadata: clients and the bridge send `ramen-group` / `ramen-zone`; GKE Gateway HTTPRoutes match on those headers (worker Service `appProtocol: kubernetes.io/h2c`, `HealthCheckPolicy` type `GRPC`, no path rewrite); AWS ALB target groups `backend-protocol-version: GRPC` with header listener rules and gRPC health code 0; Cloud Armor / WAF unchanged.
- Console: `ramen_console.grpcclient` (grpcio) replaces every HTTP call to workers (`Admin/Reload` + `tools/list` smoke, `Admin/Metrics` for load, `Health/Check` for readiness); `RAMEN_GCP_POD_PROXY` / `RAMEN_AWS_POD_PROXY` removed. Local stack, `make demo`, `mcp_call.py` and `mcp-client-config.example.json` use gRPC / the bridge; harness `ramen_tests.mcp_client` speaks gRPC and covers the bridge end to end through the mcp stdio client; `scripts/cloud_smoke.sh` uses `grpcurl`.
- Protos: `proto/ramen/v1/{mcp,admin}.proto` are the single source; Rust via tonic-build, Python stubs (`ramen_proto`) vendored in console, runtime-py and tests, regenerated with `make proto`.
- Carried security mediums fixed: GCP console GSA drops `resourcemanager.projectIamAdmin` for `iam.serviceAccountAdmin` + a custom role limited to `setIamPolicy` on `ramen-*` service accounts, with bucket-/secret-level bindings; AWS console role narrows `wafv2:*` to the `ramen` web ACL / IP sets and `iam:PutRolePolicy` to `/ramen/` roles; console ClusterRole loses cluster-wide `secrets`/`serviceaccounts` in favour of a namespaced Role + RoleBinding per attached zone; `sync_repo` passes the GitHub token via `http.extraheader`/`GIT_ASKPASS` and strips credentials from `.git/config`; OIDC uses PKCE (S256) + `nonce`; node key compares are constant-time.
- Logo v2 (same coral/off-white palette) in the console, docs site (`docs/img/logo.png`, `logo-mark.png`, `favicon.png`), README and launch drafts. Docs: architecture v0.3.1, transport sections in how-it-works / protos / concepts, bridge + grpcurl quickstart, GCP/AWS header routing and gRPC health, security parity table, migration note; `mkdocs.yml` `version_current: 0.3.1`.
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
