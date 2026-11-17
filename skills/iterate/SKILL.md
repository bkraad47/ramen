---
name: iterate
description: Fix a bug or change behavior in the Ramen codebase through a reproduce-fix-verify loop — a failing test first, the smallest root-cause fix, the full suites of every touched component, then the state snapshot updated. Use when asked to fix, change, debug or implement something in Ramen's code, not just to run the existing tests (that is the test skill).
license: BSD-3-Clause
compatibility: Needs a checkout of github.com/bkraad47/ramen and the toolchain of the test skill (uv, Rust from rust-toolchain.toml).
metadata:
  version: "1.0.0"
  author: bkraad47
---

# Iterate on Ramen

A fix-verify loop any coding agent can run: write a failing test first, make it pass, confirm nothing else broke,
write down why. No agent-specific tooling.

## Inputs
- The issue in plain language (a bug report, a wanted change, a failing CI run).
- A working tree of the repo with the test skill's toolchain.

## Steps
1. Read `facts/STATE.md` (what exists now) and the relevant section of `docs/CONTRACTS.md` (what is specified).
   If the issue contradicts the contract, that is a human's decision: stop and ask.
2. **Reproduce.** Write the smallest test that captures the issue where it belongs (`node-rs/src/…` unit tests or
   `node-rs/tests/`, `runtime-py/tests/`, `console/tests/`, or `tests/` for cross-component behavior). Run it and
   confirm it fails for the expected reason. If it will not fail, check `git log` for the area and say whether the
   issue is already fixed or needs another reproduction.
3. **Fix.** The smallest change that makes the new test pass without weakening or deleting an existing test. Fix
   the root cause; if the real fix is large, say so and propose the smaller version explicitly.
4. **Verify.** Run the full suites and CI's lint for every component you touched (the test skill, steps 2–5). Docs
   you changed must pass `mkdocs build --strict`. Something unrelated now failing: stop and report it.
5. Update `facts/STATE.md` when the fix changes what is true (a capability, a default, a backend); a snapshot, not
   a changelog. Name user-visible changes in `CHANGELOG.md` under the version being built.
6. Report what changed, the root cause, which tests prove it, and what is still open.

## Validate
Hand verbatim to a second agent or a fresh review pass.
- V1 A test exists that fails on the code before the fix and passes after it (check the diff, not the report).
- V2 The test skill's Validate passes in full, not only for the changed component.
- V3 The commit or PR message states the root cause, not just the symptom.
- V4 No existing test was weakened (an assertion loosened, a case removed, a skip added) to make the suite pass.

## Boundaries
- Never call something fixed on manual testing alone: a test must catch a regression.
- Never edit `docs/CONTRACTS.md` to make the spec match the fix unless the task asks for a contract change; it is
  binding and append-only.
- Stop and ask a human after three reproduce-fix-verify rounds on the same issue.
