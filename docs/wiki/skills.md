# Cloud-ops agent skills

`skills/` in the repo holds [agentskills](https://agentskills.io)-style skills that a coding or ops agent (Claude
Code, Cursor, a CI bot) can load to operate Ramen safely. Each skill is a folder with a `SKILL.md`
(front matter `name` / `description`, numbered steps, explicit boundaries).

| Skill | Purpose |
|---|---|
| [`deploy-gcp`](https://github.com/bkraad47/ramen/tree/main/skills/deploy-gcp) | Terraform + images + Helm bring-up on GKE, then first zone/group/deploy through the API |
| [`deploy-aws`](https://github.com/bkraad47/ramen/tree/main/skills/deploy-aws) | Same for EKS (untested path; the skill says so and demands a dry run) |
| [`rotate-keys`](https://github.com/bkraad47/ramen/tree/main/skills/rotate-keys) | Rotate `rmk_` MCP keys, `rmn_` API keys, the admin key and the Fernet key without downtime |
| [`backup-restore`](https://github.com/bkraad47/ramen/tree/main/skills/backup-restore) | Versioned JSON backups to local/bucket and restores |
| [`scale-zone`](https://github.com/bkraad47/ramen/tree/main/skills/scale-zone) | Add a zone, scale counts/sizes, rebalance, remove a zone |

## Validation sub-agent convention
Every skill ends with a **Validate** section. The convention is that the agent running the skill spawns a cheaper,
read-only sub-agent with only that section and the URLs/keys it needs; the sub-agent reports PASS/FAIL with the
evidence (HTTP status, gRPC status, job status, `tools/call` result) and never mutates anything. The parent agent may not
declare the task done until the validator has passed. `skills/README.md` has the exact wording to hand to the
sub-agent.
