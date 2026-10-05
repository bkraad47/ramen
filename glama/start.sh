#!/bin/sh
# Start the Ramen worker for the bundled demo group, wait until it has loaded the group's code, then serve MCP on
# stdio through the bridge. stdout carries only JSON-RPC; the worker's logs go to stderr.
set -eu
KEY=${RAMEN_MCP_KEYS:-local-demo-key}
RAMEN_MCP_KEYS=$KEY RAMEN_ADMIN_KEY=${RAMEN_ADMIN_KEY:-local-admin} RAMEN_GROUP=demo RAMEN_ZONE=local \
  ramen-node 1>&2 &
B=/opt/venv/bin/ramen-mcp-bridge
i=0
until $B --target "127.0.0.1:$RAMEN_NODE_PORT" --health --timeout 2 >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 120 ] || { echo "ramen worker did not become ready" >&2; exit 1; }; sleep 0.5
done
exec $B --target "127.0.0.1:$RAMEN_NODE_PORT" --key "$KEY" --group demo --zone local
