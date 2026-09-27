#!/usr/bin/env bash
# Full demo against the compose stack: create group `demo` → deploy → call demo_calculator_tool over MCP.
# Console API paths follow tests/src/ramen_tests/console.py ROUTES (CONTRACTS §4 has no route list yet).
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
CONSOLE=${RAMEN_CONSOLE_URL:-https://localhost:8443}
NODE=${RAMEN_NODE_URL:-http://localhost:8080}
EMAIL=${RAMEN_ADMIN_EMAIL:-admin@ramen.local}
PASS=${RAMEN_ADMIN_PASSWORD:-changeme-ramen}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
C="curl -sk -c $HERE/.cookies -b $HERE/.cookies"
$C -o /dev/null -w "login: %{http_code}\n" -X POST "$CONSOLE/login" -d "email=$EMAIL" -d "password=$PASS"
api() { $C -H 'Content-Type: application/json' -X "$1" "$CONSOLE/api/v1$2" ${3:+-d "$3"}; echo; }
api POST /zones '{"name":"local","provider":"local","region":"local"}'
api POST /groups "{\"name\":\"demo\",\"repo_url\":\"$REPO\",\"ref\":\"main\"}"
api POST /groups/demo/environments '{"name":"dev","ref":"main","zones":["local"]}'
KEY=$(api POST /groups/demo/mcp-keys "{\"name\":\"demo-$(date +%s)\"}" | python3 -c 'import sys,json;print(json.load(sys.stdin)["key"])')
JOB=$(api POST /groups/demo/environments/dev/deploy '{"canary":true}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["id"])')
for _ in $(seq 1 60); do S=$(api GET "/jobs/$JOB" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("status",""))'); [ "$S" = ok ] && break; [ "$S" = error ] && { echo "deploy failed"; exit 1; }; sleep 2; done
"$HERE/../../scripts/wait_ready.sh" "$NODE/readyz" 120 2>/dev/null || for _ in $(seq 1 60); do curl -sf "$NODE/readyz" >/dev/null && break; sleep 2; done
(cd "$HERE" && uv run -q --with 'mcp>=2,<3' --python 3.14 python mcp_call.py "$NODE/mcp" "$KEY" demo_calculator_tool '{"var1": 2, "var2": 3, "func": "add"}')
rm -f "$HERE/.cookies"
