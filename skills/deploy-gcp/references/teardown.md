# Teardown on GCP, in order

Only on an explicit request. `CTX` is the home cluster's context from the skill's step 4 (re-create it with
`gcloud container clusters get-credentials ramen --region $REGION --project $PROJECT; CTX=$(kubectl config current-context)`
and check `kubectl --context "$CTX" get ns ramen-system` before anything else).

1. **Satellites first**: [satellites.md](satellites.md) "Teardown".
2. **Groups.** As a super admin, delete every group: `DELETE /api/v1/groups/<group>` (or the group page). That
   removes each zone's namespace, service account and IAM bindings, route, Cloud Armor policy, the group's image
   repository `ramen-groups-<group>` and its secrets. Wait until
   `kubectl --context "$CTX" get ns -o name | grep '^namespace/ramen-' | grep -v ramen-system` is empty — after a
   group delete the namespaces can sit `Terminating` on the NEG finalizer for up to about fifteen minutes; the zones'
   network endpoint groups are reaped in the background for up to fifteen minutes (`network endpoint groups
   reaped` in the console log).
3. **The chart, before Terraform.** `helm --kube-context "$CTX" uninstall ramen -n ramen-system --wait`. This
   releases the Gateway's forwarding rule, which holds the static IP: skip it and `terraform destroy` stalls on the
   address. Wait until `gcloud compute forwarding-rules list --project $PROJECT --filter='name~gkegw'` is empty.
4. **Terraform.** `terraform -chdir=deploy/terraform/gcp destroy`. It does not give `roles/editor` back to the
   default compute account (by design).
5. **Leftovers.** `bash <skill>/scripts/leftovers.sh $PROJECT $REGION [satellite regions…]` lists, by name only,
   what the console made outside Terraform: its own secrets `ramenc-…` (label `ramen=console`: the install CA key, identity-provider,
   SMTP, LLM provider and GitHub App secrets), group secrets `ramen-<group>-…`, group image repositories, network
   endpoint groups and backend services named `ramen-…`, Cloud Armor policies, `ramen-*` service accounts, PVC
   disks. Show the list to the human; delete only after their OK (Secret Manager has no recovery window), e.g.
   `gcloud secrets list --filter='labels.ramen=console' --format='value(name)' | xargs -n1 gcloud secrets delete --quiet`.
6. **The project**, if it was a throwaway: `scripts/gcp_test_project.sh delete` (repo script), then
   `scripts/gcp_cost_check.sh $PROJECT --expect-empty`. Move the local `terraform.tfstate` aside afterwards, or the
   next apply plans against a deleted project.

Never print secret values while sweeping: list names only.
