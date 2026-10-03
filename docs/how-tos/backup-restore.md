# Back up and restore the console

The console's state — zones, groups, environments, worker records, users, configuration — lives in the console
store (Firestore on GCP, DynamoDB on AWS, Postgres where self-hosted). A backup is a JSON export of it, tagged with
the release version. **It never contains secret values, password hashes or GitHub tokens**; those stay in the
secrets backend and survive a restore untouched.

<figure markdown>
![The backups page](../img/backups.png){ .ramen-shot }
</figure>

## Create
=== "Console"
    *Backups* (super admins) → *New backup* → target `local` (the console's `RAMEN_BACKUP_ROOT`) or `bucket` (the
    groups bucket, `_backups/`). The row shows the release version the backup was taken on.
=== "API"
    ```sh
    curl -sk -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' \
      -X POST https://<edge>/api/v1/backups -d '{"target":"bucket"}'
    # 201 {"id":"…","release_version":"0.6.0",…}
    curl -sk -H "X-Ramen-Api-Key: $RMN" https://<edge>/api/v1/backups/<id>/download > ramen-backup.json
    ```

Schedule it the way you schedule anything else: a CI job with an `rmn_` key, or the
[backup-restore skill](../wiki/skills.md) for an agent operator.

## Restore
1. **Preview** first: *Preview restore* (API: `{"dry_run": true}`) prints what would be created, updated, left
   alone, and what the live store has that the backup does not. Nothing is written.
2. **Restore**: merges each backup document over the live one. Fields the backup never contained (password
   hashes, secret refs) are kept from the live document, so nobody loses a password hash or a secret.
3. **Restore and prune** also deletes what the backup does not contain — groups, zones, environments, workers,
   users. `config` is never pruned and the account running the restore is never deleted.
4. **Reconcile** (`{"reconcile": true}`) re-applies every restored zone so counts, sizes and image pins take effect
   in the cluster. If the cloud is still running a namespace the backup never knew about, it is listed as an
   `orphan`: reported, never deleted.

| Flag | Default | Meaning |
|---|---|---|
| `dry_run` | false | plan only |
| `prune` | false | delete what the backup lacks |
| `reconcile` | false | re-apply restored zones in the cluster |
| `force` | false | accept a backup taken on a **newer** release (otherwise 409) |

## What a restore does to people
- Every restored user's session epoch is bumped: everyone else is signed out at once, so if a restore lowers
  someone's role, an already-open session cannot keep the old one.
- A user the store no longer had comes back with `login_disabled` (the backup holds no hash) until a password reset
  by an admin or an SSO sign-in.
- Per-group roles are restored as stored; documents from before 0.5.95 are migrated on the next start.

## Moving between stores or clouds
Take a backup, bring the new console up ([Configuration](configuration.md) for the store settings), restore with
`reconcile`, then re-add secrets (they are not in the backup) and deploy each environment. The
[storage backend](storage-backend.md) page covers switching stores on the same console.
