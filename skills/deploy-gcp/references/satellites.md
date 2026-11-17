# Satellite clusters on GCP (a zone in another region)

From `docs/wiki/deploy-gcp.md` "Satellite clusters". A satellite is a worker-only Autopilot cluster in another region
of the same project, with its own static IP and Google-managed certificate, driven by the home console. Keep one
Terraform state per satellite (a copy of `deploy/terraform/gcp-satellite` per region).

## Bring-up
```sh
SAT=australia-southeast1
HOME_POD=$(gcloud container clusters describe ramen --region $REGION --format='value(clusterIpv4Cidr)')
cd deploy/terraform/gcp-satellite && cp terraform.tfvars.example terraform.tfvars   # project, region = $SAT, home_pod_cidr = $HOME_POD
terraform init && terraform apply
terraform output -raw node_tag_command | sh        # set node_tag = <that tag> in terraform.tfvars, then apply again
terraform output -raw kubeconfig_command | sh; SCTX=$(kubectl config current-context)
cd ../../..
kubectl --context "$SCTX" create ns ramen-system
kubectl --context "$SCTX" label ns ramen-system ramen.io/routes=true
helm --kube-context "$SCTX" upgrade --install ramen-satellite deploy/helm/ramen-satellite -n ramen-system \
  $(terraform -chdir=deploy/terraform/gcp-satellite output -raw helm_set)
# home chart: the console's identity in every zone RoleBinding, the home pod range into satellite zone policies
helm --kube-context "$CTX" upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values \
  --set-json "console.k8sSubjects=[{\"kind\":\"User\",\"name\":\"ramen-console@$PROJECT.iam.gserviceaccount.com\"}]" \
  --set "console.sourceCidrs={$HOME_POD}"
```
Then, as a super admin, **Config → Clusters → Add satellite** (or `POST /api/v1/config/regions/clusters`): region
`$SAT`, cluster name (`terraform output cluster_name`), host (`terraform output host`). Zones in `$SAT-<letter>` are
then offered; clients of those zones call `https://<satellite host>/mcp`.

Caveats: a satellite zone's judge and wake calls go to the console's public URL; the chart's Jaeger/Phoenix are
cluster-local, so point Config → Telemetry at a collector the satellite can reach.

## Teardown (before the home teardown)
1. Delete every zone in `$SAT` in the console (`DELETE /api/v1/zones/<zone>`) and wait until
   `kubectl --context "$SCTX" get ns` shows no `ramen-<group>-<zone>` namespace.
2. Remove the satellite on **Config → Clusters** (refused with 409 while a zone uses the region).
3. `helm --kube-context "$SCTX" uninstall ramen-satellite -n ramen-system --wait` (releases the satellite's
   Gateway, forwarding rule and static IP use).
4. `terraform -chdir=deploy/terraform/gcp-satellite destroy`.
5. Satellite network endpoint groups can outlive a cluster destroyed while namespaces were terminating:
   the skill's `scripts/leftovers.sh $PROJECT $REGION $SAT` lists them.
