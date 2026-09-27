# Changelog
All notable changes. Versions follow semver; 0.x is pre-stable.

## [Unreleased]
## [0.2.0] — GCP
- (in progress) GCP cloud adapter, Secret Manager secrets, GCS bucket sync, canary deploys, rebalance, Cloud Logging, Terraform apply + Helm on GKE Autopilot.
## [0.1.0] — local core
- Python runtime sidecar (`ramen_runtime`): loads group repos, validates protos, executes tools/resources/prompts, resolves `{{$group.VAR}}` secrets.
- Rust MCP node (`ramen-node`): JSON-RPC 2.0 over MCP Streamable HTTP, bearer auth, CIDR allowlist, metrics, admin reload, sidecar supervisor.
- FastAPI console: auth, RBAC (super admin / group admin / viewer), groups, environments, zones, secrets (names only), deploy jobs, rebalance, logs, audit, API keys, backups, config; Firestore, DynamoDB and memory stores with Fernet encryption.
- Local stack: docker-compose (Firestore emulator + console + worker), `make demo`; Helm chart; GCP Terraform skeleton (Phase 2).
- Test harness: conformance (node, sidecar, console API), e2e demo flow, coverage report, version check, CI with e2e job, tag-driven releases.
