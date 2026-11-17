# Satellite clusters on AWS (a zone in another region)

From `docs/wiki/deploy-aws.md` "Satellite clusters". A worker-only EKS cluster in another region of the same
account, peered with the home VPC, with its own ALB and self-signed certificate. One Terraform state per satellite.

## Bring-up
```sh
SAT=eu-west-1
terraform -chdir=deploy/terraform/aws output -raw vpc_id          # → home_vpc_id
cd deploy/terraform/aws-satellite && cp terraform.tfvars.example terraform.tfvars   # region = $SAT, home_region, home_vpc_id
terraform init && terraform apply
$(terraform output -raw kubeconfig_command); SCTX=$(kubectl config current-context)
cd ../../..
kubectl --context "$SCTX" create ns ramen-system
kubectl --context "$SCTX" label ns ramen-system ramen.io/routes=true
helm --kube-context "$SCTX" upgrade --install ramen-satellite deploy/helm/ramen-satellite -n ramen-system \
  $(terraform -chdir=deploy/terraform/aws-satellite output -raw helm_set)
kubectl --context "$SCTX" -n ramen-system get ingress edge        # ADDRESS = the satellite host, after a few minutes
aws acm get-certificate --certificate-arn $(terraform -chdir=deploy/terraform/aws-satellite output -raw certificate_arn) \
  --region $SAT --query Certificate --output text > ramen-$SAT.pem
# home chart: the access entry's group in every zone RoleBinding, and the satellite's ALB subnets in the zone policies
helm --kube-context "$CTX" upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values \
  --set-json 'console.k8sSubjects=[{"kind":"Group","name":"ramen-console"}]' \
  --set console.env.RAMEN_AWS_ALB_CIDRS=<home alb_cidrs>\,<satellite alb_cidrs>    # commas escaped for --set
```
Then **Config → Clusters → Add satellite**: region `$SAT`, cluster name (`terraform output cluster_name`), the Ingress
address as host. Redeploy the satellite's zones after any change to `RAMEN_AWS_ALB_CIDRS`.

Caveat: a satellite zone's judge and wake calls go to the console's public URL, which the default self-signed ALB
certificate does not satisfy; that needs a publicly trusted certificate on the console's ALB.

## Teardown (before the home teardown)
1. Delete the satellite's zones in the console; wait until `kubectl --context "$SCTX" get ns` has no zone namespace.
2. Remove it on **Config → Clusters** (409 while a zone uses the region).
3. `helm --kube-context "$SCTX" uninstall ramen-satellite -n ramen-system --wait` (releases its ALB).
4. `terraform -chdir=deploy/terraform/aws-satellite destroy` (runs the same interface and security-group sweeps).
5. The console's web ACL `ramen` in `$SAT` is not Terraform's: the skill's `scripts/leftovers.sh $REGION $SAT`
   lists it.
