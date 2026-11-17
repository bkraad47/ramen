# Teardown on Azure, in order

Only on an explicit request. Check `az account show --query name -o tsv` and that `CTX` is the home cluster
(`$(terraform -chdir=deploy/terraform/azure output -raw kubeconfig_command) --file "$KUBECONFIG"; CTX=$(kubectl config current-context)`).

1. **Satellites first**: [satellites.md](satellites.md) "Teardown".
2. **Groups.** As a super admin, `DELETE /api/v1/groups/<group>` for each: removes the zone namespaces, zone and
   build identities with their federated credentials and role assignments, routes and Envoy policies, the group's
   repository `ramen/groups/<group>/worker` and its Key Vault secrets. Wait until no `ramen-<group>-<zone>`
   namespace is left (`kubectl --context "$CTX" get ns`).
3. **The chart, before Terraform.** `helm --kube-context "$CTX" uninstall ramen -n ramen-system --wait`. The Envoy
   Gateway Service binds the static public IP `ramen-edge` in the node resource group; skip this and `terraform
   destroy` stalls on the IP. Check with `kubectl --context "$CTX" -n envoy-gateway-system get svc` (no
   `LoadBalancer` left for the `ramen` Gateway).
4. **Terraform.** `terraform -chdir=deploy/terraform/azure destroy`. It removes the resource group (cluster, ACR,
   storage, Managed Redis), the node resource group with the IP, and purges the Key Vault — the console's own
   secrets (`ramenc--…`, tag `ramen=console`) and every group secret live there and go with it.
5. **Leftovers.** `bash <skill>/scripts/leftovers.sh` lists `ramen*` and `MC_ramen*` resource groups (satellites
   included) and soft-deleted `ramen*` Key Vaults. Nothing should remain; show anything that does to the human and
   delete only with their OK.

Never print secret values; list names only.
