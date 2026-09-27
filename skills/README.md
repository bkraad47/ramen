# Ramen cloud-ops skills

[agentskills](https://agentskills.io)-style skills for agents (Claude Code, Cursor, CI bots) that operate a Ramen
deployment. Each folder has one `SKILL.md`: front matter (`name`, `description`), **Inputs**, numbered **Steps**,
**Validate**, **Boundaries**. Skills only use the documented surfaces: the console API (`rmn_` key), `terraform`,
`helm`, `kubectl`, `make`. They never read secret values and never post anything outside the deployment.

| Skill | Use when |
|---|---|
| `deploy-gcp/` | bringing Ramen up on a GCP project (verified path) |
| `deploy-aws/` | bringing Ramen up on AWS (**untested path**; the skill demands a dry run + validator) |
| `rotate-keys/` | rotating `rmk_` MCP keys, `rmn_` API keys, the worker admin key or the Fernet key |
| `backup-restore/` | taking a versioned backup before risky changes, restoring after a bad one |
| `scale-zone/` | adding/removing a zone, changing worker count/size, rebalancing |

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
   status, JSON result). It must not run any write call, `terraform apply`, `helm`, or `kubectl` verbs other than
   `get`/`describe`/`logs`.
3. The parent may not report the task as done until it has a `PASS`. On `FAIL` it fixes and re-validates; after
   three failures it stops and asks a human.

Prompt template for the validator:
```
You are a read-only validator. Run only the checks below and nothing else; do not change anything.
Reply with exactly one line: PASS, or FAIL: <check id> - <evidence>. Then a short list of every command you ran.
<paste the Validate section>
```
