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
1. Confirm the target console runs the **same or newer** release than `release_version`; restores across major changes need a human OK.
2. Stop writers: tell admins, or set `auth.password_login:false` temporarily (`PUT $U/config/auth`) if the team agrees.
3. `POST $U/backups/<id>/restore` (backup must exist on that console; to restore into a new console, upload the JSON to `<bucket>/_backups/` with the same name first, or use `target:"local"` and copy it to `RAMEN_BACKUP_ROOT`).
4. Users are restored without password hashes: the bootstrap super admin is re-applied from env on the next console start (`kubectl -n ramen-system rollout restart deploy/console`); other users log in via OAuth/magic link or get a password reset (`POST $U/users/<id>/password`).
5. Re-add secret values (if `RAMEN_SECRETS_BACKEND=store`), re-mint `rmn_` keys, then deploy every environment so zones are reconciled (`POST $U/refresh` first on GCP/AWS to re-discover namespaces).

## Validate
(read-only sub-agent; viewer or super-admin `rmn_` key; the backup file path)
- V1 Backup file parses as JSON, has keys `release_version`, `zones`, `users`, `groups`, `environments`; no string starts with `rmk_`/`rmn_`; no `password_hash`/`value` fields.
- V2 `GET $U/audit` contains `backup.create` (and `backup.restore` after a restore) with `ok:true`.
- V3 After restore: `GET $U/groups` lists every group in the file; `GET $U/zones` every zone; `GET $U/groups/<g>/environments` every environment with the same `zones` lists.
- V4 After restore + deploy: `GET $U/groups/<g>/zones/<z>/workers` → ≥1 live worker; one `tools/list` through the LB with a fresh `rmk_` key → 200.

## Boundaries
- Restore only on explicit request; it overwrites current state. Take a backup of the current state first (step Backup) and quote both ids.
- Never try to extract secret values or hashes from the store to "complete" a backup.
- If the backup's `release_version` is newer than the console's, stop and ask.
