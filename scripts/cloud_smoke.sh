#!/usr/bin/env bash
# cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_url]
# Console → group demo → zone a → env → MCP key → deploy → wait → tools/call through the node/LB. Prints PASS/FAIL.
# Env: RAMEN_NODE_URL (if no 5th arg), RAMEN_SMOKE_GROUP (demo), RAMEN_SMOKE_ZONE (a), RAMEN_ZONE_PROVIDER (gcp),
#      RAMEN_ZONE_REGION (us-central1-a), RAMEN_SMOKE_ENV (dev), RAMEN_DEMO_REPO, RAMEN_DEPLOY_TIMEOUT (600).
# admin_key is the worker RAMEN_ADMIN_KEY: /admin/reload must be 401/403 without it and 200 with it.
# node_url may be a bare node (http://host:8080) or an MCP-only LB route (https://<ip>/mcp/<group>/<zone>); on the
# latter /readyz and /admin/* are not routed, so those steps are skipped and the MCP endpoint is used as given.
set -uo pipefail
CONSOLE=${1:?usage: cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_url]}
EMAIL=${2:?admin_email}; PASS=${3:?admin_pw}; ADMIN_KEY=${4:?admin_key}
NODE=${5:-${RAMEN_NODE_URL:-}}
GROUP=${RAMEN_SMOKE_GROUP:-demo}; ZONE=${RAMEN_SMOKE_ZONE:-a}; ENVN=${RAMEN_SMOKE_ENV:-dev}
PROVIDER=${RAMEN_ZONE_PROVIDER:-gcp}; REGION=${RAMEN_ZONE_REGION:-us-central1-a}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
TIMEOUT=${RAMEN_DEPLOY_TIMEOUT:-600}
CONSOLE=${CONSOLE%/}; NODE=${NODE%/}
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
STEP=""; fail() { echo "FAIL: ${STEP}${1:+ — $1}" >&2; exit 1; }
C() { curl -sk -c "$JAR" -b "$JAR" --max-time 60 "$@"; }
api() { C -H 'Content-Type: application/json' -X "$1" "$CONSOLE/api/v1$2" ${3:+-d "$3"}; }
jget() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')' 2>/dev/null; }

STEP="login"; code=$(C -o /dev/null -w '%{http_code}' -X POST "$CONSOLE/login" -d "email=$EMAIL" -d "password=$PASS"); [ "$code" = 303 ] || [ "$code" = 200 ] || fail "HTTP $code"
STEP="zone $ZONE";  api POST /zones "{\"name\":\"$ZONE\",\"provider\":\"$PROVIDER\",\"region\":\"$REGION\"}" >/dev/null
STEP="group $GROUP"; api POST /groups "{\"name\":\"$GROUP\",\"repo_url\":\"$REPO\",\"ref\":\"main\"}" >/dev/null
STEP="env $ENVN";   api POST "/groups/$GROUP/environments" "{\"name\":\"$ENVN\",\"ref\":\"main\",\"zones\":[\"$ZONE\"]}" >/dev/null
STEP="mcp key";     KEY=$(api POST "/groups/$GROUP/mcp-keys" "{\"name\":\"smoke-$(date +%s)\"}" | jget '["key"]'); [ -n "$KEY" ] || fail "no key"
STEP="deploy";      JOB=$(api POST "/groups/$GROUP/environments/$ENVN/deploy" '{"canary":true}' | jget '["id"]'); [ -n "$JOB" ] || fail "no job id"
deadline=$(( $(date +%s) + TIMEOUT )); S=running
while [ "$S" = running ]; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "job $JOB still running after ${TIMEOUT}s"
  sleep 5; J=$(api GET "/jobs/$JOB"); S=$(echo "$J" | jget '.get("status","")')
done
[ "$S" = ok ] || fail "job $JOB: $(echo "$J" | jget '.get("error")')"
STEP="workers";     W=$(api GET "/groups/$GROUP/zones/$ZONE/workers"); echo "$W" | jget '["live"][0]["load"]' >/dev/null || fail "no live worker: $W"
[ -n "$NODE" ] || { echo "PASS (console only; pass node_url for the MCP call)"; exit 0; }
case "${NODE#*://}" in */mcp*) MCP=$NODE; ADMIN=0 ;; *) MCP=$NODE/mcp; ADMIN=1 ;; esac
H=(-H 'Content-Type: application/json' -H 'Accept: application/json, text/event-stream' -H "Authorization: Bearer $KEY")
if [ $ADMIN = 1 ]; then
  STEP="node ready";  ok=0; for _ in $(seq 1 60); do [ "$(curl -sk -o /dev/null -w '%{http_code}' --max-time 5 "$NODE/readyz")" = 200 ] && { ok=1; break; }; sleep 3; done; [ $ok = 1 ] || fail "$NODE/readyz"
  STEP="admin gate";  code=$(curl -sk -o /dev/null -w '%{http_code}' -X POST "$NODE/admin/reload"); { [ "$code" = 401 ] || [ "$code" = 403 ]; } || fail "/admin/reload without key → $code"
else
  STEP="mcp route ready"; ok=0; for _ in $(seq 1 60); do c=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 15 "${H[@]}" -X POST "$MCP" -d '{"jsonrpc":"2.0","id":0,"method":"ping"}'); [ "$c" = 200 ] && { ok=1; break; }; sleep 3; done; [ $ok = 1 ] || fail "$MCP ping → $c"
fi
STEP="mcp initialize"; R=$(curl -sk --max-time 30 "${H[@]}" -X POST "$MCP" -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cloud_smoke","version":"0.2.0"}}}')
[ "$(echo "$R" | jget '["result"]["protocolVersion"]')" = "2025-06-18" ] || fail "$R"
STEP="tools/call";  R=$(curl -sk --max-time 60 "${H[@]}" -X POST "$MCP" -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}')
OUT=$(echo "$R" | jget '["result"]["content"][0]["text"]'); [ "${OUT%.0}" = 5 ] || fail "$R"
if [ $ADMIN = 1 ]; then STEP="admin reload"; code=$(curl -sk -o /dev/null -w '%{http_code}' --max-time 120 -X POST "$NODE/admin/reload" -H "X-Ramen-Admin-Key: $ADMIN_KEY"); [ "$code" = 200 ] || fail "/admin/reload with key → $code"; fi
STEP="unauth";      code=$(curl -sk -o /dev/null -w '%{http_code}' -H 'Content-Type: application/json' -X POST "$MCP" -d '{"jsonrpc":"2.0","id":3,"method":"ping"}'); [ "$code" = 401 ] || fail "no-key call → $code"
echo "PASS: $GROUP/$ENVN on zone $ZONE via $MCP (2+3=$OUT, job $JOB)"
