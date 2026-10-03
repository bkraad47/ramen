# Concepts

| Term | Meaning | Who manages it |
|---|---|---|
| **Group** | A tenant or team. Owns one git repo (URL + ref), one bucket prefix, its secrets, `rmk_` MCP keys, users, SA restrictions. Deleting a group destroys its cloud resources. | super admin creates; group admins manage |
| **Environment** | A deployable configuration of a group: a git ref plus a list of zones, a verbose-logging flag, a blocked-tool list and the last deploy result. | group admin |
| **Zone** | A cloud zone/region where workers run. On GCP/AWS a zone is a Kubernetes namespace `ramen-<group>-<zone>` pinned to that cloud zone, with its own service account, LB route and IP rules. Locally it is the compose worker. | super admin creates zones; group admins attach them to environments |
| **Worker** | One pod: Rust MCP node (gRPC `ramen.v1.Mcp` / `Admin` / `grpc.health.v1.Health` on port 8080) + Python runtime, 1:1. Two tracks per zone: `worker` (stable) and `worker-canary`. Count is set by admins, size (`s`/`m`/`l`) by super admins. | |
| **Package** | One tool, resource or prompt: a folder under `mcp/<type>s/<name>/` with `<name>.json` (proto) and `<name>.py`. See [Protos](protos.md). | the group's repo |
| **Deploy job** | Background job returned by `POST .../deploy` (202) and polled at `/api/v1/jobs/{id}`; carries a status, a streamed log and the package list reported by workers. | |
| **MCP key** `rmk_…` | Bearer key MCP clients send to a worker as gRPC metadata `authorization: Bearer` (the bridge's `--key`). Generated on the group page or `POST /api/v1/groups/{g}/mcp-keys`, shown once, pushed to workers on deploy. | group admin |
| **Bridge** `ramen-mcp-bridge` | Stdio MCP server, its own package/repo since v0.5.7 (also installed in the worker image) that forwards each JSON-RPC message to `Mcp/Call` with the key and the `ramen-group` / `ramen-zone` routing metadata. How Claude Desktop, Cursor and the `mcp` SDK connect. | the client's owner |
| **Routing metadata** | gRPC metadata `ramen-group` and `ramen-zone` on every call. The load balancer (GKE Gateway HTTPRoute header match / ALB listener rule) picks the zone from them; the node ignores them for auth. | set by the client / bridge |
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
| Console (HTTP) | `https://localhost:8443` | `https://<console_ip>/` | `https://<alb-dns>/` |
| MCP endpoint (gRPC) | `localhost:8080`, plaintext h2c (`--insecure`) | `<console_ip>:443`, TLS (`--tls --ca <pem>` while self-signed) + metadata `ramen-group`, `ramen-zone` | `<alb-dns>:443`, TLS + the same metadata |
| Worker health | `grpc.health.v1.Health/Check` on the same port (unauthenticated) | same, also used by the Gateway `HealthCheckPolicy` | same, ALB health check gRPC code 0 |
| Store | Firestore emulator | Firestore | DynamoDB |
| Bucket | `/buckets/<group>` (volume) | `gs://ramen-<project>-groups/<group>` | `s3://ramen-<account>-groups/<group>` |
