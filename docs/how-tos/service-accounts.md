# Service accounts and secrets management (GCP and AWS)

Every zone of a group runs as **its own cloud identity**, and that identity holds only what was approved for it.
Secrets are a separate thing: named values the console keeps for the group and hands to the workers on deploy.
This page is both halves, for GCP and AWS, with the console steps and what happens in the cloud.

## 1. The identity of a zone
| | GCP | AWS |
|---|---|---|
| Identity | Service account `ramen-<group>-<zone>@<project>.iam.gserviceaccount.com`, bound to the namespace's Kubernetes service account `worker` through Workload Identity | IAM role `ramen-<group>-<zone>` assumed by the namespace's `worker` service account through IRSA |
| Created | On the group page → *Zone actions → Create service account*, or automatically on the first deploy to the zone | The same |
| Baseline | Read the group's prefix of the groups bucket (`roles/storage.objectViewer` with a condition on `objects/<group>/`) and the group's secrets (`roles/secretmanager.secretAccessor` on `ramen-<group>-*`) | Read `s3://<groups bucket>/<group>/*` and `secretsmanager:…:secret:ramen/<group>/*` |
| Removed | With the zone (deleting the zone, dropping it from every environment, deleting the environment — 0.6.0) or with the group | The same |

The console's own identity (`ramen-console@<project>` / the console IRSA role) can administer only the groups
bucket, the group secrets and the per-group identities — never the project at large. Project-wide roles need the
optional `console_project_iam = true` Terraform variable.

## 2. Granting a permission
The catalogue is cloud-neutral; the console maps each entry to roles or actions:

| Permission | GCP | AWS |
|---|---|---|
| `bucket.read` / `bucket.write` | `roles/storage.objectViewer` / `objectUser` on the groups bucket (group prefix) or on named buckets | `s3:GetObject`, `s3:ListBucket` / `PutObject`, `DeleteObject` on the group prefix or named buckets |
| `secrets.read` | `roles/secretmanager.secretAccessor` on the group's secrets or named ones | `secretsmanager:GetSecretValue` on the group's path or named secrets |
| `logs.write`, `metrics.write`, `pubsub.publish`, `queue.consume`, `datastore.user`, `kms.decrypt`, `ai.user` | project-wide roles (`roles/logging.logWriter`, …) — need `console_project_iam` | account-wide actions (`logs:*`, `cloudwatch:PutMetricData`, …) |

The flow, in order:

1. **A super admin sets the rules** on the Config page: `Deny kms.*` style rules that gate what anyone may request
   (deny wins; when allow rules exist a permission must match one). A group admin can add stricter *restrictions*
   on the group page; a restriction that allows what a super-admin rule denies is refused.
2. **A group admin requests** on the group page → *Service-account permissions*: a zone, a permission and a
   **scope**. Leave the scope empty or set `*` for the group's own area, the baseline above. Name resources
   (`bucket-a, bucket-b`, or a secret name) to bind the permission to those alone. On GCP, naming another bucket
   needs the console's identity granted `roles/storage.admin` on that bucket first; otherwise the approval fails
   with a message naming the missing binding.
3. **Another admin of the group, or a super admin, approves** (never the requester — 0.5.95). The console binds the
   roles and records the permission and its scope on the zone's worker record (`sa_permissions`, `sa_scopes`).
4. **Revoke** takes it back: the group page's *Revoke permission* on the badge, or the request's *Revoke*; the
   console unbinds exactly what it bound and keeps the baseline.

Everything is audited (`permission.request`, `permission.approve`, `permission.revoke`) with the user, group and
permission as tags.

## 3. Secrets
A secret is `{name, value, env?, zone?}` under a group; more specific scopes win on a name clash. Values are
never returned by the console or the API, never in backups or logs.

| `RAMEN_SECRETS_BACKEND` | Where the value lives | What the worker identity needs |
|---|---|---|
| `store` | the console store, Fernet-encrypted with `RAMEN_FERNET_KEY` (local, kind) | nothing — the console injects the value on deploy |
| `gcp` | Secret Manager `ramen-<group>-<env\|all>-<zone\|all>-<NAME>` | `secretAccessor` on `ramen-<group>-*` (baseline) |
| `aws` | Secrets Manager `ramen/<group>/<env\|all>/<zone\|all>/<NAME>` | `GetSecretValue` on `ramen/<group>/*` (baseline) |

- **Manage**: the *Secrets* page (super and group admins; viewers do not see secrets) or
  `POST/DELETE /api/v1/groups/{g}/secrets`. Then **deploy**: secrets reach workers only through a deploy, as
  `RAMEN_SECRET_<GROUP>__<NAME>` on the pods.
- **Use**: `{{$group.NAME}}` in call arguments, or keys in `mcp/env.yaml` rendered at load. See
  [Write and develop an MCP repo](develop-mcp-repo.md).
- **Rotate**: delete and add under the same name, then deploy; workers pick it up during the canary reload.

`GITHUB_TOKEN` is the one special name: the console uses it to clone a private group repo.

<figure markdown>
![Secrets page](../img/secrets.png){ .ramen-shot }
</figure>

## 4. Checking what a zone holds
- Group page → the *Service-account permissions* table lists each zone's identity and approved permissions with
  their scopes.
- `GET /api/v1/groups/{g}/zones/{z}/workers` → `service_account`, `sa_permissions`, `sa_scopes`.
- GCP: `gcloud projects get-iam-policy`, `gcloud storage buckets get-iam-policy gs://<bucket>`; AWS:
  `aws iam get-role-policy --role-name ramen-<group>-<zone> --policy-name ramen-sa-permissions`.
