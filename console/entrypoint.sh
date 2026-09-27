#!/bin/sh
set -e
PORT="${RAMEN_CONSOLE_PORT:-8000}"
PY="$(command -v python || command -v python3)"
if [ "${RAMEN_TLS:-}" = "self" ]; then
  "$PY" -m ramen_console.tls /tmp/ramen-tls "${RAMEN_TLS_HOST:-localhost}" >/dev/null
  exec uvicorn ramen_console.app:app --host 0.0.0.0 --port "${RAMEN_TLS_PORT:-8443}" \
    --ssl-keyfile /tmp/ramen-tls/key.pem --ssl-certfile /tmp/ramen-tls/cert.pem --proxy-headers "$@"
fi
exec uvicorn ramen_console.app:app --host 0.0.0.0 --port "$PORT" --proxy-headers "$@"
