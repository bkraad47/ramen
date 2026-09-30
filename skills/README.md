# Ramen skills

[agentskills](https://agentskills.io)-style skills for agents (Claude Code, Cursor, Gemini CLI, Copilot, or any
other agentskills-compatible client — AI-agnostic by design) working on Ramen. Each folder has one `SKILL.md`:
front matter (`name`, `description`), **Inputs**, numbered **Steps**, **Validate**, **Boundaries**.

Two kinds, in two groups below: **operate** a running Ramen deployment, or **contribute** to Ramen's own code.
The operate skills only use the documented surfaces: the console API (`rmn_` key), the worker's gRPC surface
(`grpcurl` / `ramen-mcp-bridge` with an `rmk_` key), `terraform`, `helm`, `kubectl`, `make`. They never read secret
values and never post anything outside the deployment.

## Operate a deployment
| Skill | Use when |
|---|---|
| `deploy-gcp/` | bringing Ramen up on a GCP project (verified path) |
| `deploy-aws/` | bringing Ramen up on AWS (**untested path**; the skill demands a dry run + validator) |
| `rotate-keys/` | rotating `rmk_` MCP keys, `rmn_` API keys, the worker admin key or the Fernet key |
| `backup-restore/` | taking a versioned backup before risky changes, restoring after a bad one |
| `scale-zone/` | adding/removing a zone, changing worker count/size, rebalancing |

## Contribute to the code (v0.5.5 I17)
| Skill | Use when |
|---|---|
| `test/` | running the test suite (Rust, Python runtime, Python console, cross-component harness) with the 90% coverage gate |
| `iterate/` | fixing a bug or changing behavior: a reproduce-fix-verify loop, TDD, against `facts/STATE.md` and `docs/CONTRACTS.md` |

`facts/STATE.md` (repo root) is the scratchpad/fact block these two skills read first — a short, kept-current
snapshot of what exists, separate from `docs/CONTRACTS.md` (the binding, append-only spec). Any agent, not just
one vendor's, can point at either file with no special setup.

## Loading
Point the agent at the folder (`skills/<name>/SKILL.md`) or copy it into the agent's skills directory
(`.claude/skills/<name>/SKILL.md` for Claude Code). The skill assumes the repo is checked out and `RAMEN_CONSOLE_URL`
+ `RAMEN_API_KEY` (an `rmn_` key) are in the environment unless it says otherwise.

## Validation sub-agent convention
Every skill ends with a **Validate** section written so it can be handed verbatim to a cheaper, read-only
sub-agent. Convention:

1. The parent agent finishes the Steps, then spawns a sub-agent with **only** the Validate section, the URLs, and
   a key that cannot mutate (a `viewer`-scoped `rmn_` key, or the `rmk_` key for `tools/call`).
2. The sub-agent runs the listed checks and replies `PASS` or `FAIL: <check> — <evidence>` (HTTP status, job
   status, gRPC status, JSON result). It must not run any write call, `terraform apply`, `helm`, or `kubectl` verbs
   other than `get`/`describe`/`logs`/`port-forward`.
3. The parent may not report the task as done until it has a `PASS`. On `FAIL` it fixes and re-validates; after
   three failures it stops and asks a human.

Prompt template for the validator:
```
You are a read-only validator. Run only the checks below and nothing else; do not change anything.
Reply with exactly one line: PASS, or FAIL: <check id> - <evidence>. Then a short list of every command you ran.
<paste the Validate section>
```
