# The console, page by page

The console is the whole management surface: everything below is also an API call
([DevOps with the API and keys](devops-api.md)), so nothing here is a dead end. Sign in at `https://<console>/`
with the bootstrap super admin (`RAMEN_ADMIN_EMAIL` / `RAMEN_ADMIN_PASSWORD`) or your identity provider.

What each role sees: a **Viewer** reads their groups; a **Group Admin** changes their groups; a **Super Admin** sees
Users, API keys, Backups and Config as well. The sidebar names the signed-in address and the role, and the release
version sits at the bottom — it comes from the installed package, so it is the version actually running.

## Dashboard

<figure markdown>
![The dashboard's load grid](../img/dashboard.png){ .ramen-shot }
</figure>

A grid of zones by groups. Each cell names its load — `low`, `even`, `high` or `down` — and colours it; the words
carry the meaning, so the grid reads without colour.

The grid refreshes when the page opens and **every minute** after that. `Auto refresh: on` turns the polling off and
back on; leave it off while reading a busy cluster, and it stays off until you turn it back on or reload.
`Refresh discovery` (super admin) is a different thing: it re-reads namespaces, service accounts and workers from the
cloud and reconciles the store.

## Groups
One page per group: environments, deploy jobs, zones and workers, the packages each zone serves, service-account
permissions, MCP keys, the group's worker image and its service-account restrictions.

Every per-zone action — scale workers, IP rules, create service account, rebalance, view logs — is in one `Actions`
group, all the same width. Deploying is per environment, canary first.

**Worker image.** A group runs the release worker image unless one is pinned for it. `Record image` stores a registry
reference you have already pushed (`make build-worker GROUP=… && make push-worker GROUP=…`); the pin reaches the
zones on the next deploy. The history keeps every reference recorded, so `Recall` puts a group back on an older build
and `Use the release image` drops the pin entirely.

**Service-account permissions.** Each granted permission is a badge with its own `Revoke permission` button. Granting
goes through a request a super admin approves; revoking calls the cloud straight away, so the mapped role is unbound.
Two permissions are special and the page cannot change that: `bucket.read` and `secrets.read` map onto the roles a
worker needs to load its own code and secrets, so revoking them clears the grant but leaves that baseline access —
see [Security](security.md#granting-and-taking-back-v041).

## Environments
Every environment across the groups you can see, with the last deploy flattened into the row: the outcome, when it
ran, and the error if it failed. `Open group` goes to where deploys are started.

## Zones and workers, Secrets, Logs
Zones lists the zones and the worker counts per group. Secrets shows names only, per environment and zone — no value
is ever returned to a browser. Logs is two panes: entries newest first on the left, the selected entry's body on the
right, with a worker filter above; every row names the consumer, the time, the method and whether the call succeeded.

## Users
Create a user with a role and groups; each row edits the role and groups (`Save`) and deletes the account
(`Delete user`, to the right of Save, and it asks first). Permission requests from viewers and group admins are
listed below with `Approve`, `Deny` and, once approved, `Revoke` — revoking ends that user's open sessions
immediately. `Change my password` at the bottom applies the 12-character rule to everyone, super admins included.

## API keys

<figure markdown>
![Generating an API key](../img/api-keys.png){ .ramen-shot }
</figure>

`Generate key` mints a key that is shown **once**. Two types, and each is refused by the other side:

| Type | Prefix | Used by |
|---|---|---|
| Devops | `rmn_` | scripts and CI against `/api/v1/*` |
| Agent | `rmk_` | a language model against a worker, through the bridge |

Pick the groups from the dropdown and press `Add` for each one — the chosen groups appear as chips you can remove.
Add none and the key inherits your own groups. A key can never outrank its creator.

## Audit

<figure markdown>
![The audit page with its search and outcome filter](../img/audit.png){ .ramen-shot }
</figure>

The newest 100 entries, with a search box that filters across every column and an outcome filter for succeeded or
failed; both work on what is already on the page, so they are instant. `Load 500` fetches more, and CSV and JSON
downloads give the whole log. Every mutating request is here, failed sign-ins included.

## Backups

<figure markdown>
![The backups page](../img/backups.png){ .ramen-shot }
</figure>

`Create backup` writes a JSON export tagged with the release version — no secret values, no password hashes — to a
local path or the groups bucket. Each row then offers four equally sized actions:

| Action | What it does |
|---|---|
| Download | Fetches the file. |
| Preview restore | Shows exactly what a restore would create, update and delete. Writes nothing. |
| Restore | Merges the backup over the current state and re-applies the restored zones. |
| Restore and prune | The same, and deletes what the backup does not contain. |

Preview first. A restore signs every other session out, and a user the store had lost comes back unable to sign in
until a password reset — the result panel says so. Details: [DevOps with the API and keys](devops-api.md#backups).

## Config

<figure markdown>
![The config page](../img/config.png){ .ramen-shot }
</figure>

The config file in use and a hot reload, the authentication toggles, the service-account permission catalogue, the
effective `RAMEN_*` environment (secrets masked), and the **super-admin service-account rules**: pick `Allow` or
`Deny` and a permission from the catalogue — or type a pattern such as `kms.*` — then `Add rule`. Each rule has
`Remove`. Group admins cannot add restrictions that clash with these; a clash is an error, not a silent merge.
