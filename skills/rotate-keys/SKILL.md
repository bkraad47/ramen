---
name: rotate-keys
description: Rotate Ramen credentials without downtime — rmk_ MCP keys for a group, rmn_ console API keys, the worker admin key, the console admin password, or the Fernet encryption key. Use when asked to rotate, revoke, or replace keys or after a suspected leak.
---

# Rotate keys

Which key? Pick the section. All console calls use `$U=$RAMEN_CONSOLE_URL/api/v1` with an `rmn_` super-admin
key in `X-Ramen-Api-Key` (or a cookie session). Contract: `docs/CONTRACTS.md` §3, §4a, §9.

## A. `rmk_` MCP keys (what MCP clients send to workers)
Zero-downtime because workers accept the union of every key generated for the group.
1. Generate the new key: `POST $U/groups/<g>/mcp-keys {name:"<client>-<date>"}` → `{id,key:"rmk_…"}`. Hand the key to the client owner once.
2. Deploy every environment of the group (`POST $U/groups/<g>/environments/<e>/deploy {canary:true}`, poll `/jobs/<id>` to `ok`) so the new key reaches all zones.
3. Wait for clients to switch (agree a window). Then revoke: `GET $U/groups/<g>/mcp-keys` → find the old `id` → `DELETE $U/groups/<g>/mcp-keys/<id>`.
4. Deploy again; the old key stops working when the reload completes.

## B. `rmn_` API keys (console automation)
1. `POST $U/api-keys {name,role,groups}` with the same or narrower scope → new key, hand over once.
2. Update the consumer (CI secret). 3. `DELETE $U/api-keys/<old id>`. Keys have no rotation grace period beyond this ordering.

## C. Worker admin key (`RAMEN_ADMIN_KEY`, console → `ramen.v1.Admin/Reload` + `Admin/Metrics`, gRPC metadata `x-ramen-admin-key`)
- Local: edit `deploy/local/.env`, `make up` (recreates console + worker).
- GCP/AWS: `helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values --set console.secrets.RAMEN_ADMIN_KEY=<new>`; then deploy each environment once (the deploy writes the new key into every zone's `ramen-deploy` Secret and rolls the workers). Until a zone is redeployed, its workers still hold the old key and the console's reload would fail — so deploy all zones right after the upgrade.

## D. Console admin password
Change the env/Helm value `RAMEN_ADMIN_PASSWORD` and restart the console; the bootstrap super admin is re-applied on every start. Or `POST $U/users/<id>/password {password}` for any user.

## E. Fernet key (`RAMEN_FERNET_KEY`, encrypts values at rest)
Rotating this re-encrypts nothing automatically. Procedure:
1. `POST $U/backups {target:"bucket"}` (backup excludes secrets; secret values must be re-entered if the store backend is used).
2. If `RAMEN_SECRETS_BACKEND=store`: export the list of secret **names** and scopes (`GET $U/groups/<g>/secrets`) for every group; get the values from their owners.
3. Set the new key (Helm `console.secrets.RAMEN_FERNET_KEY` / `.env`), restart the console. Encrypted fields written with the old key are now unreadable: users log in via the bootstrap admin (re-applied), OAuth, or a password reset; `rmn_` keys must be generated again; secrets re-added (store backend) — with `gcp`/`aws` backends the values live in the cloud secret manager and survive.
4. Deploy every environment so workers get the re-added secrets.
Prefer `gcp`/`aws` secret backends in production precisely so that E is cheap.

## Validate
(read-only sub-agent; needs the **new** `rmk_` and a viewer `rmn_` key, plus the old `rmk_` if step A was run)
- V1 `tools/list` with the new `rmk_` through the bridge (`ramen-mcp-bridge --target <lb>:443 --tls --ca lb.pem --key $RMK_NEW --group <g> --zone <z>`; locally `--target localhost:8080 --insecure`) or `grpcurl … ramen.v1.Mcp/Call` (see deploy-gcp V3) → OK with tools; the old key → `Unauthenticated`.
- V2 Same call with the old revoked `rmk_` → 401 (only after A.4).
- V3 `curl -sk -H "X-Ramen-Api-Key: $RMN_NEW" $CONSOLE/api/v1/me` → 200; with the old `rmn_` → 401.
- V4 `curl -sk -H "X-Ramen-Api-Key: $RMN_NEW" $CONSOLE/api/v1/audit` shows entries `mcp-keys.create`/`delete` (or `api-keys.*`) with `ok:true`.
- V5 For C: the last deploy job of every environment is `ok` (`GET $U/jobs/<id>` or the environment's `last_deploy.status`).

## Boundaries
- Never log or paste key values into tickets, commits or chat beyond the single hand-over. Never read secret values (the API cannot return them anyway).
- Do not revoke a key before its consumer confirmed the switch, except on an explicit leak response ("revoke now").
- Fernet rotation (E) needs a human go-ahead: it invalidates sessions and stored values.
