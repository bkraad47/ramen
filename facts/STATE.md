# Ramen — current state (public snapshot)

AI-agnostic scratchpad for anyone (human or agent) working on this codebase, regardless of which coding
assistant they use (v0.5.5 I17). `docs/CONTRACTS.md` is the binding spec — the source of truth for **what**
each component must do; this file is the source of truth for **what already exists**, kept short and
overwritten as the code changes. Read `docs/CONTRACTS.md` first, this file second.

## Components
| Path | What | Language |
|---|---|---|
| `node-rs/` | `ramen-node`: gRPC + Streamable HTTP on one port, one set of guards (CONTRACTS §11, §16) | Rust |
| `runtime-py/` | `ramen_runtime`: loads a group's `mcp/` packages, runs tool/resource/prompt calls, pip-installs `requirements.txt` | Python 3.14 |
| `console/` | FastAPI + Jinja2 + HTMX manager UI + `/api/v1`: groups, environments, zones, secrets, deploys, backups, audit | Python 3.14 |
| `tests/` | Cross-component conformance/e2e/cloud/kind suites — see `tests/README.md` | Python 3.14 |
| `deploy/` | `terraform/{gcp,aws}`, `helm/{ramen,ramen-worker}`, `local/` (docker compose), `kind/`, `cloudformation/` | Terraform, Helm, YAML |
| `skills/` | This directory: agentskills.io-format skills for operating (`deploy-gcp`, `rotate-keys`, ...) and for contributing to the code (`iterate/`, `test/`) |

## Transport and storage
- Transport: **Streamable HTTP at the edge, gRPC inside** (CONTRACTS §16) — `POST /mcp` and `ramen.v1.Mcp/Call`
  on the same port, same guards. Standard MCP clients use HTTP directly; stdio-only clients use the bridge.
- State store: pluggable — `RAMEN_STORE` = `firestore` (GCP default), `dynamodb` (AWS default), `postgres`
  (self-hosted, v0.5.5), or `memory` (tests/dev only). See `docs/how-tos/storage-backend.md`.
- Secrets backend: `RAMEN_SECRETS_BACKEND` = `gcp` (Secret Manager) or `aws` (Secrets Manager), or the store
  itself (Fernet-encrypted) as a fallback.

## Verified vs not (kept current at `docs/wiki/transport.md#what-is-verified-and-what-is-not`)
GCP: gRPC and Streamable HTTP + OAuth both verified live on GKE (separate runs); Google-managed TLS via
Certificate Manager is the default since v0.5.5 (a free `sslip.io` hostname needs no domain purchase).
AWS: built and unit-tested only — **never applied to a real account** (no AWS account available).
Local: docker compose stack proven in CI on every push.

## Build and test
`make test` runs everything (`test-runtime` + `test-node` + `test-console`); `make lint` runs every linter
(ruff, clippy, helm lint, terraform fmt, actionlint, shellcheck). Coverage gate: 90%, enforced per component
(`--cov-fail-under=90`). `cd tests && make kind-test` (needs `deploy/kind/up.sh` first) runs the multi-zone/
autoscale/rebalance proofs against a real local cluster. See `skills/test/SKILL.md` for the full loop.

## Conventions worth knowing before changing code
- TDD: the failing test goes in first. A PR that adds behavior without a test that would have caught its
  absence is incomplete, not just unpolished.
- Cheap first: smallest containers, one zone by default, but every layer must stay multi-zone capable — no
  code path may assume a single zone exists.
- `docs/CONTRACTS.md` is binding and append-only: a new numbered section per shipping version, never rewrite
  a past one. Historical text elsewhere (README callouts, changelog-style doc lines) stays historically
  accurate; only genuinely current/forward-looking claims get updated when something changes.
