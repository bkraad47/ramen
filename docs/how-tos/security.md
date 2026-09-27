# Security

## Threat model in one paragraph
Workers run untrusted-ish user code (your team's tools) and are reachable by MCP clients across the internet
through a load balancer. The console holds secrets and cloud credentials. Ramen keeps protocol handling, auth and
network policy in the Rust node and the console, and keeps user code in an on-demand Python process that only ever
sees the secrets it was scoped to.

## Identities and roles
| Actor | Auth | Scope |
|---|---|---|
| Console user | email + argon2 password, OAuth/OIDC provider button, optional magic link (v0.3.0) | `super_admin` / `group_admin` (groups) / `viewer` (groups) |
| Automation | `rmn_` API key in `X-Ramen-Api-Key` | role + groups ≤ creator's |
| MCP client | `rmk_` MCP key in `Authorization: Bearer` | one group's workers |
| Console → worker | `X-Ramen-Admin-Key` (`RAMEN_ADMIN_KEY`) on `/admin/reload` | separate `RAMEN_ADMIN_CIDRS` |
| Worker → cloud | Workload Identity (GSA) / IRSA (IAM role) per group+zone, least privilege (bucket prefix, secret prefix) | |
| Console → cloud | master GSA / IAM role from Terraform | |

Bootstrap super admin comes from `RAMEN_ADMIN_EMAIL` / `RAMEN_ADMIN_PASSWORD` and is re-applied on every start
(cloud admins reset it by changing the env). `auth.password_login: false` (super admin toggle) disables password
login except break-glass with `RAMEN_ADMIN_FORCE_PASSWORD=1`.

## Keys
- **MCP keys** `rmk_<id>_<secret>`: stored hashed in the group's secrets, shown once, unioned into
  `RAMEN_MCP_KEYS` on the workers at deploy. **No keys = deny all** (401, JSON-RPC `-32001`). Revoke on the group
  page, then deploy to push the change.
- **API keys** `rmn_<id>_<secret>`: stored hashed, shown once, scoped. Revoke on the API Keys page.
- **Admin key**: gates `/admin/reload`; rotate with the [rotate-keys skill](../wiki/skills.md).
- **Fernet key** `RAMEN_FERNET_KEY`: encrypts password hashes, secret values, API-key hashes and git tokens at
  rest. Required in production; rotation = re-encrypt (see the skill).

## Network
- Node: `RAMEN_ALLOWED_CIDRS` (IPv4 + IPv6) for `/mcp`, `RAMEN_ADMIN_CIDRS` for `/admin/*`, `RAMEN_TRUST_PROXY=1`
  to honour the first `X-Forwarded-For` hop behind the LB.
- IP rules from the console become **Cloud Armor** (GCP) / **WAFv2** (AWS) policies on the zone's backend *and*
  node CIDRs, so the node still enforces if the edge is misconfigured. Changing rules rolls the zone's pods.
- TLS: ingress/LB terminates. Locally and on the throwaway GCP setup the cert is self-signed (D17); swap in a
  managed certificate when a domain exists (`deploy/scripts/selfsigned.sh` is the only place that assumes otherwise).
- Console sessions: signed cookie (`ramen_session`, 12 h, `SameSite=Lax`, `Secure` with `RAMEN_COOKIE_SECURE=1`).
- CSRF (v0.3.0): per-session `ramen_csrf` token on HTML forms / `X-Ramen-CSRF` header; JSON API with an API key is exempt.

## Secrets
Never returned by any endpoint, page, backup or log. Delivered to workers only as `RAMEN_SECRET_<GROUP>__<NAME>`
env vars scoped to env + zone. Redacted from tool errors. Details: [Secrets](secrets.md).

## Audit and logs
- `audit`: every mutating console request `{ts, user, ip, action, target, ok, tags}` (also failed logins).
- `activity`: deploys, permission requests, invocations summary.
- Worker log: one JSON line per MCP call `ts, ip, group, method, name, status, ms, key_id` (key id, never the key).
  `RAMEN_VERBOSE=1` per environment logs full request/response bodies — turn it on only while debugging.

## Isolation
- Runtime is spawned per worker with the deploy env only; killed after `RAMEN_SIDECAR_IDLE_SECS`; a call timeout
  (`RAMEN_CALL_TIMEOUT_SECS`, 120 s) kills and respawns it.
- Containers run as uid 10001; the worker image has no shell tools beyond python/pip.
- Zone namespaces are labelled `ramen.io/group=<group>`; group delete destroys them and their service accounts.
- Tool blocking (v0.3.0): per-environment block list enforced by the node (`-32601`), so a bad tool can be pulled
  without a redeploy of code.

## Reporting
Open a private security advisory on GitHub (Security → Advisories) rather than a public issue.
