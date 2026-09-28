#!/usr/bin/env bash
# cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_target]
# Console → group demo → zone → env → MCP key → deploy → wait → tools/call over gRPC (CONTRACTS §11). Prints PASS/FAIL.
# node_target is `host:port` of the worker or the LB (routing by metadata ramen-group/ramen-zone, no path).
# Env: RAMEN_NODE_URL (if no 5th arg), RAMEN_NODE_TLS (1 through a TLS LB; self-signed accepted), RAMEN_SMOKE_GROUP (demo),
#      RAMEN_SMOKE_ZONE (a), RAMEN_ZONE_PROVIDER (gcp), RAMEN_ZONE_REGION (us-central1-a), RAMEN_SMOKE_ENV (dev),
#      RAMEN_DEMO_REPO, RAMEN_DEPLOY_TIMEOUT (600), RAMEN_SMOKE_ADMIN (1: also assert Admin/Reload; needs the worker's
#      RAMEN_ADMIN_CIDRS to include this host — through an LB it usually does not).
# admin_key is the worker RAMEN_ADMIN_KEY: Admin/Reload must be UNAUTHENTICATED/PERMISSION_DENIED without it.
# Needs grpcurl (brew/apt) + python3 + curl.
set -uo pipefail
HERE=$(cd "$(dirname "$0")" && pwd); ROOT=$(cd "$HERE/.." && pwd)
CONSOLE=${1:?usage: cloud_smoke.sh <console_url> <admin_email> <admin_pw> <admin_key> [node_target]}
EMAIL=${2:?admin_email}; PASS=${3:?admin_pw}; ADMIN_KEY=${4:?admin_key}
NODE=${5:-${RAMEN_NODE_URL:-}}
GROUP=${RAMEN_SMOKE_GROUP:-demo}; ZONE=${RAMEN_SMOKE_ZONE:-a}; ENVN=${RAMEN_SMOKE_ENV:-dev}
PROVIDER=${RAMEN_ZONE_PROVIDER:-gcp}; REGION=${RAMEN_ZONE_REGION:-us-central1-a}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
TIMEOUT=${RAMEN_DEPLOY_TIMEOUT:-600}
CONSOLE=${CONSOLE%/}; NODE=${NODE#grpc://}; NODE=${NODE#grpcs://}; NODE=${NODE#http://}; NODE=${NODE#https://}; NODE=${NODE%%/*}
JAR=$(mktemp); trap 'rm -f "$JAR"' EXIT
STEP=""; fail() { echo "FAIL: ${STEP}${1:+ — $1}" >&2; exit 1; }
C() { curl -sk -c "$JAR" -b "$JAR" --max-time 60 "$@"; }
csrf() { awk '$6=="ramen_csrf"{print $7}' "$JAR" | tail -1; }   # cookie sessions must echo the CSRF cookie (CONTRACTS §9)
api() { C -H 'Content-Type: application/json' -H "X-Ramen-CSRF: $(csrf)" -X "$1" "$CONSOLE/api/v1$2" ${3:+-d "$3"}; }
jget() { python3 -c 'import sys,json; d=json.load(sys.stdin); print(d'"$1"')' 2>/dev/null; }
b64() { python3 -c 'import sys,base64; print(base64.b64encode(sys.stdin.read().encode()).decode())'; }
unb64() { python3 -c 'import sys,json,base64; d=json.load(sys.stdin); print(base64.b64decode(d.get("body","")).decode())' 2>/dev/null; }

STEP="login"; code=$(C -o /dev/null -w '%{http_code}' -X POST "$CONSOLE/login" -d "email=$EMAIL" -d "password=$PASS"); [ "$code" = 303 ] || [ "$code" = 200 ] || fail "HTTP $code"
STEP="zone $ZONE";  api POST /zones "{\"name\":\"$ZONE\",\"provider\":\"$PROVIDER\",\"region\":\"$REGION\"}" >/dev/null
STEP="group $GROUP"; api POST /groups "{\"name\":\"$GROUP\",\"repo_url\":\"$REPO\",\"ref\":\"main\"}" >/dev/null
STEP="env $ENVN";   api POST "/groups/$GROUP/environments" "{\"name\":\"$ENVN\",\"ref\":\"main\",\"zones\":[\"$ZONE\"]}" >/dev/null
STEP="mcp key";     R=$(api POST "/groups/$GROUP/mcp-keys" "{\"name\":\"smoke-$(date +%s)\"}"); KEY=$(echo "$R" | jget '["key"]'); [ -n "$KEY" ] || fail "no key: ${R:0:200}"
STEP="deploy";      JOB=$(api POST "/groups/$GROUP/environments/$ENVN/deploy" '{"canary":true}' | jget '["id"]'); [ -n "$JOB" ] || fail "no job id"
deadline=$(( $(date +%s) + TIMEOUT )); S=running
while [ "$S" = running ]; do
  [ "$(date +%s)" -lt "$deadline" ] || fail "job $JOB still running after ${TIMEOUT}s"
  sleep 5; J=$(api GET "/jobs/$JOB"); S=$(echo "$J" | jget '.get("status","")')
done
[ "$S" = ok ] || fail "job $JOB: $(echo "$J" | jget '.get("error")')"
STEP="workers";     W=$(api GET "/groups/$GROUP/zones/$ZONE/workers"); echo "$W" | jget '["live"][0]["load"]' >/dev/null || fail "no live worker: $W"
[ -n "$NODE" ] || { echo "PASS (console only; pass node_target for the MCP call)"; exit 0; }
command -v grpcurl >/dev/null || fail "grpcurl not installed"
TLS=(-plaintext); [ "${RAMEN_NODE_TLS:-0}" = 1 ] && TLS=(-insecure)
G=(grpcurl "${TLS[@]}" -max-time 60 -import-path "$ROOT/proto" -import-path "$ROOT/tests/proto" -H "ramen-group: $GROUP" -H "ramen-zone: $ZONE")
health() { "${G[@]}" -proto grpc/health/v1/health.proto "$NODE" grpc.health.v1.Health/Check 2>/dev/null | jget '.get("status","")'; }
mcp() {  # mcp <key> <json> → response body; gRPC status text on stderr when non-OK (exit code of grpcurl kept)
  local key=$1 body; body=$(printf '%s' "$2" | b64)
  "${G[@]}" -proto ramen/v1/mcp.proto ${key:+-H "authorization: Bearer $key"} -d "{\"body\":\"$body\"}" "$NODE" ramen.v1.Mcp/Call
}
STEP="node ready (health SERVING)"; ok=0; for _ in $(seq 1 60); do [ "$(health)" = SERVING ] && { ok=1; break; }; sleep 3; done; [ $ok = 1 ] || fail "$NODE health → $(health)"
STEP="mcp initialize"; for _ in $(seq 1 20); do  # a draining old pod may still answer UNAUTHENTICATED for a few seconds after deploy
  R=$(mcp "$KEY" '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"cloud_smoke","version":"0.3.1"}}}' 2>/dev/null | unb64)
  [ "$(echo "$R" | jget '["result"]["protocolVersion"]')" = "2025-06-18" ] && break; sleep 3; done
[ "$(echo "$R" | jget '["result"]["protocolVersion"]')" = "2025-06-18" ] || fail "$R"
STEP="tools/call";  R=$(mcp "$KEY" '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}' 2>/dev/null | unb64)
OUT=$(echo "$R" | jget '["result"]["content"][0]["text"]'); [ "${OUT%.0}" = 5 ] || fail "$R"
STEP="unauth";      ERR=$(mcp "" '{"jsonrpc":"2.0","id":3,"method":"ping"}' 2>&1 >/dev/null); echo "$ERR" | grep -q Unauthenticated || fail "no-key call → ${ERR:-OK}"
STEP="bad key";     ERR=$(mcp "not-a-key" '{"jsonrpc":"2.0","id":4,"method":"ping"}' 2>&1 >/dev/null); echo "$ERR" | grep -q Unauthenticated || fail "bad-key call → ${ERR:-OK}"
# Admin/Reload without a key: UNAUTHENTICATED (direct), PERMISSION_DENIED (outside RAMEN_ADMIN_CIDRS) or UNIMPLEMENTED/404
# through an LB that does not route ramen.v1.Admin at all (CONTRACTS §11: Admin stays cluster-internal) — all three mean "gated".
STEP="admin gate";  ERR=$("${G[@]}" -proto ramen/v1/admin.proto "$NODE" ramen.v1.Admin/Reload 2>&1 >/dev/null); echo "$ERR" | grep -Eq 'Unauthenticated|PermissionDenied|Unimplemented' || fail "Admin/Reload without key → ${ERR:-OK}"
if [ "${RAMEN_SMOKE_ADMIN:-0}" = 1 ]; then
  STEP="admin reload"; "${G[@]}" -max-time 180 -proto ramen/v1/admin.proto -H "x-ramen-admin-key: $ADMIN_KEY" "$NODE" ramen.v1.Admin/Reload >/dev/null 2>&1 || fail "Admin/Reload with key failed"
fi
echo "PASS: $GROUP/$ENVN on zone $ZONE via $NODE (2+3=$OUT, job $JOB)"
