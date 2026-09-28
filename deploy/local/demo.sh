#!/usr/bin/env bash
# Full demo against the compose stack: create group `demo` → deploy → call demo_calculator_tool over MCP (gRPC via the stdio bridge, CONTRACTS §11).
# Console API paths follow CONTRACTS §4a (mirrored by tests/src/ramen_tests/console.py ROUTES).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CONSOLE=${RAMEN_CONSOLE_URL:-https://localhost:8443}
NODE=${RAMEN_NODE_TARGET:-localhost:8080}
ROOT=$(cd "$HERE/../.." && pwd)
EMAIL=${RAMEN_ADMIN_EMAIL:-admin@ramen.local}
PASS=${RAMEN_ADMIN_PASSWORD:-changeme-ramen}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
C="curl -sk -c $HERE/.cookies -b $HERE/.cookies"
$C -o /dev/null -w "login: %{http_code}\n" -X POST "$CONSOLE/login" -d "email=$EMAIL" -d "password=$PASS"
CSRF=$(awk '$6=="ramen_csrf"{print $7}' "$HERE/.cookies")   # CONTRACTS §9: cookie sessions send X-Ramen-CSRF on mutations
api() { $C -H 'Content-Type: application/json' -H "X-Ramen-CSRF: $CSRF" -X "$1" "$CONSOLE/api/v1$2" ${3:+-d "$3"}; echo; }
api POST /zones '{"name":"local","provider":"local","region":"local"}'
api POST /groups "{\"name\":\"demo\",\"repo_url\":\"$REPO\",\"ref\":\"main\"}"
api POST /groups/demo/environments '{"name":"dev","ref":"main","zones":["local"]}'
KEY=$(api POST /groups/demo/mcp-keys "{\"name\":\"demo-$(date +%s)\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
JOB=$(api POST /groups/demo/environments/dev/deploy '{"canary":true}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
for _ in $(seq 1 60); do S=$(api GET "/jobs/$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))'); [ "$S" = ok ] && break; [ "$S" = error ] && { echo "deploy failed"; exit 1; }; sleep 2; done
(cd "$ROOT/runtime-py" && uv sync -q --all-extras)
BRIDGE="$ROOT/runtime-py/.venv/bin/ramen-mcp-bridge"
for _ in $(seq 1 60); do "$BRIDGE" --target "$NODE" --health --timeout 2 >/dev/null 2>&1 && break; sleep 2; done   # SERVING once loaded
echo "health: SERVING $NODE"
OUT=$(cd "$ROOT/runtime-py" && RAMEN_BRIDGE_GROUP=demo RAMEN_BRIDGE_ZONE=local uv run -q --all-extras python "$HERE/mcp_call.py" "$NODE" "$KEY" demo_calculator_tool '{"var1": 2, "var2": 3, "func": "add"}')
echo "$OUT"
rm -f "$HERE/.cookies"
RES=$(printf '%s\n' "$OUT" | sed -n 's/.*-> \([^ ]*\).*/\1/p' | tail -1)
RES=$(python3 -c 'import sys;v=float(sys.argv[1]);print(int(v) if v.is_integer() else v)' "$RES") || { echo "FAIL: unexpected result '$RES'"; exit 1; }
[ "$RES" = 5 ] || { echo "FAIL: demo_calculator_tool(2,3,add) -> $RES (expected 5)"; exit 1; }
echo "PASS: demo_calculator_tool(2,3,add) -> $RES"
