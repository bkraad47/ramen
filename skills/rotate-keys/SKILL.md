---
name: rotate-keys
description: Rotate or revoke Ramen 1.0 credentials without downtime — rmk_ MCP keys, rmn_ API keys, a person's sessions and OAuth tokens, OAuth clients, the worker admin key, the console admin password, the Fernet key, and the install CA (two-phase rotation). Revocations reach running workers within seconds (D92). Use when asked to rotate, revoke, replace or expire keys, tokens or certificates, or after a suspected leak.
license: BSD-3-Clause
compatibility: Needs a reachable Ramen 1.0 console and a super-admin rmn_ key or session; helm and kubectl with the install's kube context for the admin key, password, Fernet key and chart values.
metadata:
  version: "1.0.0"
  author: bkraad47
  docs: https://bkraad47.github.io/ramen/wiki/api-keys/
---

# Rotate keys

`U=$RAMEN_CONSOLE_URL/api/v1`, header `X-Ramen-Api-Key: $RMN` (a super-admin `rmn_` key) on every call. Source of
truth: `docs/wiki/api-keys.md`, `docs/wiki/users-access.md`, `docs/wiki/transport.md` (TLS), CONTRACTS §16.7 and §39.

## Rules
- A new key is shown once: hand it to its owner once, never paste it into chat, tickets or commits. The API never
  returns a stored key or secret value; do not try to read one from the store or the cluster.
- Do not revoke before the consumer confirmed the switch, unless the request is a leak response ("revoke now").
- Fernet key and CA retirement need the human's explicit go-ahead.
- Every push answer may carry `pending: ["<group>/<zone>"]`: that zone did not get it yet and still accepts the old
  credential until the console's retry (every `RAMEN_REVOKE_RETRY_SECS`, 30 s) lands; the group page shows
  **Revocation pending**. Report pending zones; do not loop.

## A. `rmk_` MCP keys (a group's agents and clients)
1. Mint: `POST $U/groups/<g>/mcp-keys {"name":"<client>-<date>"}` → `{key, pushed, pending?}`. The console pushes it to
   the group's running workers (no deploy; the zone's workers roll to take it, about a minute). `403 configs not accepted`: the minting person must accept the
   group's LLM configs first (`POST $U/groups/<g>/configs/accept {"version": …}`, version from
   `/.well-known/ramen-policy?group=<g>&zone=<z>`).
2. The owner switches the client. 3. Revoke: `GET $U/groups/<g>/mcp-keys` → old `id` →
   `DELETE $U/groups/<g>/mcp-keys/<id>` → the workers refuse it within seconds.

## B. `rmn_` API keys (automation)
`POST $U/api-keys {"name","role","groups","client_type":"devops"}` with the same or a narrower role and groups →
update the consumer (CI secret) → `DELETE $U/api-keys/<old id>` (refused at once by the console). An `agent` key
is also mirrored as an MCP key of its groups: minting and deleting it are pushed to their workers like A.

## C. A person, their tokens, an OAuth client
- **Sign out everywhere**: `POST $U/users/<id>/sign-out` (`me` for yourself) ends the console sessions, refresh
  tokens and every live MCP access token on the workers → `{revoked_groups}`. Any role, membership or password change
  does the same.
- A client app gone bad: `DELETE $U/oauth/clients/<client_id>` denies every live token of its grants.
- A client revokes its own token with RFC 7009 `POST <console>/oauth/revoke`.
- Where a zone cannot be reached, a token still works until `RAMEN_OAUTH_ACCESS_TTL` (1 h) runs out.

## D. Worker admin key (`RAMEN_ADMIN_KEY`, `x-ramen-admin-key` on `ramen.v1.Admin/*`)
`helm --kube-context "$CTX" upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set console.secrets.RAMEN_ADMIN_KEY=$(openssl rand -hex 24)`,
`kubectl --context "$CTX" -n ramen-system rollout restart deploy/console`, then deploy every environment of every
group at once (`POST $U/groups/<g>/environments/<e>/deploy {"canary":true}`, poll `GET $U/jobs/<id>`): until a zone is
redeployed its workers hold the old key and the console's reload, metrics and wake calls to it fail. Local stack:
edit `deploy/local/.env`, `make up`.

## E. Console admin password
`helm … --reuse-values --set console.secrets.RAMEN_ADMIN_PASSWORD=$(openssl rand -base64 18)` and restart the console:
it re-applies the value to `admin@ramen.local` on every start. Tell the human the `kubectl get secret ramen-console`
command to read it; do not print it. Other people: `POST $U/users/<id>/password`.

## F. Fernet key (`RAMEN_FERNET_KEY`, encrypts the store)
Nothing re-encrypts. With the human's go-ahead: `POST $U/backups {"target":"bucket"}` (holds no secrets); list each
group's secret names (`GET $U/groups/<g>/secrets`); set the new key with Helm and restart; then re-add secrets (the
store backend; with Secret Manager / Secrets Manager / Key Vault the values survive), re-enter the Config page's
provider, SMTP and identity-provider secrets, re-mint `rmn_` keys, and deploy every environment.

## G. The install CA (D86), in two phases
1. `POST $U/config/tls/rotate` → stage `bundle`: the new CA is published beside the old one everywhere it is
   trusted. After `RAMEN_TLS_ROTATE_SETTLE_SECS` (about 3 min, `switch_after`) the new CA issues: the console's and
   every zone's leaf, then the chart services' (Redis, Phoenix, Jaeger and promptfoo restart). No redeploy.
2. Poll `GET $U/config/tls` until `stage` is `rotated` and every `leaves[].ca` is `current`.
3. With the human's OK: `POST $U/config/tls/rotate?retire=1` (409 while any leaf is not `current`).
4. Anyone who trusted the CA on their machine re-exports ConfigMap `ramen-ca` (`docs/wiki/trust-ca.md`).
A leaf marked `generic_name` (pre-D92) needs its zone redeployed.

## Validate
Read-only sub-agent with the **new** credentials (and the old ones to prove refusal). Reply `PASS` or `FAIL: <id> — <evidence>`.
- V1 A: `tools/call` through the edge with the new `rmk_` → result; with the revoked one → HTTP 401 / gRPC
  `UNAUTHENTICATED` within a minute of the revoke, on every zone not listed `pending`.
- V2 B: `GET $U/me` with the new `rmn_` → 200; the old one → 401.
- V3 C: a revoked person's MCP call with their old access token → 401 (`token_revoked` in the worker access log).
- V4 D: every environment's last deploy job `ok`; `GET $U/groups/<g>/zones/<z>/workers` shows live metrics.
- V5 G: `GET $U/config/tls` (super admin) → `stage` `single` after retire, every leaf `current`; `curl` through the
  edge still answers.
- V6 `GET $U/audit` (super admin) has `mcp_key.create` / `mcp_key.delete`, `api_key.*`, `user.sign_out` or
  `config.tls.rotate` with `ok: true` for what was done.
