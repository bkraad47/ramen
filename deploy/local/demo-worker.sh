#!/usr/bin/env bash
# Worker-only demo: run ramen-node locally against a clone of the demo repo and call the tool over /mcp.
# Needs: cargo, uv, git. Run from ramen/ via `make demo-worker`.
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
WORK=${RAMEN_DEMO_DIR:-/tmp/ramen-demo-worker}
PORT=${RAMEN_NODE_PORT:-8080}
KEY=${RAMEN_MCP_KEYS:-demo-key}
REPO=${RAMEN_DEMO_REPO:-https://github.com/bkraad47/ramen-demo-mcp-group}
mkdir -p "$WORK"
[ -d "$WORK/demo/.git" ] && git -C "$WORK/demo" pull -q || git clone -q "$REPO" "$WORK/demo"
(cd "$ROOT/runtime-py" && uv sync -q --all-extras)
(cd "$ROOT/node-rs" && cargo build -q --release)
RAMEN_BUCKET="$WORK/demo" RAMEN_PYTHON="$ROOT/runtime-py/.venv/bin/python" RAMEN_NODE_PORT=$PORT \
RAMEN_MCP_KEYS=$KEY RAMEN_ADMIN_KEY=demo-admin RAMEN_GROUP=demo RAMEN_ZONE=local \
  "$ROOT/node-rs/target/release/ramen-node" > "$WORK/node.log" 2>&1 &
NODE=$!
trap 'kill $NODE 2>/dev/null || true' EXIT
for _ in $(seq 1 60); do curl -sf "http://127.0.0.1:$PORT/readyz" >/dev/null && break; sleep 0.5; done
curl -sf "http://127.0.0.1:$PORT/readyz" >/dev/null || { echo "worker not ready; log:"; cat "$WORK/node.log"; exit 1; }
echo "metrics: $(curl -s http://127.0.0.1:$PORT/metrics)"
(cd "$ROOT/deploy/local" && uv run -q --with 'mcp>=2,<3' --python 3.14 python mcp_call.py "http://127.0.0.1:$PORT/mcp" "$KEY" demo_calculator_tool '{"var1": 2, "var2": 3, "func": "add"}')
