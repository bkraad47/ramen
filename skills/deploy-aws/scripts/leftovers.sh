#!/usr/bin/env bash
# leftovers.sh <region> [satellite region ...]
# Lists (never deletes) what a Ramen install made outside Terraform on AWS, by name only: no secret values, no ARNs
# (they carry the account id). Exit 0 when nothing is left, 1 when something is.
set -uo pipefail
: "${1:?usage: leftovers.sh <region> [satellite region ...]}"
found=0
show() { # <label> <aws args...>: print the names (text output, one per line), count them
  local label=$1 out; shift
  out=$(aws "$@" --output text 2>/dev/null | tr '\t' '\n' | grep -v '^None$' | grep .) || out=""
  printf '== %s: %s\n' "$label" "$(printf '%s' "$out" | grep -c . || true)"
  [ -n "$out" ] && { printf '%s\n' "$out" | sed 's/^/   /'; found=1; }
  return 0
}
show "IAM roles under /ramen/ (zone and build roles)" iam list-roles --path-prefix /ramen/ --query 'Roles[].RoleName'
for r in "$@"; do
  show "$r: console secrets (ramen-console/)" secretsmanager list-secrets --region "$r" --filters Key=name,Values=ramen-console/ --query 'SecretList[].Name'
  show "$r: group secrets (ramen/)" secretsmanager list-secrets --region "$r" --filters Key=name,Values=ramen/ --query 'SecretList[].Name'
  show "$r: group image repositories (ramen/groups/)" ecr describe-repositories --region "$r" --query "repositories[?starts_with(repositoryName,'ramen/groups/')].repositoryName"
  show "$r: EBS volumes tagged app=ramen" ec2 describe-volumes --region "$r" --filters Name=tag:app,Values=ramen --query 'Volumes[].VolumeId'
  show "$r: load balancers (k8s-ramen*)" elbv2 describe-load-balancers --region "$r" --query "LoadBalancers[?starts_with(LoadBalancerName,'k8s-ramen')].LoadBalancerName"
  show "$r: target groups (k8s-ramen*)" elbv2 describe-target-groups --region "$r" --query "TargetGroups[?starts_with(TargetGroupName,'k8s-ramen')].TargetGroupName"
  show "$r: WAF web ACL ramen" wafv2 list-web-acls --scope REGIONAL --region "$r" --query "WebACLs[?Name=='ramen'].Name"
  show "$r: WAF IP sets (ramen-*)" wafv2 list-ip-sets --scope REGIONAL --region "$r" --query "IPSets[?starts_with(Name,'ramen-')].Name"
  show "$r: log groups (/aws/containerinsights/ramen*)" logs describe-log-groups --region "$r" --log-group-name-prefix /aws/containerinsights/ramen --query 'logGroups[].logGroupName'
  show "$r: EKS clusters (ramen*)" eks list-clusters --region "$r" --query "clusters[?starts_with(@,'ramen')]"
done
[ "$found" = 0 ] && echo "nothing left" || echo "leftovers above: delete them only with the human's OK"
exit "$found"
