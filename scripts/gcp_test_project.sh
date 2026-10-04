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
# `delete` never needs the billing account (unlink takes none), and demanding it there means teardown fails
# for whoever did not keep the id — which is exactly when a project must be easy to delete.
[ "$cmd" = create ] && BILLING="${RAMEN_BILLING_ACCOUNT:?set RAMEN_BILLING_ACCOUNT to your billing account id}"
BILLING="${BILLING:-}"
APIS="container.googleapis.com firestore.googleapis.com secretmanager.googleapis.com storage.googleapis.com"

run() { if [ "${DRY_RUN:-0}" = "1" ]; then echo "+ $*"; else echo "+ $*" >&2; "$@"; fi; }

case "$cmd" in
  create)
    # A torn-down project keeps its id for 30 days (DELETE_REQUESTED) and describes fine, but cannot be billed:
    # same-day re-runs hit it with the default id.
    state="$([ "${DRY_RUN:-0}" = "1" ] || gcloud projects describe "$PROJECT" --format='value(lifecycleState)' 2>/dev/null || true)"
    if [ -n "$state" ] && [ "$state" != ACTIVE ]; then
      echo "project $PROJECT is $state (deleted ids stay reserved for 30 days); set RAMEN_GCP_PROJECT=${PROJECT}b" >&2
      exit 1
    elif [ -n "$state" ]; then
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
