# Secrets

## Model
A secret is `{name, value, env?, zone?}` under a group. Scope rules: a secret with no `env`/`zone` applies to every
environment and zone of the group; with `env` only to that environment; with `env` + `zone` only to that zone of
that environment. On deploy, the console resolves the secrets that match the target env + zone and injects them
into the workers as `RAMEN_SECRET_<GROUP>__<NAME>` (upper-cased). More specific scopes win on a name clash.

Values are **never** returned: not by the API, not on the Secrets page, not in backups, not in logs. Admins add
and delete; viewers see names only.

<figure markdown>
![Secrets page](../img/secrets.png){ .ramen-shot }
</figure>

## Add
=== "Console"
    Secrets → choose the group → name, value, optional env and zone → **Add**.
=== "API"
    ```sh
    curl -sk -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' \
      -X POST https://<console>/api/v1/groups/demo/secrets -d '{"name":"OPENAI_API_KEY","value":"sk-…","env":"dev","zone":"a"}'
    # 201 {"id":"…","name":"OPENAI_API_KEY"}
    ```
Then **deploy** the environment; secrets reach workers only through a deploy.

## Use in tool code
```python
def call_llm(prompt):
    key = "{{$demo.OPENAI_API_KEY}}"      # substituted by the runtime before the call
    ...
```
Any string argument that contains `{{$<group>.<NAME>}}` (nested too) is substituted before your function runs, and
code can call `ramen_runtime.secrets.resolve(text)` explicitly. Unknown references stay literal. Values are redacted
from exception messages and never logged.

## Backends
| `RAMEN_SECRETS_BACKEND` | Where values live | Notes |
|---|---|---|
| `store` (default, local) | the console store, Fernet-encrypted with `RAMEN_FERNET_KEY` | |
| `gcp` | Secret Manager `ramen-<group>-<env|all>-<zone|all>-<NAME>` (labels group/env/zone) | store keeps name + `sm://` ref; worker GSA has `secretAccessor` on `ramen-<group>-*` |
| `aws` (untested) | Secrets Manager `ramen/<group>/<env|all>/<zone|all>/<NAME>` (tags) | store keeps `asm://` ref |

`rmk_` MCP keys are stored through the same backend (`kind: mcp_key`).

## Special names
- `GITHUB_TOKEN`: used by the console to clone a private group repo (env- or group-scoped). Alternative: the
  group's *fallback GitHub token* field (stored encrypted, never shown).

## Rotation
Add the new value under the same name (delete + add), deploy. Workers pick it up on `/admin/reload` during the
canary step. The [rotate-keys skill](../wiki/skills.md) covers keys and the Fernet key.
