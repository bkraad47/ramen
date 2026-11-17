# Ramen satellite cluster (GCP)

A worker-only GKE Autopilot cluster in another region of the home project, with its own static IP and
Google-managed certificate, driven by the one console in the home cluster (CONTRACTS §28). Apply the home module
(`../gcp`) first; this one shares its Firestore, bucket, registry and service accounts and creates none of them.

The console calls workers by pod IP, so the satellite's network is joined to the home one (§28.1): both clusters sit
on the default VPC, whose VPC-native pod ranges route, and this module adds the firewall rule
`ramen-home-to-satellite-<region>` letting the home pod range (`home_pod_cidr`) in on tcp:8080. Scope it to the
satellite nodes with `node_tag` once the cluster exists (GKE picks the tag's hash at creation).

```sh
cp terraform.tfvars.example terraform.tfvars   # project = the home project, region = the new region,
                                               # home_pod_cidr = gcloud container clusters describe ramen --region <home> --format='value(clusterIpv4Cidr)'
terraform init && terraform apply
terraform output -raw node_tag_command | sh    # gke-<name>-<hash>-node → node_tag in terraform.tfvars, apply again
terraform output -raw kubeconfig_command | sh
kubectl create ns ramen-system && kubectl label ns ramen-system ramen.io/routes=true
helm upgrade --install ramen-satellite ../../helm/ramen-satellite -n ramen-system $(terraform output -raw helm_set)
```

Then register it on the console: **Config → Clusters → Add satellite**, provider `gcp`, the `region`, `cluster_name` and
`host` outputs. Full walk-through: `docs/wiki/deploy-gcp.md`, "Satellite clusters". Keep one state per satellite
(one directory copy or workspace per region).

Teardown: delete the satellite's zones in the console, `helm uninstall ramen-satellite -n ramen-system`, then
`terraform destroy` here. The APIs stay enabled (`disable_on_destroy = false`) for the home cluster.
