#!/usr/bin/env bash
# eni_sweep.sh <vpc id> <region> [cluster-sg <cluster name>] — run by `terraform destroy` (0.7.5 AWS teardown).
# Default: after the node group is gone, before the subnets. The VPC CNI can leave a terminated node's interface
# (`aws-K8S-i-…`) behind; it blocks DeleteSubnet with DependencyViolation. Wait for those to detach, then delete them.
# Never touches other interfaces (ALB, EKS control plane).
# cluster-sg: after the cluster is gone, before the VPC. EKS can leave `eks-cluster-sg-<name>-<n>` behind, which blocks
# DeleteVpc. Deletes only that group, retrying while its last interfaces drain.
set -uo pipefail
VPC=${1:?vpc id}; REGION=${2:?region}; TRIES=${RAMEN_ENI_WAIT_TRIES:-20}
if [ "${3:-}" = cluster-sg ]; then
  NAME=${4:?cluster name}
  for i in $(seq 1 "$TRIES"); do
    ids=$(aws ec2 describe-security-groups --region "$REGION" --filters "Name=vpc-id,Values=$VPC" \
      --query 'SecurityGroups[].[GroupId,GroupName]' --output text | awk -F'\t' -v p="eks-cluster-sg-$NAME-" 'index($2, p) == 1 {print $1}')
    [ -z "$ids" ] && exit 0
    left=0
    for id in $ids; do
      aws ec2 delete-security-group --region "$REGION" --group-id "$id" >/dev/null 2>&1 && echo "deleted leftover $id" || left=1
    done
    [ "$left" = 0 ] && exit 0
    [ "$i" -lt "$TRIES" ] && sleep 15
  done
  echo "EKS cluster security group still in $VPC after $TRIES tries; destroy may need a second run" >&2
  exit 0
fi
list() {
  aws ec2 describe-network-interfaces --region "$REGION" --filters "Name=vpc-id,Values=$VPC" \
    --query 'NetworkInterfaces[].[NetworkInterfaceId,Status,Description]' --output text
}
for i in $(seq 1 "$TRIES"); do
  rows=$(list | awk -F'\t' '$3 ~ /^aws-K8S-/')
  for id in $(echo "$rows" | awk -F'\t' '$2 == "available" {print $1}'); do
    aws ec2 delete-network-interface --region "$REGION" --network-interface-id "$id" && echo "deleted leftover CNI interface $id"
  done
  echo "$rows" | awk -F'\t' '$2 == "in-use"' | grep -q . || exit 0
  [ "$i" -lt "$TRIES" ] && sleep 15  # an in-use one detaches within a minute or two of its node terminating
done
echo "CNI interfaces still attached in $VPC after $TRIES checks; destroy may need a second run" >&2
