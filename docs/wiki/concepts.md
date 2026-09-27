# Concepts

| Term | Meaning | Who manages it |
|---|---|---|
| **Group** | A tenant or team. Owns one git repo (URL + ref), one bucket prefix, its secrets, `rmk_` MCP keys, users, SA restrictions. Deleting a group destroys its cloud resources. | super admin creates; group admins manage |
| **Environment** | A deployable configuration of a group: a git ref plus a list of zones, a verbose-logging flag, a blocked-tool list and the last deploy result. | group admin |
| **Zone** | A cloud zone/region where workers run. On GCP/AWS a zone is a Kubernetes namespace `ramen-<group>-<zone>` pinned to that cloud zone, with its own service account, LB route and IP rules. Locally it is the compose worker. | super admin creates zones; group admins attach them to environments |
| **Worker** | One pod: Rust MCP node + Python runtime, 1:1. Two tracks per zone: `worker` (stable) and `worker-canary`. Count is set by admins, size (`s`/`m`/`l`) by super admins. | |
| **Package** | One tool, resource or prompt: a folder under `mcp/<type>s/<name>/` with `<name>.json` (proto) and `<name>.py`. See [Protos](protos.md). | the group's repo |
| **Deploy job** | Background job returned by `POST .../deploy` (202) and polled at `/api/v1/jobs/{id}`; carries a status, a streamed log and the package list reported by workers. | |
| **MCP key** `rmk_…` | Bearer key MCP clients send to a worker. Minted on the group page or `POST /api/v1/groups/{g}/mcp-keys`, shown once, pushed to workers on deploy. | group admin |
| **API key** `rmn_…` | Console automation key sent as `X-Ramen-Api-Key` to `/api/v1/*`. Scoped to a role and groups never wider than its creator's. Never reaches a worker. | super admin / group admin |
| **Secret** | Named value scoped to a group, optionally to an environment and a zone. Referenced from tool code as `{{$group.NAME}}`. Never displayed. See [Secrets](../how-tos/secrets.md). | group admin |

## Roles
- **Super admin**: everything, including users, groups, zones, worker sizes, SA rules, backups, config, refresh.
- **Group admin** (per group): environments, deploys, scale count, secrets (names), MCP keys, IP rules, rebalance,
  viewers in the same group, SA restrictions (must not clash with super-admin rules → 409), permission requests.
- **Viewer** (per group): read-only — repo, load, packages, secret names, logs, audit.
- **Cloud admin**: out-of-band operator who runs Terraform/Helm and can reset the bootstrap super admin via
  `RAMEN_ADMIN_EMAIL` / `RAMEN_ADMIN_PASSWORD` (re-applied on every console start).

## Addresses
| | Local | GCP | AWS |
|---|---|---|---|
| Console | `https://localhost:8443` | `https://<console_ip>/` | `https://<alb-dns>/` |
| MCP endpoint | `http://localhost:8080/mcp` | `https://<console_ip>/mcp/<group>/<zone>` | `https://<alb-dns>/mcp/<group>/<zone>` |
| Store | Firestore emulator | Firestore | DynamoDB |
| Bucket | `/buckets/<group>` (volume) | `gs://ramen-<project>-groups/<group>` | `s3://ramen-<account>-groups/<group>` |
