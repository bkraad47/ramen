---
name: test
description: Run Ramen's test suite (Rust node, Python runtime, Python console, cross-component harness) with the 90% coverage gate, and read the state snapshot first. Use when asked to test, verify, check coverage, or confirm a change didn't break anything.
---

# Test Ramen

Any coding agent — Claude Code, Cursor, Copilot, Gemini CLI, or a human — can run this. No agent-specific
tooling required: a shell and the languages' own toolchains (`uv`, `cargo`) are enough.

## Inputs
- A working tree of this repo, checked out (submodule or standalone clone).
- `uv` and `cargo` installed (`rust-toolchain.toml` pins the Rust version; `uv` resolves Python 3.14 itself).
- Optional: `docker` (for `make demo` / the compose stack), `kubectl` + `kind` (for `tests/kind/`), a real
  Postgres if testing `storage/postgres.py`'s live-integration case.

## Steps
1. Read `facts/STATE.md`, then `docs/CONTRACTS.md` for the area you're touching — the binding spec for what
   correct behavior looks like.
2. Run `make test` (equivalently `make test-runtime test-node test-console` one at a time). Each component
   enforces `--cov-fail-under=90`; a run below that threshold is a failure, not a warning.
3. Run `make lint` — ruff (Python, shared `ruff.toml`), `cargo fmt --check` + `cargo clippy -- -D warnings`
   (Rust), `helm lint` (both charts, both providers), `terraform fmt -check` (both clouds), `actionlint` and
   `shellcheck` when installed.
4. If your change touches `tests/` (cross-component conformance/e2e), run `cd tests && uv sync -q && uv run
   pytest -q`. Suites that need infrastructure they can't find (a built `node-rs` binary, a live console URL,
   a kind cluster) skip with a stated reason — that's expected locally; CI provides what each suite needs.
5. If your change touches multi-zone behavior (autoscale, rebalance, worker routing), bring up a local kind
   cluster (`deploy/kind/up.sh`) and run `deploy/kind/test.sh` — this is the only suite that exercises real
   Kubernetes HPA/Deployment scaling, not a mock.
6. Write the report as a short paragraph, not a new file: what ran, pass/fail counts, coverage per
   component, anything skipped and why. If nothing changed since the last run, say so instead of re-running
   everything from scratch.

## Validate
(hand this section, verbatim, to a second agent or a fresh review pass — it should not need the context above)
- V1 `make test` exits 0; no component's coverage output falls below 90%.
- V2 `make lint` exits 0.
- V3 If `tests/` was touched: `cd tests && uv run pytest -q` — no unexpected failures (skips with a reason
  are fine; a failure or an unexplained skip is not).
- V4 If the change affects deploy config (`deploy/helm/`, `deploy/terraform/`): `helm lint`/`helm template`
  both providers, `terraform validate` on any `.tf` file touched.
- V5 The commit/PR message states the coverage numbers and what, if anything, was skipped and why.

## Boundaries
- Never lower the 90% coverage gate or add a blanket coverage exclusion to make a run pass — fix the gap or
  justify a narrow, commented exclusion for genuinely untestable lines (e.g. a real-cloud-only code path
  already covered by a fake-backed unit test, matching the pattern in `console/src/ramen_console/storage/`).
- Never skip a test by commenting it out or deleting it to unblock a run; mark it `xfail`/skip with a reason
  and open a note in `facts/STATE.md` or the PR description instead.
- A kind-cluster test run changes real cluster state (scales deployments). Restore what you changed
  (`kubectl scale`) before finishing if a later suite in the same session depends on a clean baseline — a
  zone left scaled up raises the floor for every test that runs after it.
