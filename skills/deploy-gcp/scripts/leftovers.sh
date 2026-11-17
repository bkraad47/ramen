#!/usr/bin/env bash
# leftovers.sh <project> <region> [satellite region ...]
# Lists (never deletes) what a Ramen install made outside Terraform on GCP, by name only: no secret values.
# Exit 0 when nothing is left, 1 when something is (so it can gate "teardown done").
set -uo pipefail
P=${1:?usage: leftovers.sh <project> <region> [satellite region ...]}; shift
REGIONS=("${@:-us-central1}")
found=0
show() { # <label> <gcloud args...>: print the names, count them
  local label=$1 out; shift
  out=$(gcloud "$@" --project "$P" 2>/dev/null) || out=""
  printf '== %s: %s\n' "$label" "$(printf '%s' "$out" | grep -c . || true)"
  [ -n "$out" ] && { printf '%s\n' "$out" | sed 's/^/   /'; found=1; }
  return 0
}
show "console secrets (label ramen=console)" secrets list --filter='labels.ramen=console' --format='value(name)'
show "group secrets (ramen-<group>-...)" secrets list --filter='name~/secrets/ramen-' --format='value(name)'
for r in "${REGIONS[@]}"; do
  show "group image repositories in $r" artifacts repositories list --location="$r" --filter='name~ramen-groups-' --format='value(name)'
done
show "network endpoint groups (ramen-...)" compute network-endpoint-groups list --filter='name~^ramen-' --format='value(name,zone.basename())'
show "backend services (zone backends)" compute backend-services list --filter='name~ramen-' --format='value(name)'
show "Gateway forwarding rules" compute forwarding-rules list --filter='name~gkegw' --format='value(name)'
show "Cloud Armor policies (ramen-<group>)" compute security-policies list --filter='name~^ramen-' --format='value(name)'
show "service accounts (ramen-*)" iam service-accounts list --filter='email~^ramen-' --format='value(email)'
show "PVC disks" compute disks list --filter='name~^pvc-' --format='value(name,zone.basename())'
show "GKE clusters (ramen*)" container clusters list --filter='name~^ramen' --format='value(name,location)'
[ "$found" = 0 ] && echo "nothing left" || echo "leftovers above: delete them only with the human's OK"
exit "$found"
