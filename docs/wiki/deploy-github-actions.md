# Deploy from GitHub Actions

A push to the group's repo changes nothing by itself: the workers serve what the last **deploy** synced. This page
wires the two together. A GitHub Actions job holds a console API key, asks the console to deploy the group's
environment when `mcp/` changes on `main`, and waits for the deploy job to finish, failing the run when the deploy
fails. The [demo repo](https://github.com/bkraad47/ramen-demo-mcp-group) carries the same workflow.

## 1. Create an API key for CI

Use a **devops** key (`rmn_`) with the least it needs: role **Group Admin**, one group. A group admin of `demo` may
deploy `demo` and nothing else. Only a super admin can create API keys.

**On the API keys page**: Name `ci-demo`, Client type *Devops — console API*, Role *Group Admin*, pick the group
`demo` and press **Add**, then **Generate key**. Name the group explicitly: a key with no group takes the creator's
groups, and a super admin has none of their own, so that key could deploy nothing (`403`). The key appears once,
under the form.

**Or through the API**, as a super admin (session plus CSRF header, or an existing super-admin key):

```sh
CONSOLE=https://<console>            # the base URI, path prefix included
curl -s -c c.txt -X POST $CONSOLE/login --data-urlencode email=admin@ramen.local --data-urlencode password='...' -o /dev/null
CSRF="X-Ramen-CSRF: $(awk '$6=="ramen_csrf"{print $7}' c.txt)"
curl -s -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST $CONSOLE/api/v1/api-keys \
  -d '{"name":"ci-demo","role":"group_admin","groups":["demo"]}'
# 201 {"name":"ci-demo","role":"group_admin","groups":["demo"],"client_type":"devops",...,"id":"41ec71772452","key":"rmn_..."}
```

Store it in the repo, never in a file:

```sh
gh secret set RAMEN_API_KEY --repo <owner>/<group repo>          # paste the rmn_ key when asked
gh variable set RAMEN_URL --repo <owner>/<group repo> --body https://<console>
gh variable set RAMEN_GROUP --repo <owner>/<group repo> --body demo
gh variable set RAMEN_ENV --repo <owner>/<group repo> --body prod
gh secret set RAMEN_CA_PEM --repo <owner>/<group repo> < ramen-lb.pem   # only for a self-signed console
```

`RAMEN_URL` is the address the console is reached at: its [base URI](configuration.md#the-public-address-base-uri),
path prefix included (`https://ops.example.com/ramen`). The job calls `$RAMEN_URL/api/v1/...`, which a prefix
proxy forwards to the console like any other path. `RAMEN_CA_PEM` is for a console behind a self-signed
certificate, such as the one the [AWS guide](deploy-aws.md#3-the-console) exports from ACM; a GCP console has a
publicly trusted certificate and needs none.

## 2. How code reaches a group

The group's git repo is the source, and the console is the only thing that reads it. A deploy job:

1. clones or fetches the repo at the **environment's ref** (or, if the environment has none, the group's), on the
   console, and copies it into the group's bucket prefix;
2. writes each zone's config, rolls a canary, reloads and smoke-tests it, then rolls the stable pods
   ([Deploying](groups-zones.md#deploying)).

So the workflow needs no checkout and no GitHub token: it only tells the console to deploy. The deploy takes the
tip of the ref **when the job syncs**, not the commit that triggered the run; a second push during a deploy is
picked up by the next one. An environment pinned to a tag deploys that tag whatever `main` holds.

A **private** repo needs the console to authenticate, in this order of preference:

| Where | What |
|---|---|
| Group page, *GitHub App installation id* | With the GitHub App configured once on the Config page, the console mints a short-lived installation token per deploy. Nothing long-lived is stored for the group |
| Secrets, `GITHUB_TOKEN` | A fine-grained token with *Contents: read* on the repo, as a group secret or scoped to the environment |
| Group page, *Fallback GitHub token* | The same kind of token stored on the group, used when there is no `GITHUB_TOKEN` secret |

The token goes to git as an HTTP header for that one command, never into the remote URL, the clone's config or
the job log.

## 3. The workflow

Commit this as `.github/workflows/ramen-deploy.yml` in the group's repo. It runs on a push to `main` that touches
`mcp/`, and by hand from the Actions tab with a *canary* switch. Without `RAMEN_URL` and `RAMEN_API_KEY` it
prints a notice and does nothing, so forks and fresh clones stay green.

```yaml title=".github/workflows/ramen-deploy.yml"
# Deploys this group to a Ramen console when mcp/ changes on main, and waits for the deploy job.
# Settings (repo → Settings → Secrets and variables → Actions):
#   secret   RAMEN_API_KEY   rmn_ key, role Group Admin, groups [<this group>]           (required)
#   variable RAMEN_URL       the console's address, the base URI with any path prefix     (required)
#   variable RAMEN_GROUP     the group's name in Ramen (default demo)
#   variable RAMEN_ENV       the environment to deploy (default prod)
#   secret   RAMEN_CA_PEM    PEM of a self-signed console certificate                     (optional)
# Without RAMEN_URL and RAMEN_API_KEY the job does nothing and says so.
name: ramen-deploy

on:
  push:
    branches: [main]
    paths: ["mcp/**"]
  workflow_dispatch:
    inputs:
      canary:
        description: Roll a canary and smoke-test it before the stable pods
        type: boolean
        default: true

permissions: {} # the console clones the repo itself; this job needs no GitHub token

concurrency:
  group: ramen-deploy-${{ vars.RAMEN_GROUP || 'demo' }}-${{ vars.RAMEN_ENV || 'prod' }}
  cancel-in-progress: false

jobs:
  deploy:
    runs-on: ubuntu-latest
    timeout-minutes: 20
    env:
      RAMEN_URL: ${{ vars.RAMEN_URL }}
      RAMEN_API_KEY: ${{ secrets.RAMEN_API_KEY }}
      RAMEN_CA_PEM: ${{ secrets.RAMEN_CA_PEM }}
      RAMEN_GROUP: ${{ vars.RAMEN_GROUP || 'demo' }}
      RAMEN_ENV: ${{ vars.RAMEN_ENV || 'prod' }}
      CANARY: ${{ github.event_name != 'workflow_dispatch' || inputs.canary }}
      DEPLOY_TIMEOUT_SECS: "900"
    steps:
      - name: Not configured
        if: env.RAMEN_URL == '' || env.RAMEN_API_KEY == ''
        run: echo "::notice::RAMEN_URL or RAMEN_API_KEY is not set; nothing deployed."

      - name: Deploy and wait for the job
        if: env.RAMEN_URL != '' && env.RAMEN_API_KEY != ''
        run: |
          set -euo pipefail
          U="${RAMEN_URL%/}/api/v1"
          TLS=()
          if [ -n "${RAMEN_CA_PEM:-}" ]; then
            CA="${RUNNER_TEMP:-/tmp}/ramen-ca.pem"; printf '%s\n' "$RAMEN_CA_PEM" > "$CA"; TLS=(--cacert "$CA")
          fi
          OUT="${RUNNER_TEMP:-/tmp}/ramen-out.json"
          api() { # METHOD PATH [JSON] -> HTTP status (000: no answer); body in $OUT
            rm -f "$OUT"
            curl -sS ${TLS[@]+"${TLS[@]}"} --max-time 30 -o "$OUT" -w '%{http_code}' -X "$1" "$U$2" \
              -H "X-Ramen-Api-Key: $RAMEN_API_KEY" -H 'Content-Type: application/json' ${3:+-d "$3"} || true
          }

          code=$(api POST "/groups/$RAMEN_GROUP/environments/$RAMEN_ENV/deploy" "{\"canary\": $CANARY}")
          case "$code" in
            202) ;;
            000) echo "::error::no answer from $U (network, or TLS: set RAMEN_CA_PEM for a self-signed console)"; exit 1 ;;
            401) echo "::error::401: the API key is wrong, revoked or missing"; exit 1 ;;
            403) echo "::error::403: the key may not deploy $RAMEN_GROUP (role or groups): $(cat "$OUT")"; exit 1 ;;
            *) echo "::error::deploy refused with HTTP $code: $(cat "$OUT" 2>/dev/null)"; exit 1 ;;
          esac
          JOB=$(jq -r .id "$OUT")
          echo "deploy job $JOB: $RAMEN_GROUP/$RAMEN_ENV, canary=$CANARY"

          deadline=$((SECONDS + DEPLOY_TIMEOUT_SECS))
          while :; do
            sleep 5
            code=$(api GET "/jobs/$JOB")
            status=unknown
            if [ "$code" = 200 ]; then status=$(jq -r '.status // "unknown"' "$OUT" 2>/dev/null || echo unknown); fi
            if [ "$code" = 404 ]; then
              echo "::error::job $JOB is gone: jobs live in the console's memory, so it restarted. See the group page."
              exit 1
            fi
            case "$status" in
              ok)
                jq -r '.log[]' "$OUT"; echo "deploy ok"; exit 0 ;;
              error)
                jq -r '.log[]' "$OUT"; echo "::error::deploy failed: $(jq -r '.error' "$OUT")"; exit 1 ;;
            esac
            # running, or a load-balancer blip (503, non-JSON) while the console restarts: keep polling
            if [ "$SECONDS" -ge "$deadline" ]; then
              echo "::error::no result after ${DEPLOY_TIMEOUT_SECS}s (last HTTP $code, status $status)"; exit 1
            fi
          done
```

What it does, in order:

- `POST $RAMEN_URL/api/v1/groups/<group>/environments/<env>/deploy` with `{"canary": true}` and the
  `X-Ramen-Api-Key` header. `202` carries the job id.
- `GET $RAMEN_URL/api/v1/jobs/<id>` every five seconds until `status` is `ok` or `error`, then prints the job log.
  `error` fails the run with the job's error line, and so does no answer within `DEPLOY_TIMEOUT_SECS`.
- A `503` or a non-JSON answer while it polls (a load balancer blip during a console restart) is ridden out. A
  `404` for the job is not: jobs live in the console's memory, so the console restarted (or another console replica
  answered) and the outcome is on the group page's environment row.
- `concurrency` queues a second run for the same environment behind the first instead of starting two deploys.

A successful run ends like this (the local compose stack, zone `local`):

```text
deploy job f6b28cac554645c7b3115046906aaaab: demo/dev, canary=true
2026-10-04T14:25:27+00:00 syncing repo https://github.com/bkraad47/ramen-demo-mcp-group
2026-10-04T14:25:29+00:00 zone local: deploying (canary=on)
deploy ok
```

A failed deploy prints its log and fails the run, here an environment whose ref does not exist:

```text
deploy job 7fc641bf16774099823ef3c0dc51cbc0: demo/cifail, canary=true
2026-10-04T14:26:40+00:00 syncing repo https://github.com/bkraad47/ramen-demo-mcp-group
2026-10-04T14:26:41+00:00 zone local: aborting, canary scaled to 0
::error::deploy failed: RuntimeError: git failed: fatal: couldn't find remote ref no-such-branch
```

Both runs are the workflow's own `run:` block, taken from the YAML and executed against the compose stack with a
key minted as in step 1; so were a run through a prefix proxy (`RAMEN_URL=http://localhost:9090/ramen` with the
base URI set to it) and every row of the table below except `422`. The workflow passes `actionlint`.

## 4. Rotate and revoke

- **Rotate**: create a second key, `gh secret set RAMEN_API_KEY` with it, run the workflow once by hand, then
  **Revoke** the old key on the API keys page (or `DELETE /api/v1/api-keys/<id>`). A revoked key fails at once.
- **Revoke on a leak** first, then rotate. The audit log lists every deploy the key started, as `deploy.start`
  and `deploy`, under the key's name.

| Answer | Meaning |
|---|---|
| `401` | The key is wrong, revoked, or not sent. Check the secret's name and that it holds the whole `rmn_...` value |
| `403` with *Agent key cannot call the console API* | An `rmk_` key was stored; CI needs a devops `rmn_` key |
| `403` otherwise | The key's role or groups do not cover this group: it needs Group Admin on it |
| `404` on the deploy | The group or environment name is wrong (`RAMEN_GROUP`, `RAMEN_ENV`), or `RAMEN_URL` lacks the path prefix |
| `422` | The environment has no such zone, or the body is malformed |
| No answer (`000`, `curl: (60)` above it) | A self-signed console without `RAMEN_CA_PEM`, or a `RAMEN_URL` the runner cannot reach |

See [API keys and the API](api-keys.md) for the rest of the API, and [Groups, zones and regions](groups-zones.md)
for what a deploy does in each zone.
