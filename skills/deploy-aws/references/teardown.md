# Teardown on AWS, in order

Only on an explicit request. Check `aws sts get-caller-identity` is the install's account and `CTX` is the home
cluster (`aws eks update-kubeconfig --name ramen --region $REGION; CTX=$(kubectl config current-context)`).

1. **Satellites first**: [satellites.md](satellites.md) "Teardown".
2. **Groups.** As a super admin, `DELETE /api/v1/groups/<group>` for each: removes the zone namespaces and their
   Ingresses, the zone IAM roles, the group's build role and ECR repository `ramen/groups/<group>/worker`, its WAF IP
   sets and its secrets. Wait until no `ramen-<group>-<zone>` namespace is left
   (`kubectl --context "$CTX" get ns`).
3. **The chart, before Terraform.** `helm --kube-context "$CTX" uninstall ramen -n ramen-system --wait`. The Load
   Balancer Controller deletes the ALB and its target groups, and the EBS CSI driver deletes the PVCs' volumes,
   while both still run. Skip this and `terraform destroy` stalls on the VPC (the ALB's interfaces and security
   groups). Wait until
   `aws elbv2 describe-load-balancers --region $REGION --query "LoadBalancers[?starts_with(LoadBalancerName,'k8s-ramen')].LoadBalancerName" --output text`
   is empty.
4. **Terraform.** `terraform -chdir=deploy/terraform/aws destroy`. It sweeps a terminated node's leftover interface
   and the `eks-cluster-sg-ramen-…` group itself; if it still stops on `DependencyViolation`, run
   `bash deploy/terraform/aws/scripts/eni_sweep.sh <vpc id> $REGION` (and `… cluster-sg ramen`) and destroy again.
5. **Leftovers.** `bash <skill>/scripts/leftovers.sh $REGION [satellite regions…]` lists by name: the console's own
   secrets `ramen-console/…` (identity-provider, SMTP, LLM provider and GitHub App secrets, the install CA's key),
   group secrets `ramen/…`, IAM roles under `/ramen/` (zone roles; `/ramen/build/` build roles), ECR
   `ramen/groups/…`, EBS volumes tagged `app=ramen`, ALBs and target groups `k8s-ramen…`, the console's WAF web ACL
   `ramen` and IP sets (billed monthly, not Terraform's), CloudWatch log groups. Show it to the human; delete after
   their OK. Secrets: the default 30-day recovery window (restorable), and only when no other Ramen install shares
   the account and region:
   `for id in $(aws secretsmanager list-secrets --region $REGION --filters Key=name,Values=ramen-console/ --query 'SecretList[].Name' --output text); do aws secretsmanager delete-secret --region $REGION --secret-id "$id"; done`.
   A leftover zone role: delete its inline policies, then `aws iam delete-role`. The web ACL:
   `aws wafv2 delete-web-acl --scope REGIONAL --name ramen --id <id> --lock-token <token> --region <region>`.

List names only; never print a secret value or paste the account id.
