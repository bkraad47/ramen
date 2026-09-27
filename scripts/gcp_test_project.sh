#!/usr/bin/env bash
# Throwaway GCP project for cloud test runs (F12.2, D7).
#   gcp_test_project.sh create   → creates ramen-test-<yymmdd>, links billing, enables APIs, prints PROJECT_ID
#   gcp_test_project.sh delete   → deletes it (irreversible after the 30-day grace period)
# Env: RAMEN_GCP_PROJECT (override id), RAMEN_BILLING_ACCOUNT (default Raads Billing), DRY_RUN=1 (print only).
set -euo pipefail
cmd="${1:?usage: gcp_test_project.sh create|delete}"
STATE="${RAMEN_GCP_PROJECT_FILE:-$HOME/.ramen-test-project}"   # remembers the id so `delete` works on a later day
if [ -n "${RAMEN_GCP_PROJECT:-}" ]; then PROJECT="$RAMEN_GCP_PROJECT"
elif [ "$1" = "delete" ] && [ -s "$STATE" ]; then PROJECT="$(cat "$STATE")"
else PROJECT="ramen-test-$(date +%y%m%d)"; fi
BILLING="${RAMEN_BILLING_ACCOUNT:-012374-5439A5-74606A}"
APIS="container.googleapis.com firestore.googleapis.com secretmanager.googleapis.com storage.googleapis.com"

run() { if [ "${DRY_RUN:-0}" = "1" ]; then echo "+ $*"; else echo "+ $*" >&2; "$@"; fi; }

case "$cmd" in
  create)
    if [ "${DRY_RUN:-0}" != "1" ] && gcloud projects describe "$PROJECT" >/dev/null 2>&1; then
      echo "project $PROJECT already exists" >&2
    else
      run gcloud projects create "$PROJECT" --name="ramen test $(date +%F)" --labels=purpose=ramen-test,ephemeral=true
    fi
    run gcloud billing projects link "$PROJECT" --billing-account="$BILLING"
    # shellcheck disable=SC2086
    run gcloud services enable $APIS --project="$PROJECT"
    [ "${DRY_RUN:-0}" = "1" ] || echo "$PROJECT" > "$STATE"
    echo "$PROJECT"
    [ -n "${GITHUB_OUTPUT:-}" ] && echo "project=$PROJECT" >> "$GITHUB_OUTPUT" || true
    ;;
  delete)
    run gcloud billing projects unlink "$PROJECT" || true
    run gcloud projects delete "$PROJECT" --quiet
    echo "deleted $PROJECT"
    [ -s "$STATE" ] && [ "$(cat "$STATE")" = "$PROJECT" ] && rm -f "$STATE"
    ;;
  *) echo "unknown command: $cmd" >&2; exit 2 ;;
esac
