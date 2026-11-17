# Satellite clusters on Azure (a zone in another location)

From `docs/wiki/deploy-azure.md` "Satellite clusters". A worker-only AKS cluster in another location of the same
subscription, with its own VNet (`vnet_cidr`, default `10.2.0.0/16`, peered both ways with the home AKS VNet), its
own static IP and Envoy Gateway edge, and Calico. One Terraform state per satellite.

## Bring-up
```sh
SAT=westeurope; TF="terraform -chdir=deploy/terraform/azure"; HOME_RG=$($TF output -raw resource_group)
az identity show -g $HOME_RG -n ramen-console --query principalId -o tsv               # console_principal_id
az acr show -n $($TF output -raw acr | cut -d. -f1) --query id -o tsv                   # acr_id
for o in vnet_id vnet_name vnet_resource_group; do $TF output -raw $o; echo; done      # home_vnet_*
cd deploy/terraform/azure-satellite && cp terraform.tfvars.example terraform.tfvars    # subscription_id, location = $SAT, the values above
terraform init && terraform apply
$(terraform output -raw kubeconfig_command); SCTX=$(kubectl config current-context)    # admin credentials
cd ../../..
kubectl --context "$SCTX" create ns ramen-system
kubectl --context "$SCTX" label ns ramen-system ramen.io/routes=true
helm --kube-context "$SCTX" upgrade --install ramen-satellite deploy/helm/ramen-satellite -n ramen-system \
  $(terraform -chdir=deploy/terraform/azure-satellite output -raw helm_set)
kubectl --context "$SCTX" -n ramen-system get certificate edge                         # READY True
```
These print ids, not secrets; keep them out of the report all the same. Then **Config → Clusters → Add
satellite**: region `$SAT`, cluster name (`ramen` by default), the `public_hostname` output as host, and the
satellite's resource group (`terraform output resource_group`) in *Resource group (if not the console's)*. Zones
`$SAT-<n>` are then offered.

## Teardown (before the home teardown)
1. Delete the satellite's zones in the console; wait until `kubectl --context "$SCTX" get ns` has no zone namespace.
2. Remove it on **Config → Clusters**.
3. `helm --kube-context "$SCTX" uninstall ramen-satellite -n ramen-system --wait` (releases the edge's public IP).
4. `terraform -chdir=deploy/terraform/azure-satellite destroy` (removes the resource group it created, never one it
   reused).
