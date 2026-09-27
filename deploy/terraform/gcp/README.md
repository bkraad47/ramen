# deploy/terraform/gcp — Phase 2 (v0.2.0), skeleton only
Creates: regional GKE Autopilot cluster, Firestore (native), one GCS bucket per group, Secret Manager
secret for the Fernet key, console + per-group/zone worker service accounts, and a global external
HTTPS load balancer whose backend is the per-zone worker NEGs created by the Helm chart.

```
terraform init && terraform validate
terraform apply -var project=<id> -var domain=mcp.example.com
helm upgrade --install ramen ../../helm/ramen --set image.tag=$(cat ../../../VERSION)
# then: terraform apply -var 'worker_negs={"<zone>"="<neg name from kubectl get svc -o yaml>"}'
```
Not yet applied to a real project. Costs: Autopilot regional cluster is the main line item; delete with `terraform destroy`.
