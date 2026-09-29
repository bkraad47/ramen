---
name: backup-restore
description: Take a versioned JSON backup of Ramen console state (zones, users, groups, environments, workers, config) to a local path or the groups bucket, verify it, and restore it after a bad change or into a new console. Use before risky operations and for disaster recovery.
---

# Backup and restore

Console state lives in Firestore/DynamoDB; backups are JSON exports tagged with the release version
(`docs/CONTRACTS.md` §4a, F7.2). They **exclude secret values and password hashes**. Group code is in git + the
bucket and needs no backup. Super-admin `rmn_` key required: `H='X-Ramen-Api-Key: $RMN'`, `U=$RAMEN_CONSOLE_URL/api/v1`.

## Backup
1. `curl -sk -H "$H" -H 'Content-Type: application/json' -X POST $U/backups -d '{"target":"bucket"}'` (`"local"` writes under `RAMEN_BACKUP_ROOT` on the console pod/container — only useful locally). → `201 {id, release_version}`.
2. Download a copy: `curl -sk -H "$H" $U/backups/<id>/download -o ramen-backup-<release>-<date>.json`; store it where the team keeps recovery material (not in the repo).
3. Check the file: valid JSON, `release_version` matches `curl -sk $CONSOLE/readyz | jq .version`, contains `zones`, `users`, `groups`, `environments`, and `grep -c 'rmk_\|rmn_\|password' file` is 0.
4. Note the id in the change ticket.

## Restore
Flags (all default false, CONTRACTS §13.1): `dry_run` plans without writing, `prune` deletes what the backup does
not contain, `reconcile` re-applies the restored zones, `force` accepts a backup from a newer release.
1. Confirm the target console runs the **same or newer** release than `release_version`. A newer backup is refused
   with 409; `force:true` is a human decision, not a default.
2. Back up the current state first (step Backup) and quote both ids in the ticket.
3. Plan it: `curl -sk -H "$H" -H 'Content-Type: application/json' -X POST $U/backups/<id>/restore -d '{"dry_run":true}'`
   → `{restored:{col:{created,updated,unchanged}}, extra, warnings}`. Read `extra` before considering `prune`: those
   are the rows a prune would delete.
4. Restore: `… -d '{"reconcile":true}'` (merge and re-apply the zones), or `… -d '{"prune":true,"reconcile":true}'`
   when the intent is "make it look exactly like the backup". `config` is never pruned and the key or account running
   the restore is never deleted — the response says so in `warnings`.
5. Everyone else is signed out (every restored user's session epoch is bumped); the caller keeps their session. Users
   the store had lost come back `login_disabled`: re-invite them or `POST $U/users/<id>/password`. The bootstrap super
   admin is re-applied from env on the next console start.
6. `orphans` in the response lists namespaces the cloud still runs that the backup never knew about. Nothing deletes
   them for you: check each one, then `DELETE $U/groups/<g>` if it should not exist.
7. Re-add secret values (if `RAMEN_SECRETS_BACKEND=store`), generate `rmn_` keys again, and deploy every environment
   that changed. To restore into a **different** console, upload the JSON to `<bucket>/_backups/` under the same name
   first (or copy it into `RAMEN_BACKUP_ROOT` with `target:"local"`).

## Validate
(read-only sub-agent; viewer or super-admin `rmn_` key; the backup file path)
- V1 Backup file parses as JSON, has keys `release_version`, `zones`, `users`, `groups`, `environments`; no string starts with `rmk_`/`rmn_`; no `password_hash`/`value` fields.
- V2 `GET $U/audit` contains `backup.create` (and `backup.restore` after a restore) with `ok:true`.
- V3 After restore: `GET $U/groups` lists every group in the file; `GET $U/zones` every zone; `GET $U/groups/<g>/environments` every environment with the same `zones` lists.
- V4 After restore + deploy: `GET $U/groups/<g>/zones/<z>/workers` → ≥1 live worker; one `tools/list` through the LB with a fresh `rmk_` key → 200.
- V5 A `dry_run` restore of the same backup now reports `created: 0` for every collection, and `extra` holds only what was deliberately created after it.

## Boundaries
- Restore only on explicit request; it overwrites current state. Take a backup of the current state first (step Backup) and quote both ids.
- Never try to extract secret values or hashes from the store to "complete" a backup.
- If the backup's `release_version` is newer than the console's, stop and ask; never pass `force` on your own.
- Never pass `prune` without showing the human the `dry_run` `extra` list first — it deletes groups, zones,
  environments, workers and users.
