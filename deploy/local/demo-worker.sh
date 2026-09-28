#!/usr/bin/env bash
# Worker-only demo: run ramen-node locally against a clone of the demo repo and call the tool over gRPC via the bridge.
# Needs: cargo, uv, git (grpcurl optional, for the metrics line). Run from ramen/ via `make demo-worker`.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${RAMEN_DEMO_DIR:-/tmp/ramen-demo-worker}
PORT=${RAMEN_NODE_PORT:-8080}
KEY=${RAMEN_MCP_KEYS:-demo-key}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
mkdir -p "$WORK"
if [ -d "$WORK/demo/.git" ]; then git -C "$WORK/demo" pull -q; else git clone -q "$REPO" "$WORK/demo"; fi
(cd "$ROOT/runtime-py" && uv sync -q --all-extras)
(cd "$ROOT/node-rs" && cargo build -q --release)
BRIDGE="$ROOT/runtime-py/.venv/bin/ramen-mcp-bridge"
RAMEN_BUCKET="$WORK/demo" RAMEN_PYTHON="$ROOT/runtime-py/.venv/bin/python" RAMEN_NODE_PORT=$PORT \
RAMEN_MCP_KEYS=$KEY RAMEN_ADMIN_KEY=demo-admin RAMEN_GROUP=demo RAMEN_ZONE=local \
  "$ROOT/node-rs/target/release/ramen-node" > "$WORK/node.log" 2>&1 &
NODE=$!
trap 'kill $NODE 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do "$BRIDGE" --target "127.0.0.1:$PORT" --health --timeout 2 >/dev/null 2>&1 && break; sleep 0.5; done
"$BRIDGE" --target "127.0.0.1:$PORT" --health --timeout 2 >/dev/null || { echo "worker not ready; log:"; cat "$WORK/node.log"; exit 1; }
if command -v grpcurl >/dev/null; then
  echo "metrics: $(grpcurl -plaintext -H 'x-ramen-admin-key: demo-admin' "127.0.0.1:$PORT" ramen.v1.Admin/Metrics | python3 -c 'import sys,json,base64;print(base64.b64decode(json.load(sys.stdin)["json"]).decode())')"
fi
cd "$ROOT/runtime-py" && RAMEN_BRIDGE_GROUP=demo RAMEN_BRIDGE_ZONE=local \
  uv run -q --all-extras python "$ROOT/deploy/local/mcp_call.py" "127.0.0.1:$PORT" "$KEY" demo_calculator_tool '{"var1": 2, "var2": 3, "func": "add"}' | tee "$WORK/call.out"
RES=$(sed -n 's/.*-> \([^ ]*\).*/\1/p' "$WORK/call.out" | tail -1)
[ "$RES" = 5 ] || { echo "FAIL: demo_calculator_tool(2,3,add) -> $RES (expected 5)"; exit 1; }
echo "PASS: demo_calculator_tool(2,3,add) -> $RES"
