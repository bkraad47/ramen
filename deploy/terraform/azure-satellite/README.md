# Ramen satellite cluster (Azure)

A worker-only AKS cluster in another location of the home subscription, with its own static public IP and Envoy
Gateway edge, driven by the one console in the home cluster (CONTRACTS §28). Apply the home module (`../azure`)
first; this one shares its ACR, storage account, Key Vault and identities and creates none of them. The cluster is
Entra-integrated so the console's workload identity is a Kubernetes `User` (its object id), which the satellite
chart binds to the console ClusterRoles; two role assignments on the cluster (Cluster User Role, Reader) let the
console fetch its endpoint and credentials.

The console calls workers by pod IP, so the satellite's network is joined to the home one (§28.1). The home cluster
is CNI overlay on an AKS-managed VNet (`aks-vnet-<n>` in its node resource group, 10.224.0.0/12), so the satellite
gets its own VNet (`vnet_cidr`, 10.2.0.0/16, non-overlapping) and runs Azure CNI without overlay: its pods carry
VNet addresses, the two VNets are peered both ways (`allow_forwarded_traffic`), and an NSG on the nodes subnet lets
the home VNet's address space in on tcp:8080. Inputs from the home apply: `home_vnet_id`, `home_vnet_name`,
`home_vnet_resource_group` (outputs of the same names). The cluster uses a user-assigned identity with Network
Contributor on its VNet (a bring-your-own subnet needs that before the cluster exists).

```sh
cp terraform.tfvars.example terraform.tfvars   # subscription_id, location, console_principal_id, acr_id, home_vnet_*
terraform init && terraform apply
$(terraform output -raw kubeconfig_command)
kubectl create ns ramen-system && kubectl label ns ramen-system ramen.io/routes=true
helm upgrade --install ramen-satellite ../../helm/ramen-satellite -n ramen-system $(terraform output -raw helm_set)
terraform output -raw public_hostname
```

Then register it on the console: **Config → Clusters → Add satellite**, provider `azure`, the `location`, `cluster` and
`public_hostname` outputs (the resource group goes in the record's `project` field). Full walk-through:
`docs/wiki/deploy-azure.md`, "Satellite clusters". Keep one state per satellite.

Teardown: delete the satellite's zones in the console, `helm uninstall ramen-satellite -n ramen-system`, then
`terraform destroy` here (removes the resource group it created, never one it reused).
