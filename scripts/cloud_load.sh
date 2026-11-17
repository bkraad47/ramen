#!/usr/bin/env bash
# cloud_load.sh <gcp|aws|azure> [pytest args]  — CONTRACTS §35: the four load scenarios (tests/load) against one cloud,
# their markdown table appended under "## Load tests — <cloud> (<date>, console <version>)" in the v1.0.0c report.
#
# Standalone: export RAMEN_CONSOLE_URL RAMEN_NODE_URL RAMEN_ADMIN_EMAIL RAMEN_ADMIN_PASSWORD RAMEN_E2E_GROUP RAMEN_E2E_ENV
# RAMEN_E2E_ZONE RAMEN_NODE_TLS (RAMEN_NODE_CA for an edge with its own certificate) and run it. Without
# RAMEN_CONSOLE_URL it hands the run to the session's driver (RAMEN_CLOUD_DRIVER, a release operator's cloud driver script):
# `cloud2.sh <cloud> verify load …`, which exports the same variables from its <cloud>-final.env.
# Knobs: RAMEN_LOAD_IDLE_SECS (scale-down wait; default here 480 — 4→1 is three −1 steps of a 120 s cooldown),
# RAMEN_LOAD_FAST=1 (dry run), RAMEN_PHOENIX_URL (port-forward of svc/phoenix 6006 for the span delta; D92: with
# RAMEN_PHOENIX_ADMIN_SECRET = Secret ramen-engines `phoenix-admin-secret`, Phoenix's auth is on),
# RAMEN_LOAD_REPORT_MD (the report file), RAMEN_LOAD_KEY (a group key the workers already know: skips the deploy).
set -uo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
CLOUD=${1:?cloud (gcp|aws|azure)}; shift
MD=${RAMEN_LOAD_REPORT_MD:-$ROOT/../reports/test-status-v1.0.0c.md}
DRIVER=${RAMEN_CLOUD_DRIVER:-}
TABLE=$(mktemp)
export RAMEN_LOAD=1 RAMEN_LOAD_REPORT=$TABLE RAMEN_LOAD_IDLE_SECS=${RAMEN_LOAD_IDLE_SECS:-480}
START=$(date +%s)
if [ -n "${RAMEN_CONSOLE_URL:-}" ]; then
  URL=${RAMEN_CONSOLE_URL%/}; CA=${RAMEN_NODE_CA:-}
  (cd "$ROOT/tests" && uv run pytest -q -rs load "$@"); RC=$?
elif [ -n "$DRIVER" ] && [ -x "$DRIVER" ]; then
  HOST=$(. "$(dirname "$DRIVER")/$CLOUD-final.env" 2>/dev/null; echo "${HOST:-}"); URL=https://$HOST
  CA="$(dirname "$DRIVER")/aws-lb.pem"; [ "$CLOUD" = aws ] && [ -f "$CA" ] || CA=""
  "$DRIVER" "$CLOUD" verify load "$@"; RC=$?
else
  echo "set RAMEN_CONSOLE_URL (+ RAMEN_NODE_URL, admin creds, RAMEN_E2E_*) or RAMEN_CLOUD_DRIVER" >&2; exit 2
fi
CURL=(curl -s --max-time 20); [ -n "$CA" ] && CURL+=(--cacert "$CA")
TAG=$("${CURL[@]}" "$URL/healthz" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("version", "?"))' 2>/dev/null || echo "?")
MINS=$(( ($(date +%s) - START) / 60 ))
{
  echo; echo "## Load tests — $CLOUD ($(date +%Y-%m-%d), console $TAG)"; echo
  echo "\`scripts/cloud_load.sh $CLOUD${*:+ $*}\` — $MINS min, pytest exit $RC, idle wait ${RAMEN_LOAD_IDLE_SECS} s, Phoenix ${RAMEN_PHOENIX_URL:-not queried}."; echo
  if [ -s "$TABLE" ]; then cat "$TABLE"; else echo "_no scenario produced a row (see the pytest output)_"; fi
} >> "$MD"
echo "appended to $MD (exit $RC)"; rm -f "$TABLE"; exit $RC
