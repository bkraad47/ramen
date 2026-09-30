---
name: iterate
description: Fix a bug or change behavior in Ramen through a reproduce-fix-verify loop, TDD, against the current state snapshot. Use when asked to fix, change, debug, or implement something in this codebase, not just to run the existing tests (see skills/test/).
---

# Iterate on Ramen

A fix-verify loop any coding agent can run: write a failing test first, make it pass, confirm nothing else
broke, write down why. No agent-specific tooling required.

## Inputs
- The issue to fix, in plain language (a bug report, a desired behavior change, a failing CI run).
- A working tree of this repo. `skills/test/SKILL.md`'s Inputs (same toolchain requirements).

## Steps
1. Read `facts/STATE.md` (what exists now) and the relevant section of `docs/CONTRACTS.md` (what correct
   behavior is specified to be). If the issue contradicts the contract, that is a decision for a human —
   stop and ask rather than silently picking a side.
2. **Reproduce.** Write the smallest test that captures the issue, in the component it belongs to
   (`node-rs/src/.../tests`, `runtime-py/tests/`, `console/tests/`, or `tests/` for cross-component
   behavior). Run it and confirm it fails for the reason you expect — not for an unrelated setup problem.
   If you cannot make it fail, the issue may already be fixed (check `git log` for the area first) or may
   need a different reproduction than first assumed; say which before proceeding.
3. **Fix.** Make the smallest change that makes the new test pass without weakening or deleting any
   existing test. Prefer fixing the root cause over adding a special case; if the true fix is large, say so
   and propose the smaller version explicitly rather than silently doing a partial fix.
4. **Verify.** Run the full test suite for every component you touched (`skills/test/SKILL.md`'s Steps 2–4).
   If something unrelated now fails, stop — do not paper over it — and report what broke; fixing a second,
   unrelated defect in the same pass is a judgment call for whoever asked for the change, not something to
   do silently.
5. Update `facts/STATE.md` if the fix changes what's true about the system (a new capability, a changed
   default, a newly-supported backend) — keep it short, this is a snapshot, not a changelog.
6. Report what changed, why (the root cause, not just the symptom), which tests prove it, and what (if
   anything) is still open.

## Validate
(hand this section, verbatim, to a second agent or a fresh review pass)
- V1 A test exists that fails on the code from before the fix and passes on the code after it (check this
  by looking at the diff, not by trusting the report).
- V2 `skills/test/SKILL.md`'s Validate section passes in full, not just for the component that changed.
- V3 The fix's root cause is stated in the PR/commit message, not just a description of the symptom.
- V4 No existing test was weakened (a tightened assertion loosened, a case removed, a skip added) to make
  the suite pass.

## Boundaries
- Never mark something "fixed" on the strength of manual testing alone — a test must exist that would catch
  a regression.
- Never touch `docs/CONTRACTS.md` to make a fix match the spec instead of matching the spec to the fix,
  unless the task explicitly asks for a contract change — that is a binding document and a PR reviewer's
  call, not an implementation detail.
- Stop and ask a human after three reproduce-fix-verify rounds on the same issue rather than continuing to
  guess.
