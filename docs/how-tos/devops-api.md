# DevOps with the API and keys

Everything the console does is a JSON route under `/api/v1` ([contract §4a](../CONTRACTS.md)). OpenAPI UI at
`/api/docs`. Authenticate with a session cookie (`POST /login`) or an **`rmn_` API key** in `X-Ramen-Api-Key`.
The console API stays HTTP in 0.3.1; what changed is how the console talks to **workers**: every worker call
(`Admin/Reload`, `Admin/Metrics`, `Health/Check`, the `tools/list` smoke) is gRPC now ([§11](../CONTRACTS.md)).

## Generate an API key
API Keys page → name, role, groups → **Create** (shown once), or:
```sh
curl -sk -c c.txt -X POST https://<console>/login -d email=admin@ramen.local -d password='…' -o /dev/null
CSRF="X-Ramen-CSRF: $(awk '$6=="ramen_csrf"{print $7}' c.txt)"   # cookie sessions must echo the CSRF token
curl -sk -b c.txt -H "$CSRF" -H 'Content-Type: application/json' -X POST https://<console>/api/v1/api-keys \
  -d '{"name":"ci-deploy","role":"group_admin","groups":["demo"]}'
# 201 {"id":"…","key":"rmn_…"}
export RMN=rmn_…
```
A key can never be wider than its creator. Keep `rmn_` keys in CI secrets; they are for the **console** only —
MCP clients need `rmk_` keys (see [local quickstart](local-quickstart.md#rmk_-vs-rmn_-two-different-keys)).

<figure markdown>
![API keys](../img/api-keys.png){ .ramen-shot }
</figure>

## Cheat sheet
`C='curl -sk -H "X-Ramen-Api-Key: $RMN" -H "Content-Type: application/json"'`, `U=https://<console>/api/v1`.

| Task | Call |
|---|---|
| Who am I | `GET $U/me` |
| Users (super admin) | `POST $U/users {email,password,role,groups}` · `GET/DELETE $U/users/{id}` · `POST $U/users/{id}/password` |
| Groups | `POST $U/groups {name,repo_url,ref}` · `PUT $U/groups/{g}` · `DELETE $U/groups/{g}` (destroys infra) |
| Zones (super admin) | `POST $U/zones {name,provider,region}` |
| Environments | `POST $U/groups/{g}/environments {name,ref,zones}` · `PUT/DELETE …/environments/{e}` |
| Deploy | `POST $U/groups/{g}/environments/{e}/deploy {canary,zone?}` → 202 `{id}`; `GET $U/jobs/{id}` until `ok`/`error` (job log shows `health`, `reload`, `smoke` steps over gRPC) |
| Verbose logging | `POST …/environments/{e}/verbose {verbose}` |
| Block tools (v0.3.0) | `PUT …/environments/{e}/blocked {blocked:[names]}` then deploy |
| Workers | `GET $U/groups/{g}/zones/{z}/workers` (live rows from `Admin/Metrics` + `Health/Check`) · `PUT … {count,size?,allowed_sizes?}` |
| Rebalance / IP rules | `POST …/zones/{z}/rebalance` · `PUT …/zones/{z}/ip-rules {cidrs}` |
| Service account | `POST …/zones/{z}/service-account` (super admin) · `PUT $U/groups/{g}/sa-restrictions {rules}` |
| Permission requests | `POST $U/requests {group,zone,permission}` · `GET $U/requests` · `POST $U/requests/{id}/approve` |
| Secrets | `POST $U/groups/{g}/secrets {name,value,env?,zone?}` · `GET` (names) · `DELETE …/secrets/{id}` |
| MCP keys | `POST $U/groups/{g}/mcp-keys {name}` → `{key:"rmk_…"}` · `DELETE …/mcp-keys/{id}` |
| API keys | `POST $U/api-keys {name,role?,groups?}` · `DELETE $U/api-keys/{id}` |
| Logs | `GET $U/logs?group=&zone=&worker=&tail=500&download=1` (text/plain) |
| Audit / dashboard | `GET $U/audit` · `GET $U/dashboard` |
| Backups (super admin) | `POST $U/backups {target:"local"|"bucket"}` → `{id,release_version}` · `GET $U/backups/{id}/download` · `POST $U/backups/{id}/restore` |
| Config (super admin) | `GET $U/config` · `POST $U/config/reload` · `GET|PUT $U/config/sa-rules` · `PUT $U/config/auth` · `POST $U/refresh` |

## A CI deploy job
```sh
JOB=$(eval $C -X POST $U/groups/demo/environments/prod/deploy -d "'{\"canary\":true}'" | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
for i in $(seq 1 150); do
  S=$(eval $C $U/jobs/$JOB | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])')
  [ "$S" = ok ] && echo PASS && exit 0; [ "$S" = error ] && { eval $C $U/jobs/$JOB; exit 1; }; sleep 2
done; echo timeout; exit 1
```

## Talking to a worker directly (gRPC)
The console is the normal path, but the worker's admin surface is reachable with any gRPC client from inside
`RAMEN_ADMIN_CIDRS` (locally `localhost:8080`; in a cluster `kubectl -n ramen-<g>-<z> port-forward svc/worker 8080`).
Run from the repo root so `-proto` resolves:
```sh
grpcurl -plaintext -H "x-ramen-admin-key: $RAMEN_ADMIN_KEY" -import-path proto -proto ramen/v1/admin.proto \
  localhost:8080 ramen.v1.Admin/Metrics | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["json"]).decode())'
# {"inflight":0,"total":12,"errors":0,"load":"low","sidecar_alive":true,"loaded_at":"…","packages":{…}}
grpcurl -plaintext -H "x-ramen-admin-key: $RAMEN_ADMIN_KEY" -import-path proto -proto ramen/v1/admin.proto \
  localhost:8080 ramen.v1.Admin/Reload                          # re-sync bucket, pip install if needed, runtime.load
grpc_health_probe -addr localhost:8080                          # SERVING / NOT_SERVING, no key needed
```
Without the key: `Unauthenticated`; from outside the admin CIDRs: `PermissionDenied`. Tool calls need an `rmk_`
key and go through `ramen.v1.Mcp/Call` — see the [local quickstart](local-quickstart.md#5-call-the-worker-raw-with-grpcurl).

## Backups
`POST $U/backups {"target":"bucket"}` writes a JSON export tagged with the release version (zones, users without
password hashes, groups, environments, workers, config; **no secrets**) to `<bucket root>/_backups` (local) or the
groups bucket. Restore with `POST $U/backups/{id}/restore`; the bootstrap admin is re-applied from env on the next
start. See the [backup-restore skill](../wiki/skills.md).

## Config yaml
`RAMEN_CONFIG=/path/ramen.yaml` — top-level keys map to `RAMEN_*` (nested keys join with `_`), env wins;
`POST $U/config/reload` hot-reloads. OAuth providers, SMTP, SA rules and auth toggles live here.

## Logs page
<figure markdown>
![Logs](../img/logs.png){ .ramen-shot }
</figure>
