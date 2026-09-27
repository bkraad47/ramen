#!/usr/bin/env bash
# wait_ready.sh <url> [timeout_secs=120] [interval_secs=2]  — poll until HTTP 200 (self-signed TLS accepted).
set -euo pipefail
url="${1:?usage: wait_ready.sh <url> [timeout] [interval]}"; timeout="${2:-120}"; interval="${3:-2}"
deadline=$(( $(date +%s) + timeout ))
while :; do
  code=$(curl -ks -o /dev/null -w '%{http_code}' --max-time 5 "$url" || true)
  if [ "$code" = "200" ]; then echo "ready: $url"; exit 0; fi
  if [ "$(date +%s)" -ge "$deadline" ]; then echo "timeout after ${timeout}s waiting for $url (last=$code)" >&2; exit 1; fi
  sleep "$interval"
done
