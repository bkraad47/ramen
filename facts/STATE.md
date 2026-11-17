# Ramen — current state (public snapshot, 1.0.0)

AI-agnostic scratchpad for anyone (human or agent) working on this codebase, regardless of which coding
assistant they use (v0.5.5 I17). `docs/CONTRACTS.md` is the binding spec — the source of truth for **what**
each component must do; this file is the source of truth for **what already exists**, kept short and
overwritten as the code changes. Read `docs/CONTRACTS.md` first, this file second.

## Components
| Path | What | Language |
|---|---|---|
| `node-rs/` | `ramen-node`: Streamable HTTP (`POST /mcp`) and gRPC on one TLS port, one set of guards; MCP 2026-07-28 and 2025-06-18 per request; activator mode for sleeping zones (CONTRACTS §11, §16, §23, §24.1) | Rust |
| `runtime-py/` | `ramen_runtime`: loads a group's `mcp/` packages, runs tool/resource/prompt calls, guardrails, pip-installs `requirements.txt` | Python 3.12 |
| `console/` | FastAPI + Jinja2 + HTMX UI + `/api/v1`: groups, environments, zones, secrets, deploys, builds, evals, scans, backups, audit, OAuth server, install CA | Python 3.14 |
| `tests/` | Cross-component conformance/e2e/cloud/kind/load suites — see `tests/README.md` | Python 3.14 |
| `deploy/` | `terraform/{gcp,aws,azure}` + `-satellite`, `helm/{ramen,ramen-worker,ramen-satellite}`, `local/` (compose), `kind/`, `cloudformation/` | Terraform, Helm, YAML |
| `skills/` | Agent Skills (agentskills.io format) for operating Ramen (`deploy-gcp`, `deploy-aws`, `deploy-azure`, `rotate-keys`, ...) and for contributing to the code (`iterate/`, `test/`) | Markdown |

## Transport and storage
- Transport: **Streamable HTTP at the edge, gRPC inside** (CONTRACTS §16); every hop TLS with the install's own
  CA (D86, §39), each zone's leaf naming the zone (D92). Stdio-only clients use `ramen-mcp-bridge` (its own repo).
- State store: `RAMEN_STORE` = `firestore` (GCP), `dynamodb` (AWS), `azuretables` (Azure), `postgres`
  (self-hosted), or `memory` (tests/dev only). See `docs/wiki/configuration.md` "The state store".
- Secrets backend: `RAMEN_SECRETS_BACKEND` = `gcp` (Secret Manager), `aws` (Secrets Manager), `azure` (Key Vault),
  or the store itself (Fernet-encrypted) as a fallback.

## Verified vs not (kept current at `docs/wiki/transport.md#what-is-verified-and-what-is-not`)
Each release's cloud pass runs on GKE, EKS and AKS and is torn down afterwards; the record and what was not run
are on that page. Local: the compose stack and the kind suite run in CI.

## Build and test
`make test` runs everything (`test-runtime` + `test-node` + `test-console`); `make lint` runs every linter
(ruff, clippy, helm lint, terraform fmt, actionlint, shellcheck). Coverage gate: 90%, enforced per component
(`--cov-fail-under=90`). `make kind-up && make kind-test` runs the multi-zone/autoscale/sleep/build proofs against a
real local cluster. See `skills/test/SKILL.md` for the full loop.

## Conventions worth knowing before changing code
- TDD: the failing test goes in first. A PR that adds behavior without a test that would have caught its
  absence is incomplete, not just unpolished.
- Cheap first: smallest containers, one zone by default, but every layer must stay multi-zone capable — no
  code path may assume a single zone exists.
- `docs/CONTRACTS.md` is binding and append-only: a new numbered section per shipping version, never rewrite
  a past one. Historical text elsewhere (README callouts, changelog-style doc lines) stays historically
  accurate; only genuinely current/forward-looking claims get updated when something changes.
