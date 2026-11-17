#!/usr/bin/env bash
# leftovers.sh <region> [satellite region ...]
# Lists (never deletes) what a Ramen install can leave on AWS — what the console made outside Terraform, and what a
# destroy that stopped half-way leaves — by name only: no secret values, no ARNs (they carry the account id).
# Exit 0 when nothing is left, 1 when something is, 2 when AWS could not be asked (then nothing is known).
set -uo pipefail
: "${1:?usage: leftovers.sh <region> [satellite region ...]}"
aws sts get-caller-identity --query Arn --output text >/dev/null 2>&1 \
  || { echo "cannot reach AWS (session expired or no credentials): log in and run again" >&2; exit 2; }
found=0 failed=0
show() { # <label> <aws args...>: print the names (text output, one per line), count them; an API error is a failure
  local label=$1 out; shift
  if ! out=$(aws "$@" --output text 2>&1); then
    printf '== %s: ERROR %s\n' "$label" "$(printf '%s' "$out" | head -1)"; failed=1; return 0
  fi
  out=$(printf '%s' "$out" | tr '\t' '\n' | grep -v '^None$' | grep .) || out=""
  printf '== %s: %s\n' "$label" "$(printf '%s' "$out" | grep -c . || true)"
  [ -n "$out" ] && { printf '%s\n' "$out" | sed 's/^/   /'; found=1; }
  return 0
}
show "IAM roles under /ramen/ (zone and build roles)" iam list-roles --path-prefix /ramen/ --query 'Roles[].RoleName'
show "IAM roles named ramen-* (cluster, nodes, console, controllers)" iam list-roles --query "Roles[?starts_with(RoleName,'ramen')].RoleName"
for r in "$@"; do
  show "$r: EKS clusters (ramen*)" eks list-clusters --region "$r" --query "clusters[?starts_with(@,'ramen')]"
  show "$r: VPCs named ramen*" ec2 describe-vpcs --region "$r" --filters "Name=tag:Name,Values=ramen*" --query 'Vpcs[].VpcId'
  show "$r: Elastic IPs tagged ramen*" ec2 describe-addresses --region "$r" --filters "Name=tag:Name,Values=ramen*" --query 'Addresses[].PublicIp'
  show "$r: load balancers (k8s-ramen*)" elbv2 describe-load-balancers --region "$r" --query "LoadBalancers[?starts_with(LoadBalancerName,'k8s-ramen')].LoadBalancerName"
  show "$r: target groups (k8s-ramen*)" elbv2 describe-target-groups --region "$r" --query "TargetGroups[?starts_with(TargetGroupName,'k8s-ramen')].TargetGroupName"
  show "$r: EBS volumes tagged app=ramen" ec2 describe-volumes --region "$r" --filters Name=tag:app,Values=ramen --query 'Volumes[].VolumeId'
  show "$r: ECR repositories (ramen/)" ecr describe-repositories --region "$r" --query "repositories[?starts_with(repositoryName,'ramen/')].repositoryName"
  show "$r: DynamoDB tables (ramen*)" dynamodb list-tables --region "$r" --query "TableNames[?starts_with(@,'ramen')]"
  show "$r: ACM certificates for the console" acm list-certificates --region "$r" --query "CertificateSummaryList[?starts_with(DomainName,'ramen')].DomainName"
  show "$r: console secrets (ramen-console/)" secretsmanager list-secrets --region "$r" --filters Key=name,Values=ramen-console/ --query 'SecretList[].Name'
  show "$r: group secrets (ramen/)" secretsmanager list-secrets --region "$r" --filters Key=name,Values=ramen/ --query 'SecretList[].Name'
  show "$r: WAF web ACL ramen" wafv2 list-web-acls --scope REGIONAL --region "$r" --query "WebACLs[?Name=='ramen'].Name"
  show "$r: WAF IP sets (ramen-*)" wafv2 list-ip-sets --scope REGIONAL --region "$r" --query "IPSets[?starts_with(Name,'ramen-')].Name"
  show "$r: log groups (/aws/eks/ramen*, /aws/containerinsights/ramen*)" logs describe-log-groups --region "$r" --log-group-name-pattern ramen --query 'logGroups[].logGroupName'
done
show "S3 buckets (ramen-*)" s3api list-buckets --query "Buckets[?starts_with(Name,'ramen-')].Name"
if [ "$failed" = 1 ]; then echo "some lookups failed (above): the list is incomplete"; exit 2; fi
[ "$found" = 0 ] && echo "nothing left" || echo "leftovers above: delete them only with the human's OK"
exit "$found"
