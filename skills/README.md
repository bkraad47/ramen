# Ramen skills

[Agent Skills](https://agentskills.io/specification) for operating Ramen and for working on its code. Each folder is
one skill: a `SKILL.md` (frontmatter `name`, `description`, `license`, `compatibility`, `metadata`, then the
instructions) and, where it helps, `references/` (read on demand) and `scripts/` (run as given). They work with any
Agent Skills client: Claude Code, Cursor, Gemini CLI, Copilot or your own.

## Install
Copy a skill's folder into your agent's skills directory, keeping the folder name:

```sh
cp -r skills/deploy-gcp ~/.claude/skills/            # Claude Code, every project
cp -r skills/deploy-gcp .claude/skills/              # Claude Code, this project only
```

Other clients: their skills directory, or point the agent at `skills/<name>/SKILL.md`. The operate skills assume the
repo is checked out at the release tag; the API ones read `RAMEN_CONSOLE_URL` and an `rmn_` key (`RMN`) from the
environment.

## Operate a deployment
| Skill | Use when |
|---|---|
| `deploy-gcp/` | installing, verifying or tearing down Ramen on GKE |
| `deploy-aws/` | installing, verifying or tearing down Ramen on EKS |
| `deploy-azure/` | installing, verifying or tearing down Ramen on AKS, Entra ID sign-in |
| `rotate-keys/` | rotating or revoking `rmk_` / `rmn_` keys, sessions and tokens, the admin key, password, Fernet key, the install CA |
| `backup-restore/` | a versioned backup before a risky change, a restore, a move between stores or clouds |
| `scale-zone/` | adding or removing a zone, count / max / size, rebalance, sleeping zones |

## Contribute to the code
| Skill | Use when |
|---|---|
| `test/` | running the suites with the 90% coverage gate and CI's lint |
| `iterate/` | fixing a bug or changing behavior: reproduce, fix, verify, against `facts/STATE.md` and `docs/CONTRACTS.md` |

## Conventions
- The operate skills use only documented surfaces (the console API, `terraform`, `helm`, `kubectl`, the cloud CLIs),
  never print secrets, pin the kube context, and tear down only on an explicit request.
- Every skill ends with a **Validate** section to hand verbatim to a cheaper, read-only sub-agent, which replies
  `PASS` or `FAIL: <check> — <evidence>` and runs no write call, `apply`, `helm` or mutating `kubectl`. The task is
  not done before a `PASS`; after three `FAIL`s, stop and ask a human.
- CI validates every skill with the reference validator:
  `uvx --from skills-ref==0.1.1 agentskills validate skills/<name>`.
