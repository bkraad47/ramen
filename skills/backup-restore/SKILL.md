---
name: backup-restore
description: Take a versioned JSON backup of a Ramen console's state (groups, zones, environments, worker records, users, configuration), check it, and restore it after a bad change, into a new console, or across stores and clouds — preview first, prune only with consent. Use before risky changes, for scheduled backups, and for disaster recovery or migration.
license: BSD-3-Clause
compatibility: Needs a reachable Ramen 1.0 console and a super-admin rmn_ key or session; curl and python3.
metadata:
  version: "1.0.0"
  author: bkraad47
  docs: https://bkraad47.github.io/ramen/wiki/backups/
---

# Backup and restore

`U=$RAMEN_CONSOLE_URL/api/v1`, header `X-Ramen-Api-Key: $RMN` (super admin; backups, restore and audit are
super-admin only). Source of truth: `docs/wiki/backups.md`, CONTRACTS §4a and §13.1.

A backup holds six collections — `groups`, `zones`, `environments`, `workers`, `users`, `config` — tagged with
`release_version`. **It holds no secrets and no keys of any kind**, not even their names: no secret records, MCP
keys, API keys, password hashes or GitHub tokens. Group code lives in git and the bucket and needs no backup; deploy
jobs are not included.

## Backup
1. `curl -s -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' -X POST $U/backups -d '{"target":"bucket"}'`
   → `201 {id, release_version, counts, path}`. `bucket` writes under the groups bucket's `_backups/`; `local` writes
   under the console's `RAMEN_BACKUP_ROOT` (useful on the local stack only).
2. Download: `curl -s -H "X-Ramen-Api-Key: $RMN" $U/backups/<id>/download -o ramen-backup-<release>-<date>.json`; keep
   it with the team's recovery material, never in the repo.
3. Check the file (V1 below) and note the id in the change ticket.
Schedule it like any job (a daily GitHub Actions `curl` is enough); Ramen never prunes old backups.

## Restore
Flags, all default false: `dry_run` (plan only), `prune` (delete what the backup lacks; `config` and the caller are
never pruned), `reconcile` (re-apply the restored zones in the cluster), `force` (accept a backup from a newer
release — a human decision only).
1. Take a backup of the current state first and quote both ids.
2. Preview: `POST $U/backups/<id>/restore {"dry_run":true}` → `{restored:{<col>:{created,updated,unchanged}}, extra,
   warnings}`. `extra` is what a prune would delete: show it to the human.
3. Restore: `{"reconcile":true}` (merge, then re-apply zones), or `{"prune":true,"reconcile":true}` after the human
   approved the `extra` list. A backup from a newer release answers 409: stop and ask.
4. After it: everyone else is signed out (restored users' session epochs are bumped; the caller keeps a session).
   Users the store had lost come back unable to sign in: reset their password (`POST $U/users/<id>/password`) or
   let them use their identity provider. `orphans` lists namespaces the cloud runs that the backup never knew: never
   delete them yourself; ask, then `DELETE $U/groups/<g>` if one should go.
5. Into a **new** console or store: upload the file to the new console's `<bucket>/_backups/` (or its
   `RAMEN_BACKUP_ROOT` with `target:"local"`) first; after the restore re-add every secret, mint new MCP and API keys
   (`docs/wiki/api-keys.md`), re-enter the Config page's provider and identity secrets, and deploy every environment
   (a restored group gets a new session secret on its next deploy; OAuth tokens verify only after it).

Rehearse: restore the file into a throwaway console started with `RAMEN_STORE=memory` and check groups and people.

## Validate
Read-only sub-agent with a super-admin key for the audit (V2) or a viewer key otherwise, and the backup file path.
Reply `PASS` or `FAIL: <id> — <evidence>`.
- V1 The file parses as JSON with `release_version`, `groups`, `zones`, `environments`, `workers`, `users`, `config`;
  no string starts with `rmk_`, `rmn_`, `rjt_` or `enc:`; no `password_hash`, `session_secret`, `ca_key` or
  `client_secret` / `api_key` field anywhere.
- V2 `GET $U/audit` has `backup.create` (and `backup.restore` after a restore) with `ok: true`.
- V3 After a restore: `GET $U/groups`, `GET $U/zones` and `GET $U/environments` list everything in the file.
- V4 After restore and deploy: `GET $U/groups/<g>/zones/<z>/workers` → ≥ 1 live worker; a `tools/call` with a freshly
  minted `rmk_` key through the edge answers.
- V5 A `dry_run` of the same backup reports `created: 0` in every collection, and `extra` holds only what was made
  after it on purpose.
