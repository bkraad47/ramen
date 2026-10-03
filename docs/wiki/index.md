# Wiki

One page per subject. [Get started](../get-started.md) is the short path; these are the full ones.

| Page | What it covers |
|---|---|
| [Deploy on GCP](deploy-gcp.md) | Terraform, images, Helm, the first zone and group, what the console's service account may do, day 2, teardown |
| [Deploy on AWS](deploy-aws.md) | The same on EKS with DynamoDB, S3, Secrets Manager and a load balancer, and what its IAM role may do |
| [Groups, zones and regions](groups-zones.md) | The model, creating each one, deploying, scaling, rebalance, blocked packages, IP rules, deleting |
| [The MCP repo](mcp-repo.md) | The folder layout, the JSON beside each tool, resources and prompts, secrets in code, testing locally, heavy dependencies |
| [Secrets](secrets.md) | Scopes, how a value reaches your code, where values live on each cloud, rotation |
| [Users and access](users-access.md) | Roles per group, the Users page, sign-in, service-account permissions, audit and logs |
| [Connect a client with OAuth](connect-oauth.md) | Registering a client, Claude Code, the bridge, what the token is, what goes wrong |
| [Connect a client with a password](connect-password.md) | The account side of the same flow |
| [API keys and the API](api-keys.md) | The two kinds of key, the OpenAPI page, a cheat sheet, a CI deploy |
| [Throttling with Redis](throttling.md) | Per-address and per-token limits, scope and item, shared across zones |
| [Entra ID and Google Workspace](sso.md) | OIDC sign-in and claim-to-role rules. Untested against a real tenant |
| [Backups](backups.md) | What an export holds and what it does not, preview, restore, prune, reconcile |
| [Configuration](configuration.md) | The config file, the state store, every variable that matters, restricting actions |

Reference material sits one level deeper: [transport hop by hop](transport.md), the
[threat model](../threat-model.md), the [cloud-ops skills](skills.md) and the
[interface contracts](../CONTRACTS.md).
