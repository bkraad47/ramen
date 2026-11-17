#!/bin/sh
# Start the Ramen worker for the bundled demo group, wait until it has loaded the group's code, then serve MCP on
# stdio through the bridge. stdout carries only JSON-RPC; the worker's logs go to stderr.
set -eu
KEY=${RAMEN_MCP_KEYS:-local-demo-key}
# TLS even on loopback (D86): a throwaway self-signed certificate for 127.0.0.1, made at every start, kept in /tmp
T=$(mktemp -d)
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 30 -subj "/CN=127.0.0.1" \
  -addext "subjectAltName=IP:127.0.0.1,DNS:localhost" -keyout "$T/key.pem" -out "$T/cert.pem" 2>/dev/null
RAMEN_MCP_KEYS=$KEY RAMEN_ADMIN_KEY=${RAMEN_ADMIN_KEY:-local-admin} RAMEN_GROUP=demo RAMEN_ZONE=local \
  RAMEN_TLS_CERT="$T/cert.pem" RAMEN_TLS_KEY="$T/key.pem" RAMEN_REQUIRE_TLS=1 ramen-node 1>&2 &
B=/opt/venv/bin/ramen-mcp-bridge
i=0
until $B --target "127.0.0.1:$RAMEN_NODE_PORT" --ca "$T/cert.pem" --health --timeout 2 >/dev/null 2>&1; do
  i=$((i + 1)); [ "$i" -lt 120 ] || { echo "ramen worker did not become ready" >&2; exit 1; }; sleep 0.5
done
exec $B --target "127.0.0.1:$RAMEN_NODE_PORT" --tls --ca "$T/cert.pem" --key "$KEY" --group demo --zone local
