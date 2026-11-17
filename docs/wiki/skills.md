# Agent skills

`skills/` in the repo holds [Agent Skills](https://agentskills.io/specification) that a coding or operations agent
(Claude Code, Cursor, Gemini CLI, Copilot, a CI bot) loads to deploy and run Ramen, or to work on its code, the way
these docs describe. Each skill is a folder named after the skill:

```
skills/deploy-gcp/
├── SKILL.md            frontmatter (name, description, license, compatibility, metadata) + the steps
├── references/         loaded only when a step needs it: first group, satellites, teardown
└── scripts/            run as given: leftovers.sh lists what a teardown left behind
```

The agent reads only the `name` and `description` until a task matches, then the `SKILL.md` body, then a reference
file when a step points at it. Every skill is checked in CI with the specification's reference validator
(`uvx --from skills-ref==0.1.1 agentskills validate skills/<name>`).

## The skills

| Skill | What it does |
|---|---|
| [`deploy-gcp`](https://github.com/bkraad47/ramen/tree/main/skills/deploy-gcp) | Terraform, Helm and the first group on GKE, as in [Deploy on GCP](deploy-gcp.md); satellites; teardown in the right order and a sweep of what the console made outside Terraform (Secret Manager, group image repositories, network endpoint groups) |
| [`deploy-aws`](https://github.com/bkraad47/ramen/tree/main/skills/deploy-aws) | The same on EKS, as in [Deploy on AWS](deploy-aws.md): the ALB subnets for the network policies (D89), the exported certificate, Secrets Manager, ECR, EBS and WAF leftovers |
| [`deploy-azure`](https://github.com/bkraad47/ramen/tree/main/skills/deploy-azure) | The same on AKS, as in [Deploy on Azure](deploy-azure.md): Envoy Gateway and Let's Encrypt, Calico, Managed Redis, the registry's repository permissions, Microsoft Entra ID sign-in |
| [`rotate-keys`](https://github.com/bkraad47/ramen/tree/main/skills/rotate-keys) | Rotate or revoke `rmk_` and `rmn_` keys, a person's sessions and tokens, OAuth clients (pushed to the workers at once, D92), the admin key and password, the Fernet key, and the install CA in two phases |
| [`backup-restore`](https://github.com/bkraad47/ramen/tree/main/skills/backup-restore) | A [backup](backups.md), its checks, a previewed restore, a move between stores or clouds |
| [`scale-zone`](https://github.com/bkraad47/ramen/tree/main/skills/scale-zone) | Add or remove a zone, set count, max and size, rebalance, [sleeping workers](sleeping-workers.md) |
| [`test`](https://github.com/bkraad47/ramen/tree/main/skills/test) | The suites with the 90% coverage gate and CI's lint, docs and secrets checks |
| [`iterate`](https://github.com/bkraad47/ramen/tree/main/skills/iterate) | A reproduce-fix-verify loop on the code, test first |

## Install

Copy a skill's folder, name unchanged, into the agent's skills directory:

```sh
git clone --depth 1 --branch v1.0.0 https://github.com/bkraad47/ramen && cd ramen
cp -r skills/deploy-gcp ~/.claude/skills/        # Claude Code, every project (.claude/skills/ for one project)
```

Other clients take the same folder in their own skills directory, or the path to `SKILL.md` as context. The deploy
skills run from a checkout of the repo at the release tag (they use its Terraform and Helm chart); the others need
only the console's address (`RAMEN_CONSOLE_URL`) and an `rmn_` key ([API keys](api-keys.md)).

## What every skill promises

- **No secrets in the output.** Passwords and keys are generated inline or read into a shell variable, never
  printed; a new key is handed over once. Account and subscription ids stay out of reports.
- **The right cluster.** The deploy skills pin the kube context they created and pass it to every `kubectl` and
  `helm` call.
- **Teardown only on request**, in the documented order: groups and satellites, `helm uninstall` before `terraform
  destroy`, then the leftovers sweep, with deletions confirmed by a person.
- **A validator.** Every skill ends with a **Validate** section that the running agent hands to a cheaper,
  read-only sub-agent. It answers `PASS` or `FAIL: <check> — <evidence>` and never mutates anything; the task is not
  done until it passes, and three failures stop the run for a person to decide.
