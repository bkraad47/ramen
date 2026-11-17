---
name: test
description: Run Ramen's test suites — Rust node, Python runtime, Python console, the cross-component harness, the kind cluster proofs — with the 90% coverage gate and CI's lint, docs and secrets checks, after reading the state snapshot. Use when asked to test, verify, lint, check coverage, or confirm a change to the Ramen codebase did not break anything.
license: BSD-3-Clause
compatibility: Needs a checkout of github.com/bkraad47/ramen, uv (Python 3.14 for console and tests, 3.12 for the runtime), the Rust toolchain from rust-toolchain.toml with protoc; docker, kind and kubectl for the kind suite; helm, terraform, actionlint and shellcheck for the full lint.
metadata:
  version: "1.0.0"
  author: bkraad47
---

# Test Ramen

Any coding agent or a human can run this with a shell and the languages' own toolchains. CI
(`.github/workflows/ci.yml`) is the reference: when this page and CI disagree, CI wins.

## Steps
1. Read `facts/STATE.md`, then the section of `docs/CONTRACTS.md` for the area you touch (the binding spec).
2. Unit suites, each with `--cov-fail-under=90` (below is a failure, not a warning): `make test`
   (`test-runtime`, `test-node`, `test-console`). The runtime runs on Python 3.12 (`UV_PYTHON=3.12`), the console and
   `tests/` on 3.14 (`UV_PYTHON=3.14`); uv fetches both.
3. Lint exactly as CI, checking each exit code (never pipe it away):
   ```sh
   for d in console tests; do (cd $d && uv sync --all-extras && uv run ruff check . && uv run ruff format --check .) || exit 1; done
   (cd runtime-py && UV_PYTHON=3.12 uv sync --all-extras && uv run ruff check . && uv run ruff format --check .)
   (cd console && uv run ruff check ../scripts ../deploy/local && uv run ruff format --check ../scripts ../deploy/local)
   python3 scripts/check_secrets.py && python3 scripts/check_versions.py && python3 scripts/check_docs_links.py
   uv run --project docs --group docs mkdocs build --strict        # when docs/ or mkdocs.yml changed
   ```
   `make lint` adds `cargo fmt --check`, `cargo clippy -- -D warnings`, `helm lint` (every chart, every provider),
   `terraform fmt -check`, `actionlint` and `shellcheck` when installed. Changed `skills/`: validate each skill with
   the pinned reference validator, `uvx --from skills-ref==0.1.1 agentskills validate skills/<name>`.
4. Changed `tests/` or cross-component behavior: `cd tests && uv sync && uv run pytest -q -rs`. Suites skip, never
   fail, when their environment is missing (a built `ramen-node`, a live console, a kind cluster); a skip must say why.
   `tests/README.md` lists what each suite needs.
5. Changed multi-zone behavior (autoscale, rebalance, sleep, builds, routing): `make kind-up && make kind-test` — the
   only suite with real Kubernetes scaling. Restore what you scaled before a later suite runs, then `make kind-down`.
6. Report in a short paragraph, not a new file: what ran, pass/fail/skip counts, coverage per component, what was
   skipped and why. If nothing changed since the last run, say so instead of re-running everything.

## Validate
Hand verbatim to a second agent or a fresh pass.
- V1 `make test` exits 0; no component below 90% coverage.
- V2 Every lint command of step 3 exits 0.
- V3 `tests/` touched: `cd tests && uv run pytest -q -rs` — no failure, no unexplained skip.
- V4 `deploy/helm/` or `deploy/terraform/` touched: `helm lint` / `helm template` for every provider, `terraform
  validate` in each changed module.
- V5 The commit or PR message states the coverage numbers and every skip with its reason.

## Boundaries
- Never lower the 90% gate or add a blanket coverage exclusion; a narrow, commented exclusion only for a
  real-cloud-only path already covered by a fake-backed test (the pattern in `console/src/ramen_console/storage/`).
- Never comment out or delete a test to unblock a run; mark it skip/xfail with a reason and note it in
  `facts/STATE.md` or the PR description.
- A kind run changes real cluster state: scale back what you changed before handing the cluster on.
