# Wiki

One page per subject. [Get started](../get-started.md) and [Deploy your first group](../first-group.md) are the short
paths; these are the full ones. [Roles](../roles.md) and the [threat model](../threat-model.md) sit beside them.

| Page | What it covers |
|---|---|
| [Deploy on GCP](deploy-gcp.md) | Terraform, images, Helm, the first zone and group, what the console's service account may do, day 2, teardown |
| [Deploy on AWS](deploy-aws.md) | The same on EKS with DynamoDB, S3, Secrets Manager and a load balancer, and what its IAM role may do |
| [Deploy on Azure](deploy-azure.md) | AKS with Table Storage, Blob, Key Vault, Envoy Gateway and Let's Encrypt; subscriptions without availability zones |
| [IAM and network on GCP](iam-network-gcp.md), [AWS](iam-network-aws.md), [Azure](iam-network-azure.md) | Every identity Ramen uses on each cloud and what it may do — console, nodes, workers, per-group build identities, the evals Jobs, Workload Identity / IRSA / federated credentials — and every network path: the edge, NetworkPolicies, firewall / security group / NSG rules, CIDRs, satellites |
| [MCP features](mcp-features.md) | Every MCP capability a worker serves and how a group repo codes it, with the demo repo's examples: tools, resources, templates, prompts, tasks, MRTR, subscriptions, apps, skills, guardrails, evals, LLM configs |
| [Groups, zones and regions](groups-zones.md) | The model, creating each one, deploying, scaling, rebalance, per-tool access, blocked packages, IP rules, deleting |
| [Autoscaling](autoscaling.md) | Ramen scales workers between 1 and an admin's max: the decision rules, cooldowns, audit, load-test results |
| [Sleeping workers](sleeping-workers.md) | 1.0: an idle zone scaled to 0 behind one small activator pod: what idle means, what the first caller sees, what it costs |
| [The MCP repo](mcp-repo.md) | The folder layout, the JSON beside each tool, resources and prompts, what a package can do since 1.0, apps and skills (1.0.0c), secrets in code, testing locally, heavy dependencies |
| [Worker images](worker-images.md) | The release image, an image built on deploy from `requirements.txt` (1.0), or one you pin: the builder identity per cloud, failed builds, settings |
| [Secrets](secrets.md) | Scopes, how a value reaches your code, where values live on each cloud, rotation |
| [Users and access](users-access.md) | Roles per group, custom roles, where a membership comes from, sign-in, service-account permissions, audit and logs |
| [Connect a client with OAuth](connect-oauth.md) | Registering a client; Claude Code over HTTP, the bridge for Cursor, VS Code and Claude Desktop; where the bridge keeps tokens; what goes wrong |
| [Connect a client with a password](connect-password.md) | The account side of the same flow |
| [API keys and the API](api-keys.md) | The two kinds of key, the OpenAPI page, a cheat sheet, a CI deploy |
| [Deploy from GitHub Actions](deploy-github-actions.md) | An API key for CI, the repo secret, a workflow that deploys on push and waits for the job, rotation |
| [Throttling with Redis](throttling.md) | Per-address and per-token limits, scope and item, shared across zones |
| [Entra ID and Google Workspace](sso.md) | Providers on the Config page, groups read from Graph or Cloud Identity, role rules, OAuth-only roles, troubleshooting |
| [Guardrails](guardrails.md) | NeMo Guardrails rails or your own `policy.py`, per tool, before the call and after the result; 1.0.0c Phase 2.5: LLM-backed rails on a provider scoped to the group or the org, and the rails as a client policy the workers gate (`-32029`) and the bridge runs locally |
| [Multi round-trip requests](mrtr.md) | 1.0: a tool that asks the client for input and is called again: the flow, the signed state, guardrails on every round, older clients |
| [Tasks](tasks.md) | 1.0: long-running tools that answer with a task id: who gets one, the lifecycle, the Redis a multi-worker zone needs, limits |
| [Telemetry](telemetry.md) | 1.0: OpenTelemetry export of calls and deploys, what is and is never exported, what agents are told, consent per group |
| [Evals, LLM providers and client policy](evals.md) | 1.0.0c: a promptfoo suite in the repo run on every deploy, report or gate per group, the LLM keys on the Config page, the evals version clients must hold, the bridge's policy bundle and editor files |
| [SkillSpector scans](skillspector.md) | 1.0.0c Phase 2: NVIDIA SkillSpector over the repo's `mcp/skills/*` and the served tools on every deploy, the report page, gate per group, more LLM providers, the bridge's local scan |
| [LLM configs from the group repo](llm-configs.md) | 1.0.0c Phase 3: `mcp/llm-config/` — client config fragments validated and versioned on deploy, accepted by every person on every sign-in path, written by the bridge, gated by the workers (`-32028`); the console's OTLP ingest for client telemetry |
| [Backups](backups.md) | What an export holds and what it does not, preview, restore, prune, reconcile |
| [Configuration](configuration.md) | The config file, the public address (base URI) and path-prefix proxies, the state store, every variable that matters, restricting actions |

Reference material sits one level deeper: [transport hop by hop](transport.md), the
[threat model](../threat-model.md), the [cloud-ops skills](skills.md) and the
[interface contracts](../CONTRACTS.md).
