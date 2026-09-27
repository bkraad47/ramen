#!/usr/bin/env bash
# gcp_cost_check.sh [project] [--expect-empty]  — list billable resources so teardown can be confirmed.
# Covers: GKE clusters, LB forwarding rules, backend services, static addresses, Cloud Armor policies, GCS buckets,
# Artifact Registry repos, Secret Manager secrets, Firestore DBs, service accounts (ramen-*), persistent disks.
# Exit 0 always, unless --expect-empty and anything billable remains (exit 1).
set -uo pipefail
PROJECT=${RAMEN_GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}; EXPECT_EMPTY=0
for a in "$@"; do case "$a" in --expect-empty) EXPECT_EMPTY=1 ;; *) PROJECT=$a ;; esac; done
[ -n "$PROJECT" ] || { echo "usage: gcp_cost_check.sh <project> [--expect-empty]" >&2; exit 2; }
if ! gcloud projects describe "$PROJECT" >/dev/null 2>&1; then echo "project $PROJECT: not found or already deleted (nothing billable)"; exit 0; fi
total=0
section() { # <label> <billable 0|1> <gcloud args...>
  local label=$1 billable=$2; shift 2
  local out; out=$(gcloud "$@" --project="$PROJECT" --format="value(name)" 2>/dev/null) || out="(error listing)"
  local n; n=$(printf '%s' "$out" | grep -c . || true)
  printf '%-22s %3s  %s\n' "$label" "$n" "$(printf '%s' "$out" | tr '\n' ' ' | cut -c1-120)"
  [ "$billable" = 1 ] && total=$((total + n)) || true
}
echo "== billable resources in $PROJECT ($(date -u +%FT%TZ))"
section "GKE clusters"         1 container clusters list
section "Forwarding rules"     1 compute forwarding-rules list
section "Backend services"     1 compute backend-services list
section "Static addresses"     1 compute addresses list
section "Persistent disks"     1 compute disks list
section "Cloud Armor policies" 0 compute security-policies list
section "GCS buckets"          1 storage buckets list
section "AR repositories"      1 artifacts repositories list --location="${RAMEN_GCP_REGION:-us-central1}"
section "SM secrets"           1 secrets list
section "Firestore DBs"        1 firestore databases list
section "Service accounts"     0 iam service-accounts list --filter="email~^ramen-"
echo "== billable objects: $total"
if [ "$EXPECT_EMPTY" = 1 ] && [ "$total" -gt 0 ]; then echo "FAIL: resources remain in $PROJECT" >&2; exit 1; fi
exit 0
