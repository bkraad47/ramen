# Secrets: how they are managed and mapped at runtime

A secret is a named value under a group, optionally narrowed to an environment and a zone. The console never
shows a value back: not on the Secrets page, not in the API, not in backups, not in logs. Super admins and group
admins add the values, read the names and delete them. A viewer cannot open the Secrets page at all.

<figure markdown>
![The secrets page](../img/secrets.png){ .ramen-shot }
<figcaption>Names, scopes and nothing else.</figcaption>
</figure>

## Scope

| Scope | Applies to |
|---|---|
| group only | every environment and zone of the group |
| group + environment | that environment in every zone |
| group + environment + zone | that zone of that environment |

The most specific scope wins on a name clash. On deploy the console resolves the secrets that match the target
environment and zone.

## Add one

=== "Console"
    **Secrets → choose the group** → name, value, optional environment and zone → **Add**. The page is open to
    super admins and group admins only.

=== "API"
    ```sh
    curl -s -H "X-Ramen-Api-Key: $RMN" -H 'Content-Type: application/json' \
      -X POST https://<edge>/api/v1/groups/demo/secrets \
      -d '{"name":"OPENAI_API_KEY","value":"sk-...","env":"prod","zone":"a"}'
    ```

Then **deploy** the environment. Secrets reach workers only through a deploy.

## How a secret reaches your code

The deploy writes each resolved secret into the zone's Secret as `RAMEN_SECRET_<GROUP>__<NAME>`, upper-cased.
The runtime maps it two ways:

- **In call arguments.** Any string a client passes that contains `{{$demo.OPENAI_API_KEY}}` is substituted
  before the function runs. Nested strings too. Unknown references stay literal.
- **Through `mcp/env.yaml`.** The worker renders the file's `{{$group.NAME}}` references and exports every key to
  the runtime process at load. Code reads `os.environ["DB_URL"]`.

Values are redacted from exception messages and never logged. A reference with no secret behind it is skipped
with a warning so the rest of the file still applies.

## Where values live

| `RAMEN_SECRETS_BACKEND` | Where | What the worker identity needs |
|---|---|---|
| `store` | the console store, Fernet-encrypted with `RAMEN_FERNET_KEY` (local, kind) | nothing; the console injects the value on deploy |
| `gcp` | Secret Manager `ramen-<group>-<env or all>-<zone or all>-<NAME>` | `secretAccessor` on `ramen-<group>-*`, granted at zone creation |
| `aws` | Secrets Manager `ramen/<group>/<env or all>/<zone or all>/<NAME>` | `GetSecretValue` on `ramen/<group>/*`, granted at zone creation |

The charts set `gcp` and `aws`. MCP keys are stored through the same backend.

## Special names

`GITHUB_TOKEN` is used by the console to clone a private group repo. It can be group- or environment-scoped. The
group's *fallback GitHub token* field on the group page is the alternative.

## Rotate

Delete and add under the same name, then deploy. Workers pick the new value up during the canary reload. The
[rotate-keys skill](skills.md) covers keys and the Fernet key.
