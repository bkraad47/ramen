#!/usr/bin/env bash
# wait_ready.sh <url|host:port> [timeout_secs=120] [interval_secs=2] [--any]
#   http(s)://…   poll until HTTP 200 (self-signed TLS accepted)
#   host:port     poll grpc.health.v1.Health/Check (CONTRACTS §11) until SERVING; with --any, until it answers at all
#                 (NOT_SERVING is fine: a fresh worker before its first deploy). RAMEN_NODE_TLS=1 → TLS (self-signed ok).
# gRPC mode uses grpcurl when installed, else python3 with grpcio (tests/.venv or any interpreter that has grpcio).
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
target="${1:?usage: wait_ready.sh <url|host:port> [timeout] [interval] [--any]}"; timeout="${2:-120}"; interval="${3:-2}"
any=0; for a in "$@"; do [ "$a" = "--any" ] && any=1; done
[ "$timeout" = "--any" ] && timeout=120; [ "$interval" = "--any" ] && interval=2
deadline=$(( $(date +%s) + timeout ))

grpc_health() {  # prints SERVING / NOT_SERVING / UNKNOWN / "" (unreachable)
  local t="${target#grpc://}"; t="${t#grpcs://}"
  local tls=(-plaintext); [ "${RAMEN_NODE_TLS:-0}" = 1 ] && tls=(-insecure)
  if command -v grpcurl >/dev/null; then
    grpcurl "${tls[@]}" -max-time 5 -import-path "$here/../tests/proto" -proto grpc/health/v1/health.proto "$t" grpc.health.v1.Health/Check 2>/dev/null \
      | python3 -c 'import sys,json; print(json.load(sys.stdin).get("status","UNKNOWN"))' 2>/dev/null || true
  else
    local py=python3; [ -x "$here/../tests/.venv/bin/python" ] && py="$here/../tests/.venv/bin/python"
    RAMEN_NODE_TLS="${RAMEN_NODE_TLS:-0}" "$py" - "$t" <<'PY' 2>/dev/null || true
import os, ssl, sys, grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc
t = sys.argv[1]
if os.environ.get("RAMEN_NODE_TLS") == "1":
    h, _, p = t.rpartition(":")
    ch = grpc.secure_channel(t, grpc.ssl_channel_credentials(ssl.get_server_certificate((h, int(p))).encode()))
else:
    ch = grpc.insecure_channel(t)
r = health_pb2_grpc.HealthStub(ch).Check(health_pb2.HealthCheckRequest(), timeout=5)
print(health_pb2.HealthCheckResponse.ServingStatus.Name(r.status))
PY
  fi
}

case "$target" in
  http://*|https://*)
    while :; do
      code=$(curl -ks -o /dev/null -w '%{http_code}' --max-time 5 "$target" || true)
      if [ "$code" = "200" ]; then echo "ready: $target"; exit 0; fi
      if [ "$(date +%s)" -ge "$deadline" ]; then echo "timeout after ${timeout}s waiting for $target (last=$code)" >&2; exit 1; fi
      sleep "$interval"
    done ;;
  *)
    while :; do
      st=$(grpc_health)
      if [ "$st" = "SERVING" ] || { [ $any = 1 ] && [ -n "$st" ]; }; then echo "ready: $target ($st)"; exit 0; fi
      if [ "$(date +%s)" -ge "$deadline" ]; then echo "timeout after ${timeout}s waiting for $target health (last=${st:-unreachable})" >&2; exit 1; fi
      sleep "$interval"
    done ;;
esac
