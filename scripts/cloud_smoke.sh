#!/usr/bin/env bash
# cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_target]
# Console → group demo → zone → env → MCP key → deploy → wait → tools/call over gRPC (CONTRACTS §11). Prints PASS/FAIL.
# node_target is `host:port` of the worker or the LB (routing by metadata ramen-group/ramen-zone, no path).
# Env: RAMEN_NODE_URL (if no 5th arg), RAMEN_NODE_TLS (1 through a TLS LB), RAMEN_SMOKE_CA (PEM to verify the console
#      and the LB against; the self-signed cert the deploy made), RAMEN_SMOKE_INSECURE (1: skip TLS verification — a
#      laptop against a fresh cluster whose cert is not yet at hand; never in CI), RAMEN_SMOKE_GROUP (demo),
#      RAMEN_SMOKE_ZONE (a), RAMEN_ZONE_PROVIDER (gcp | aws | azure), RAMEN_ZONE_REGION (us-central1-a; aws us-east-1a;
#      azure eastus-1 — AKS zones are <location>-<n>), RAMEN_SMOKE_ENV (dev),
#      RAMEN_DEMO_REPO, RAMEN_DEPLOY_TIMEOUT (600), RAMEN_SMOKE_POLL (5 s), RAMEN_SMOKE_ADMIN (1: also assert Admin/Reload; needs the worker's
#      RAMEN_ADMIN_CIDRS to include this host — through an LB it usually does not).
# admin_key is the worker RAMEN_ADMIN_KEY: Admin/Reload must be UNAUTHENTICATED/PERMISSION_DENIED without it.
# Needs grpcurl (brew/apt) + python3 + curl.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/.." && pwd)
CONSOLE=${1:?usage: cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_target]}
EMAIL=${2:?admin_email}; PASS=${3:?admin_pw}; ADMIN_KEY=${4:?admin_key}
NODE=${5:-${RAMEN_NODE_URL:-}}
GROUP=${RAMEN_SMOKE_GROUP:-demo}; ZONE=${RAMEN_SMOKE_ZONE:-a}; ENVN=${RAMEN_SMOKE_ENV:-dev}
PROVIDER=${RAMEN_ZONE_PROVIDER:-gcp}
case "$PROVIDER" in aws) DEFAULT_REGION=us-east-1a ;; azure) DEFAULT_REGION=eastus-1 ;; *) DEFAULT_REGION=us-central1-a ;; esac
REGION=${RAMEN_ZONE_REGION:-$DEFAULT_REGION}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
TIMEOUT=${RAMEN_DEPLOY_TIMEOUT:-600}
CONSOLE=${CONSOLE%/}; NODE=${NODE#grpc://}; NODE=${NODE#grpcs://}; NODE=${NODE#http://}; NODE=${NODE#https://}; NODE=${NODE%%/*}
JAR=$(mktemp); BODY=$(mktemp); trap 'rm -f "$JAR" "$BODY"' EXIT
STEP=""; fail() { echo "FAIL: ${STEP}${1:+ — $1}" >&2; exit 1; }
# TLS: verified against RAMEN_SMOKE_CA when given, the system store otherwise; -k only on explicit request (review 0.5.0 M6)
CURL_TLS=(); [ -n "${RAMEN_SMOKE_CA:-}" ] && CURL_TLS=(--cacert "$RAMEN_SMOKE_CA"); [ "${RAMEN_SMOKE_INSECURE:-0}" = 1 ] && CURL_TLS=(-k)
C() { curl -s ${CURL_TLS[@]+"${CURL_TLS[@]}"} -c "$JAR" -b "$JAR" --max-time 60 "$@"; }
csrf() { awk '$6=="ramen_csrf"{print $7}' "$JAR" | tail -1; }   # cookie sessions must echo the CSRF cookie (CONTRACTS §9)
api() { C -H 'Content-Type: application/json' -H "X-Ramen-CSRF: $(csrf)" -X "$1" "$CONSOLE/api/v1$2" ${3:+-d "$3"}; }
jget() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')' 2>/dev/null; }
b64() { python3 -c 'import sys,base64; print(base64.b64encode(sys.stdin.read().encode()).decode())'; }
unb64() { python3 -c 'import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d.get("body","")).decode())' 2>/dev/null; }

STEP="login"; code=$(C -o /dev/null -w '%{http_code}' -X POST "$CONSOLE/login" --data-urlencode "email=$EMAIL" --data-urlencode "password=$PASS"); [ "$code" = 303 ] || [ "$code" = 200 ] || fail "HTTP $code"
STEP="zone $ZONE";  api POST /zones "{\"name\":\"$ZONE\",\"provider\":\"$PROVIDER\",\"region\":\"$REGION\",\"environments\":[]}" >/dev/null
STEP="group $GROUP"; api POST /groups "{\"name\":\"$GROUP\",\"repo_url\":\"$REPO\",\"ref\":\"${RAMEN_DEMO_REF:-main}\"}" >/dev/null
# CONTRACTS §27.2: the per-group environment route attaches the environment to the zone (the zone's groups are derived,
# no bind step); the attach starts a canary deploy of the pair (§27.3), so the explicit deploy below may be told one
# is already running — then that job is the one to wait for.
STEP="env $ENVN";   api POST "/groups/$GROUP/environments" "{\"name\":\"$ENVN\",\"ref\":\"${RAMEN_DEMO_REF:-main}\",\"zones\":[\"$ZONE\"]}" >/dev/null
# CONTRACTS §31.1: the group's LLM configs (`mcp/llm-config/` in the repo) must be accepted by whoever mints a key; the
# public policy names the version (empty when the repo has none or the console predates §31).
configs_version() { C "$CONSOLE/.well-known/ramen-policy?group=$GROUP&zone=$ZONE" | jget '.get("configs",{}).get("version","")'; }
STEP="mcp key";     R=$(api POST "/groups/$GROUP/mcp-keys" "{\"name\":\"smoke-$(date +%s)\"}"); KEY=$(echo "$R" | jget '["key"]')
if [ -z "$KEY" ] && echo "$R" | grep -q "configs not accepted"; then
  STEP="configs accept"; V=$(configs_version); [ -n "$V" ] || fail "403 configs not accepted but the policy names no version: ${R:0:200}"
  api POST "/groups/$GROUP/configs/accept" "{\"version\":\"$V\"}" >/dev/null
  STEP="mcp key";   R=$(api POST "/groups/$GROUP/mcp-keys" "{\"name\":\"smoke-$(date +%s)\"}"); KEY=$(echo "$R" | jget '["key"]')
fi
[ -n "$KEY" ] || fail "no key: ${R:0:200}"
# RAMEN_SMOKE_BREAKING=1 accepts a breaking tool-schema change of the demo ref (CONTRACTS §23.3 compat gate)
STEP="deploy";      R=$(api POST "/groups/$GROUP/environments/$ENVN/deploy" "{\"canary\":true${RAMEN_SMOKE_BREAKING:+,\"breaking\":true}}")
JOB=$(echo "$R" | jget '["id"]') || JOB=$(echo "$R" | python3 -c 'import sys,re; m=re.search(r"already running \(job ([\w-]+)\)", sys.stdin.read()); print(m.group(1) if m else "")')
[ -n "$JOB" ] || fail "no job id: ${R:0:200}"
deadline=$(( $(date +%s) + TIMEOUT )); S=running
while [ "$S" = running ]; do  # a non-JSON answer (LB blip) is not a verdict: poll again
  [ "$(date +%s)" -lt "$deadline" ] || fail "job $JOB still ${S:-unanswered} after ${TIMEOUT}s: ${J:0:200}"
  sleep "${RAMEN_SMOKE_POLL:-5}"; J=$(api GET "/jobs/$JOB"); S=$(echo "$J" | jget '.get("status","")') || S=running
done
[ "$S" = ok ] || fail "job $JOB $S: $(echo "$J" | jget '.get("error")')"
# CONTRACTS §29.2: the zone's evals record when the deploy carried one (`evals: <n> passed, <n> failed`, or `absent` /
# `{"skipped": "no evals engine"}`); printed, never a verdict for the smoke
EV=$(echo "$J" | python3 -c 'import sys,json; d=json.load(sys.stdin); e=((d.get("result") or {}).get(sys.argv[1]) or {}).get("evals")
print("evals: %s passed, %s failed" % (e.get("passed"), e.get("failed")) if isinstance(e, dict) and "passed" in e else ("evals: %s" % (e if isinstance(e, str) else json.dumps(e)) if e else ""))' "$ZONE" 2>/dev/null); [ -n "$EV" ] && echo "$EV"
# CONTRACTS §30.2: the zone's SkillSpector record likewise (`skillspector: <recommendation>, score <n>, <n> findings`,
# or the `skipped` / `error` record); printed, never a verdict for the smoke
SP=$(echo "$J" | python3 -c 'import sys,json; d=json.load(sys.stdin); s=((d.get("result") or {}).get(sys.argv[1]) or {}).get("skillspector")
print("skillspector: %s, score %s, %s findings" % (s.get("recommendation"), s.get("score"), s.get("findings")) if isinstance(s, dict) and "score" in s else ("skillspector: %s" % json.dumps(s) if s else ""))' "$ZONE" 2>/dev/null); [ -n "$SP" ] && echo "$SP"
# CONTRACTS §31.1: the LLM configs version the deploy published (`configs: <version|none>`); printed, never a verdict
V=$(configs_version); echo "configs: ${V:-none}"
# CONTRACTS §36.3: the guardrails policy version (`guardrails: <version|none>`; set when the repo has mcp/guardrails.yaml)
GV=$(C "$CONSOLE/.well-known/ramen-policy?group=$GROUP&zone=$ZONE" | jget '.get("guardrails",{}).get("version","")'); echo "guardrails: ${GV:-none}"
# CONTRACTS §31.4a: a bare client is not exempt from the worker gates — every `tools/call` below carries the versions
# the public policy marks `required` (`_meta["io.ramen/evals"|"io.ramen/skillspector"|"io.ramen/configs"|
# "io.ramen/guardrails"]`) and, when telemetry is required, an empty report (`_meta["io.ramen/telemetry"]`, §26.3).
# `{}` when nothing is required.
META=$(C "$CONSOLE/.well-known/ramen-policy?group=$GROUP&zone=$ZONE" | python3 -c 'import sys,json
try: p=json.load(sys.stdin)
except ValueError: p={}
m={"io.ramen/"+k: {"version": v["version"]} for k, v in p.items() if k in ("evals","skillspector","configs","guardrails")
   and isinstance(v, dict) and v.get("required") and isinstance(v.get("version"), str)}
t=p.get("telemetry") or {}
if isinstance(t, dict) and t.get("required"): m["io.ramen/telemetry"]={"version": t.get("version",""), "seq": 1, "events": []}
print(json.dumps(m, separators=(",",":")))' 2>/dev/null); META=${META:-"{}"}
[ "$META" = "{}" ] || echo "policy: $META"
CALC_PARAMS='{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"},"_meta":'"$META"'}'
STEP="workers";     W=$(api GET "/groups/$GROUP/zones/$ZONE/workers"); echo "$W" | jget '["live"][0]["load"]' >/dev/null || fail "no live worker: $W"
[ -n "$NODE" ] || { echo "PASS (console only; pass node_target for the MCP call)"; exit 0; }
command -v grpcurl >/dev/null || fail "grpcurl not installed"
TLS=(-plaintext)
if [ "${RAMEN_NODE_TLS:-0}" = 1 ]; then
  TLS=(); [ -n "${RAMEN_SMOKE_CA:-}" ] && TLS=(-cacert "$RAMEN_SMOKE_CA"); [ "${RAMEN_SMOKE_INSECURE:-0}" = 1 ] && TLS=(-insecure)
fi
G=(grpcurl ${TLS[@]+"${TLS[@]}"} -max-time 60 -import-path "$ROOT/proto" -import-path "$ROOT/tests/proto" -H "ramen-group: $GROUP" -H "ramen-zone: $ZONE")
health() { "${G[@]}" -proto grpc/health/v1/health.proto "$NODE" grpc.health.v1.Health/Check 2>/dev/null | jget '.get("status","")'; }
mcp() {  # mcp <key> <json> → response body; gRPC status text on stderr when non-OK (exit code of grpcurl kept)
  local key=$1 body; body=$(printf '%s' "$2" | b64)
  "${G[@]}" -proto ramen/v1/mcp.proto ${key:+-H "authorization: Bearer $key"} -d "{\"body\":\"$body\"}" "$NODE" ramen.v1.Mcp/Call
}
STEP="node ready (health SERVING)"; ok=0; for _ in $(seq 1 60); do [ "$(health)" = SERVING ] && { ok=1; break; }; sleep 3; done; [ $ok = 1 ] || fail "$NODE health → $(health)"
STEP="mcp initialize"; for _ in $(seq 1 20); do  # a draining old pod may still answer UNAUTHENTICATED for a few seconds after deploy
  R=$(mcp "$KEY" '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cloud_smoke","version":"0.4.0"}}}' 2>/dev/null | unb64)
  [ "$(echo "$R" | jget '["result"]["protocolVersion"]')" = "2025-06-18" ] && break; sleep 3; done
[ "$(echo "$R" | jget '["result"]["protocolVersion"]')" = "2025-06-18" ] || fail "$R"
STEP="tools/call";  R=$(mcp "$KEY" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":'"$CALC_PARAMS"'}' 2>/dev/null | unb64)
OUT=$(echo "$R" | jget '["result"]["content"][0]["text"]'); [ "${OUT%.0}" = 5 ] || fail "$R"
STEP="unauth";      ERR=$(mcp "" '{"jsonrpc":"2.0","id":3,"method":"ping"}' 2>&1 >/dev/null); echo "$ERR" | grep -q Unauthenticated || fail "no-key call → ${ERR:-OK}"
STEP="bad key";     ERR=$(mcp "not-a-key" '{"jsonrpc":"2.0","id":4,"method":"ping"}' 2>&1 >/dev/null); echo "$ERR" | grep -q Unauthenticated || fail "bad-key call → ${ERR:-OK}"
# CONTRACTS §16: the same worker, the same guards, over Streamable HTTP — a URL and a bearer header through the LB
HTTP_URL=${RAMEN_MCP_URL:-$([ "${RAMEN_NODE_TLS:-0}" = 1 ] && echo "https://$NODE/mcp" || echo "http://$NODE/mcp")}
http_mcp() {  # http_mcp <key> <json> → "<status> <body>"
  local key=$1
  curl -s ${CURL_TLS[@]+"${CURL_TLS[@]}"} -o "$BODY" -w '%{http_code}' -X POST "$HTTP_URL" -H 'Content-Type: application/json' -H 'Accept: application/json' \
    -H "ramen-group: $GROUP" -H "ramen-zone: $ZONE" ${key:+-H "Authorization: Bearer $key"} -d "$2"; echo " $(cat "$BODY")"
}
STEP="http tools/call"; R=$(http_mcp "$KEY" '{"jsonrpc":"2.0","id":5,"method":"tools/call","params":'"$CALC_PARAMS"'}')
[ "${R%% *}" = 200 ] || fail "POST $HTTP_URL → $R"
OUT=$(echo "${R#* }" | jget '["result"]["content"][0]["text"]'); [ "${OUT%.0}" = 5 ] || fail "http result: $R"
STEP="http unauth";    R=$(http_mcp "" '{"jsonrpc":"2.0","id":6,"method":"ping"}'); [ "${R%% *}" = 401 ] || fail "no-key POST → ${R%% *}"
STEP="http bad key";   R=$(http_mcp "not-a-key" '{"jsonrpc":"2.0","id":7,"method":"ping"}'); [ "${R%% *}" = 401 ] || fail "bad-key POST → ${R%% *}"
STEP="http origin";    R=$(curl -s ${CURL_TLS[@]+"${CURL_TLS[@]}"} -o /dev/null -w '%{http_code}' -X POST "$HTTP_URL" -H 'Content-Type: application/json' -H "Authorization: Bearer $KEY" -H 'Origin: https://evil.example' -H "ramen-group: $GROUP" -H "ramen-zone: $ZONE" -d '{"jsonrpc":"2.0","id":8,"method":"ping"}'); [ "$R" = 403 ] || fail "foreign Origin → $R"
STEP="http session";   SID=$(curl -s ${CURL_TLS[@]+"${CURL_TLS[@]}"} -D - -o /dev/null -X POST "$HTTP_URL" -H 'Content-Type: application/json' -H "Authorization: Bearer $KEY" -H "ramen-group: $GROUP" -H "ramen-zone: $ZONE" -d '{"jsonrpc":"2.0","id":9,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cloud_smoke","version":"0.5.0"}}}' | tr -d '\r' | awk 'tolower($1)=="mcp-session-id:"{print $2}')
[ -n "$SID" ] || fail "initialize over HTTP returned no Mcp-Session-Id"
# Admin/Reload without a key: UNAUTHENTICATED (direct), PERMISSION_DENIED (outside RAMEN_ADMIN_CIDRS) or UNIMPLEMENTED/404
# through an LB that does not route ramen.v1.Admin at all (CONTRACTS §11: Admin stays cluster-internal); Envoy Gateway (AKS) closes
# the stream without trailers for such a path. All of them mean "gated".
STEP="admin gate";  ERR=$("${G[@]}" -proto ramen/v1/admin.proto "$NODE" ramen.v1.Admin/Reload 2>&1 >/dev/null); echo "$ERR" | grep -Eq 'Unauthenticated|PermissionDenied|Unimplemented|status code received from server: 464|closed the stream without sending trailers' || fail "Admin/Reload without key → ${ERR:-OK}"   # 464: the ALB has no gRPC route for Admin/* (console catch-all), which is the point
if [ "${RAMEN_SMOKE_ADMIN:-0}" = 1 ]; then
  STEP="admin reload"; "${G[@]}" -max-time 180 -proto ramen/v1/admin.proto -H "x-ramen-admin-key: $ADMIN_KEY" "$NODE" ramen.v1.Admin/Reload >/dev/null 2>&1 || fail "Admin/Reload with key failed"
fi
echo "PASS: $GROUP/$ENVN on zone $ZONE via $NODE (2+3=$OUT, job $JOB)"
# CONTRACTS §28 (satellite clusters): with RAMEN_SATELLITE_REGION (a zone of a satellite cluster's region, e.g.
# australia-southeast1-b / ap-southeast-2a / australiaeast-1) and RAMEN_SATELLITE_HOST (that cluster's edge), a second
# zone there is attached to the same environment, its attach job waited for, 2+3 called through the satellite host over
# Streamable HTTP, and the zone deleted (D69 cleanup on the satellite). RAMEN_SATELLITE_CA: PEM of the satellite edge
# (an EKS ALB serves its own certificate); RAMEN_SMOKE_CA / RAMEN_SMOKE_INSECURE otherwise.
if [ -n "${RAMEN_SATELLITE_REGION:-}" ] && [ -n "${RAMEN_SATELLITE_HOST:-}" ]; then
  SAT_HOST=${RAMEN_SATELLITE_HOST#https://}; SAT_HOST=${SAT_HOST#http://}; SAT_HOST=${SAT_HOST%%/*}
  SAT_ZONE=${RAMEN_SMOKE_SATELLITE_ZONE:-${ZONE}s}
  SAT_TLS=(); [ "${#CURL_TLS[@]}" -gt 0 ] && SAT_TLS=("${CURL_TLS[@]}"); [ -n "${RAMEN_SATELLITE_CA:-}" ] && SAT_TLS=(--cacert "$RAMEN_SATELLITE_CA")
  STEP="satellite cluster for $RAMEN_SATELLITE_REGION"; R=$(api GET /config/regions)
  echo "$R" | python3 -c 'import sys,json; d=json.load(sys.stdin); r=sys.argv[1]; p=sys.argv[2]
ok=any(c.get("provider")==p and (r==c["region"] or r.startswith(c["region"]+"-") or (p=="aws" and r.startswith(c["region"]) and r[len(c["region"]):].isalpha())) for c in d.get("clusters",[]))
sys.exit(0 if ok else 1)' "$RAMEN_SATELLITE_REGION" "$PROVIDER" || fail "GET /config/regions lists no $PROVIDER cluster covering it: ${R:0:300}"
  STEP="satellite zone $SAT_ZONE"; R=$(api POST /zones "{\"name\":\"$SAT_ZONE\",\"provider\":\"$PROVIDER\",\"region\":\"$RAMEN_SATELLITE_REGION\",\"environments\":[\"$ENVN\"]}")
  SJOB=$(echo "$R" | jget '["jobs"][0]'); [ -n "$SJOB" ] || fail "no attach job: ${R:0:300}"
  echo "$R" | jget '["host"]' | grep -q "$SAT_HOST" || fail "zone host is not $SAT_HOST: ${R:0:300}"
  deadline=$(( $(date +%s) + TIMEOUT )); S=running
  while [ "$S" = running ]; do
    [ "$(date +%s)" -lt "$deadline" ] || fail "job $SJOB still ${S:-unanswered} after ${TIMEOUT}s: ${J:0:200}"
    sleep "${RAMEN_SMOKE_POLL:-5}"; J=$(api GET "/jobs/$SJOB"); S=$(echo "$J" | jget '.get("status","")') || S=running
  done
  [ "$S" = ok ] || fail "job $SJOB $S: $(echo "$J" | jget '.get("error")')"
  STEP="satellite http tools/call via $SAT_HOST"; SOUT=""
  for _ in $(seq 1 40); do  # the satellite's route and certificate may take a minute after the attach
    R=$(curl -s ${SAT_TLS[@]+"${SAT_TLS[@]}"} -o "$BODY" -w '%{http_code}' --max-time 60 -X POST "https://$SAT_HOST/mcp" -H 'Content-Type: application/json' -H 'Accept: application/json' \
      -H "ramen-group: $GROUP" -H "ramen-zone: $SAT_ZONE" -H "Authorization: Bearer $KEY" \
      -d '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":'"$CALC_PARAMS"'}'; echo " $(cat "$BODY")")
    SOUT=$(echo "${R#* }" | jget '["result"]["content"][0]["text"]'); [ "${SOUT%.0}" = 5 ] && break; sleep 10
  done
  [ "${SOUT%.0}" = 5 ] || fail "POST https://$SAT_HOST/mcp → $R"
  STEP="satellite zone delete"; code=$(C -o /dev/null -w '%{http_code}' -H "X-Ramen-CSRF: $(csrf)" -X DELETE "$CONSOLE/api/v1/zones/$SAT_ZONE"); [ "$code" = 200 ] || [ "$code" = 204 ] || fail "DELETE → $code"
  echo "PASS: satellite $GROUP/$ENVN on zone $SAT_ZONE ($RAMEN_SATELLITE_REGION) via $SAT_HOST (2+3=$SOUT, job $SJOB)"
fi
