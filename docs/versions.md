# Versions

Generated from [`CHANGELOG.md`](https://github.com/bkraad47/ramen/blob/main/CHANGELOG.md) by `scripts/gen_versions.py`; do not edit by hand. Semver, `0.x` is pre-stable; each release is tagged `v<version>` and GitHub Actions attaches downloadable zips.

| Version | Date | Theme | Links |
|---|---|---|---|
| `0.3.0` **(current)** | — | AWS, auth & policy, docs | [release](https://github.com/bkraad47/ramen/releases/tag/v0.3.0) · [architecture](architecture/v0.3.0.md) |
| `0.2.0` | 2026-09-28 | GCP | [release](https://github.com/bkraad47/ramen/releases/tag/v0.2.0) · [architecture](architecture/v0.2.0.md) |
| `0.1.0` | 2026-09-27 | local core | [release](https://github.com/bkraad47/ramen/releases/tag/v0.1.0) · [architecture](architecture/v0.1.0.md) |

## 0.3.0 — AWS, auth & policy, docs

- (in progress) AWS Terraform + CloudFormation and console AWS adapter (untested, no AWS account); OAuth/OIDC login flow, email auth, SA policy engine, tool blocking, CSRF; docs site, wiki, README screenshots, launch material.

## 0.2.0 — GCP

- Console GCP adapter: zone = namespace, canary deploy flow, workers/metrics, Cloud Logging, rebalance via backend capacity, Cloud Armor IP rules, per-group+zone service accounts with Workload Identity, refresh, group destruction.
- Secrets backend `store|gcp` (Secret Manager). Runtime syncs the group bucket from GCS on load.
- Deploy: Terraform (GKE Autopilot, Firestore, Artifact Registry, static IP, groups bucket, console GSA), Helm `ramen` (console, GKE Gateway, RBAC, backend policy) and `ramen-worker` (zone namespace, canary, HTTPRoute `/mcp/<group>/<zone>`), `make push`, GCP how-to.
- Node: separate `RAMEN_ADMIN_CIDRS`; console: IPv6 CIDRs, zone validation on rebalance, drain-aware deploys, thread-safe Google API transport.
- Tests: cloud suites (canary, rebalance, IP rules, logs, service accounts, secrets), cloud smoke and cost-check scripts, manual `cloud-e2e` workflow. Verified on a throwaway GKE project: 89 passed, 0 failed.

## 0.1.0 — local core

- Python runtime sidecar (`ramen_runtime`): loads group repos, validates protos, executes tools/resources/prompts, resolves `{{$group.VAR}}` secrets.
- Rust MCP node (`ramen-node`): JSON-RPC 2.0 over MCP Streamable HTTP, bearer auth, CIDR allowlist, metrics, admin reload, sidecar supervisor.
- FastAPI console: auth, RBAC (super admin / group admin / viewer), groups, environments, zones, secrets (names only), deploy jobs, rebalance, logs, audit, API keys, backups, config; Firestore, DynamoDB and memory stores with Fernet encryption.
- Local stack: docker-compose (Firestore emulator + console + worker), `make demo`; Helm chart; GCP Terraform skeleton (Phase 2).
- Test harness: conformance (node, sidecar, console API), e2e demo flow, coverage report, version check, CI with e2e job, tag-driven releases.
