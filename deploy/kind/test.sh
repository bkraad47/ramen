#!/usr/bin/env bash
# Run the v0.4.0 proofs against the running kind cluster (CONTRACTS §12.3).
#   tests/kind/            multi-zone, autoscale, rebalance, per-zone packages
#   tests/e2e + conformance  the standard harness against zone a
# Cloud Logging, Cloud Armor, compute and IAM have no kind equivalent. RAMEN_NO_CLOUD names them, and the cases that
# need one skip with a reason instead of failing. It is a kind-only switch: never set it for a cloud run.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
export RAMEN_CONSOLE_URL=${RAMEN_CONSOLE_URL:-http://localhost:18000}
export RAMEN_ADMIN_EMAIL=${RAMEN_ADMIN_EMAIL:-admin@ramen.local}
export RAMEN_ADMIN_PASSWORD=${RAMEN_ADMIN_PASSWORD:-Kind-Console-1!}
export RAMEN_ADMIN_KEY=${RAMEN_ADMIN_KEY:-Kind-Admin-Key-1!}
export RAMEN_KIND_ZONES=${RAMEN_KIND_ZONES:-a=localhost:18081,b=localhost:18082}
export RAMEN_KIND_NODE_ZONES=${RAMEN_KIND_NODE_ZONES:-a=kind-a,b=kind-b}
export RAMEN_KIND_STABLE_ZONES=${RAMEN_KIND_STABLE_ZONES:-a=localhost:18084,b=localhost:18085}
export RAMEN_E2E_GROUP=${RAMEN_E2E_GROUP:-demo}
export RAMEN_E2E_ENV=${RAMEN_E2E_ENV:-dev}
export RAMEN_E2E_ZONE=${RAMEN_E2E_ZONE:-a}
export RAMEN_ZONE_PROVIDER=${RAMEN_ZONE_PROVIDER:-gcp}
export RAMEN_ZONE_REGION=${RAMEN_ZONE_REGION:-kind-a}
export RAMEN_NODE_URL=${RAMEN_NODE_URL:-localhost:18081}
export KIND_CLUSTER=${KIND_CLUSTER:-ramen}
export RAMEN_NO_CLOUD=${RAMEN_NO_CLOUD:-iam,logs,armor}
# the security matrix is run on its own by `make kind-test`, so its destructive auth-config case is safe here
export RAMEN_ALLOW_SESSION_RESET=${RAMEN_ALLOW_SESSION_RESET:-0}
cd "$ROOT/tests"
uv sync -q
# RAMEN_KIND_SUITES trims the run (CI uses "kind": the §12.3 proofs; e2e+conformance already run
# against the compose stack in the ci.yml `e2e` job).
# shellcheck disable=SC2086  # deliberate: RAMEN_KIND_SUITES is a list of pytest targets
exec uv run pytest -rs "$@" ${RAMEN_KIND_SUITES:-kind e2e conformance}
