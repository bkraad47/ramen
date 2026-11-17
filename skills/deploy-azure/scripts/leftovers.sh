#!/usr/bin/env bash
# leftovers.sh — lists (never deletes) what a Ramen install can leave on Azure after `terraform destroy`, by name only.
# Everything Ramen makes lives in its resource groups, so a clean teardown leaves none of them.
# Exit 0 when nothing is left, 1 when something is.
set -uo pipefail
found=0
show() { # <label> <az args...>
  local label=$1 out; shift
  out=$(az "$@" -o tsv 2>/dev/null | grep .) || out=""
  printf '== %s: %s\n' "$label" "$(printf '%s' "$out" | grep -c . || true)"
  [ -n "$out" ] && { printf '%s\n' "$out" | sed 's/^/   /'; found=1; }
  return 0
}
show "resource groups (ramen*, MC_ramen*)" group list --query "[?starts_with(name,'ramen') || starts_with(name,'MC_ramen')].name"
show "soft-deleted Key Vaults (ramen*)" keyvault list-deleted --query "[?starts_with(name,'ramen')].name"
show "AKS clusters (ramen*)" aks list --query "[?starts_with(name,'ramen')].name"
[ "$found" = 0 ] && echo "nothing left" || echo "leftovers above: delete them only with the human's OK"
exit "$found"
