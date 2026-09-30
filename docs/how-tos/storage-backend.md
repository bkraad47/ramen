# Changing the state store

## Model
The console's own state (groups, environments, zones, workers config, secrets, API keys, audit, activity,
config, backups, worker images — everything except the per-group code, which lives in the groups bucket) sits
behind one shallow interface: `get`/`put`/`delete`/`list` per collection, keyed by id, with exact-match filters
only (no joins, no schema beyond "a JSON document"). Every backend implements the same four methods
(`console/src/ramen_console/storage/base.py`), so switching stores never changes anything above the storage
layer.

Pick the backend with `RAMEN_STORE`:

| `RAMEN_STORE` | Backend | When |
|---|---|---|
| `firestore` | Firestore Native (GCP) | default on GCP (D2) |
| `dynamodb` | DynamoDB, single table | default on AWS (D2) |
| `postgres` | Postgres, one table (`ramen_store`, `collection`/`key`/`doc jsonb`) | self-hosted, or any deployment that has Postgres but not a cloud-native DB (v0.5.5 I14) |
| `memory` | In-process dict | tests and local dev only — state is lost on restart |

`RAMEN_FERNET_KEY`, when set, wraps whichever backend in `EncryptedStore` and encrypts sensitive fields
(password hashes, secret values, session secrets, API key hashes) before they ever reach the backend — this
applies the same way regardless of `RAMEN_STORE`.

## Postgres
```sh
RAMEN_STORE=postgres
RAMEN_POSTGRES_DSN=postgresql://user:password@host:5432/ramen
```
The table is created on first use (`CREATE TABLE IF NOT EXISTS`) — no migration step. Try it locally:
```sh
docker run -d --name ramen-postgres -e POSTGRES_PASSWORD=ramen -e POSTGRES_DB=ramen -p 5432:5432 postgres:17-alpine
RAMEN_STORE=postgres RAMEN_POSTGRES_DSN=postgresql://postgres:ramen@localhost:5432/ramen \
  uv run --directory console python -m ramen_console  # or however you normally start the console locally
```
On Helm: `--set console.env.RAMEN_STORE=postgres --set console.secrets.RAMEN_POSTGRES_DSN=postgresql://...`
(the `deployment.yaml` template merges `console.env`/`console.secrets` over the provider's default, so this
overrides GCP's `firestore` default without any other chart change).

## Firestore / DynamoDB
These are the GCP/AWS defaults, already wired by `deploy/helm/ramen`'s `provider` value — see
[GCP](gcp.md#4-console-helm) and [AWS](aws.md). Nothing to change unless you are deliberately moving a
deployment off its cloud provider's native store.

## Adding another backend
Implement `Store` (`console/src/ramen_console/storage/base.py`) and register it in
`storage/__init__.py::make_store()`. `storage/postgres.py` is the shortest existing adapter to copy from — one
table, four methods, no migrations.
