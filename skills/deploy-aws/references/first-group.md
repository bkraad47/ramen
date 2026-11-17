# First group through the console API (Ramen 1.0)

Set `U=https://<console host>`, `PROVIDER` (`gcp` | `aws` | `azure`), `LOCATION` (a zone of a region that has a
cluster: `us-central1-a`, `us-east-1a`, `eastus-1`), and on AWS `CURL_CA="--cacert ramen-lb.pem"` (the self-signed
ALB certificate). The demo repo is `https://github.com/bkraad47/ramen-demo-mcp-group`.

## 0. A session (cookie + CSRF), password read without printing it
```sh
PW=$(kubectl --context "$CTX" -n ramen-system get secret ramen-console -o jsonpath='{.data.RAMEN_ADMIN_PASSWORD}' | base64 -d)
J=$(mktemp); chmod 600 "$J"
curl -s $CURL_CA -c "$J" -o /dev/null -X POST $U/login --data-urlencode email=admin@ramen.local --data-urlencode "password=$PW"
api() { curl -s $CURL_CA -b "$J" -c "$J" -H "X-Ramen-CSRF: $(awk '$6=="ramen_csrf"{print $7}' "$J")" \
  -H 'Content-Type: application/json' -X "$1" "$U/api/v1$2" ${3:+-d "$3"}; }
api GET /me            # 200 with role super_admin
```
An `rmn_` key works instead of the cookie: `-H "X-Ramen-Api-Key: $RMN"`, no CSRF header.

## 1. Group, environment, zone (1.0.0b model)
```sh
api POST /groups '{"name":"demo","repo_url":"https://github.com/bkraad47/ramen-demo-mcp-group","ref":"main"}'
api POST /environments '{"name":"prod","ref":"main","groups":["demo"]}'
api POST /zones "{\"name\":\"a\",\"provider\":\"$PROVIDER\",\"region\":\"$LOCATION\",\"environments\":[\"prod\"]}"
```
The zone answer carries `jobs`: creating it binds every derived group × zone pair and starts its canary deploy
(namespace `ramen-demo-a`, zone identity, route on the two headers, TLS leaf `ramen-tls`). `422` names the regions that
have a cluster; `403 region … is blocked` is a super admin's Config setting.

## 2. Wait for the deploy job
```sh
JOB=<id from jobs[0]>; while :; do S=$(api GET /jobs/$JOB | python3 -c 'import sys,json;print(json.load(sys.stdin)["status"])'); [ "$S" = running ] || break; sleep 5; done; echo $S
```
`ok` is done. On `error`, read `api GET /jobs/$JOB` (`error`, `log`): a group repo's pip error is the repo owner's;
`rail … needs an LLM` means step 6 of the skill (provider) was skipped. The first build of a group image can take
several minutes (GCP waits for the repository tag to reach IAM).

## 3. Accept the group's LLM configs (repos with `mcp/llm-config/`)
```sh
V=$(curl -s $CURL_CA "$U/.well-known/ramen-policy?group=demo&zone=a" | python3 -c 'import sys,json;print((json.load(sys.stdin).get("configs") or {}).get("version",""))')
[ -n "$V" ] && api POST /groups/demo/configs/accept "{\"version\":\"$V\"}"
```
Without it, minting a key answers `403 configs not accepted`.

## 4. A group key
```sh
K=$(mktemp); chmod 600 "$K"; api POST /groups/demo/mcp-keys '{"name":"first"}' > "$K"
python3 -c 'import sys,json;d=json.load(open(sys.argv[1]));print("pushed:",d.get("pushed"),"pending:",d.get("pending"))' "$K"
```
The console pushes the key to the running workers (D92), no deploy needed: the zone's workers roll to take it, so
it works within about a minute (15–60 s measured) — wait for a few successful calls in a row before relying on it. A `pending` zone gets it from the
background retry. Hand the key (`"key"` in `$K`) to the human once, then delete `$K`.

## 5. An `rmn_` API key for automation (optional)
`api POST /api-keys '{"name":"ops","role":"group_admin","groups":["demo"]}'` — the narrowest role that does the job;
shown once.

## 6. Call a tool
```sh
RMK=$(python3 -c 'import sys,json;print(json.load(open(sys.argv[1]))["key"])' "$K")
curl -s $CURL_CA $U/mcp -H "Authorization: Bearer $RMK" -H 'Content-Type: application/json' -H 'Accept: application/json' \
  -H 'ramen-group: demo' -H 'ramen-zone: a' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"},"_meta":{"io.ramen/configs":{"version":"'"$V"'"}}}}'
```
Expect `"text":"5"`. `-32028 configs required` means the `_meta` is missing; `404` from the edge for a few minutes
after the zone appeared is the route still programming. People connect with OAuth instead: the group page's
**How MCP users sign in** card prints the commands (bridge 1.0.0 is TLS by default).
