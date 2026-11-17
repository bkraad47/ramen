# deploy/ — running Ramen

The step-by-step guides live on the docs site; this page only says what each directory is.

| dir | what | guide |
|---|---|---|
| `local/` | docker compose stack: console, one worker, optional engines (`make up ENGINES=phoenix,promptfoo,redis`); every hop TLS with a local CA (`local/.certs`) | [Get started](https://bkraad47.github.io/ramen/get-started/) |
| `kind/` | a local kind cluster with two zones for the multi-zone, autoscale, sleep and build proofs (`make kind-up`, `make kind-test`) | `kind/README.md` |
| `terraform/gcp/` | GKE Autopilot, Firestore, GCS, Artifact Registry, Secret Manager, static IP, Google-managed certificate on a free `sslip.io` name, the console's and evals' service accounts | [Deploy on GCP](https://bkraad47.github.io/ramen/wiki/deploy-gcp/) |
| `terraform/aws/` | VPC with pod subnets (D89), EKS with the VPC CNI policy agent and EBS CSI, DynamoDB, S3, ECR, Secrets Manager, self-signed ACM certificate, Load Balancer Controller, Fluent Bit | [Deploy on AWS](https://bkraad47.github.io/ramen/wiki/deploy-aws/) |
| `terraform/azure/` | AKS (workload identity, Calico), Table Storage, blob container, Key Vault, ACR (repository permissions), Azure Managed Redis, Envoy Gateway and cert-manager | [Deploy on Azure](https://bkraad47.github.io/ramen/wiki/deploy-azure/) |
| `terraform/{gcp,aws,azure}-satellite/` | a worker-only cluster in another region, joined to the home install | the deploy guides, "Satellite clusters" |
| `cloudformation/ramen.yaml` | the AWS base without Terraform (no Helm releases, no ACM import); outputs `NetworkSet` and `StorageSet` for the chart | [Deploy on AWS](https://bkraad47.github.io/ramen/wiki/deploy-aws/) |
| `helm/ramen/` | the console chart (`ramen-system`): console, edge (GKE Gateway, ALB Ingress or Envoy Gateway), RBAC, NetworkPolicies, the image builder, optional Redis, Phoenix / Jaeger, promptfoo, SkillSpector; `tls.require` is always true (D86) | `helm/ramen/values.yaml` |
| `helm/ramen-satellite/` | the satellite cluster's edge and the console's ClusterRoles | the deploy guides |
| `helm/ramen-worker/` | one zone namespace; the console renders it (or its built-in manifests) on every deploy, so you never install it by hand | [Groups, zones and regions](https://bkraad47.github.io/ramen/wiki/groups-zones/) |
| `docker/skillspector/` | Ramen's build of the pinned SkillSpector image | [SkillSpector scans](https://bkraad47.github.io/ramen/wiki/skillspector/) |
| `examples/mcp-headers/` | edge rules that route or rate-limit on the MCP standard headers | [Transport](https://bkraad47.github.io/ramen/wiki/transport/#standard-headers) |
| `scripts/selfsigned.sh` | fallback console TLS Secret for an IP or host (D17), only when `gateway.certificateMap` is unset | — |

## Public images
Every release publishes `ramen-console` and `ramen-worker` (linux/amd64 + linux/arm64, tags `<version>` and `latest`) to
`docker.io/bkraad47/` and `ghcr.io/bkraad47/`. Any reference works on every cloud, so pushing your own is optional:

```sh
V=$(cat VERSION)
helm upgrade --install ramen deploy/helm/ramen -n ramen-system ... \
  --set image.repository=docker.io/bkraad47/ramen-console --set image.tag=$V \
  --set console.env.RAMEN_IMAGE_WORKER=docker.io/bkraad47/ramen-worker:$V
```

Swap `docker.io/` for `ghcr.io/` if Docker Hub rate-limits your cluster. A group may record its own worker image in the
console, or have one built from its `requirements.txt` on deploy ([Worker images](https://bkraad47.github.io/ramen/wiki/worker-images/)).

## AWS without Terraform (CloudFormation, not run on a real account)
`cloudformation/ramen.yaml` creates the base resources only. Then, by hand:

```sh
aws cloudformation deploy --stack-name ramen --template-file deploy/cloudformation/ramen.yaml --capabilities CAPABILITY_NAMED_IAM
# the cluster add-ons Terraform installs from terraform/aws/helm.tf (Load Balancer Controller, Fluent Bit),
# with the role ARNs from the stack outputs; with Terraform, install_addons=false skips them the same way
openssl req -x509 -newkey rsa:2048 -nodes -days 825 -keyout tls.key -out tls.crt -subj "/CN=*.<region>.elb.amazonaws.com" \
  -addext "subjectAltName=DNS:*.<region>.elb.amazonaws.com"
aws acm import-certificate --certificate fileb://tls.crt --private-key fileb://tls.key   # → aws.certificateArn
```

Then install the `ramen` chart as in [Deploy on AWS](https://bkraad47.github.io/ramen/wiki/deploy-aws/) step 3, with
the stack outputs `NetworkSet` and `StorageSet` in place of Terraform's `network_set` and `engines_set`.

## Teardown
Each guide has a Teardown section. The order is the same everywhere: delete the groups in the console, remove
satellites, `helm uninstall ramen` against the install's own kube context (it releases the load balancer),
`terraform destroy`, then sweep what the console created outside Terraform. The `deploy-gcp`, `deploy-aws` and
`deploy-azure` agent skills in `skills/` do exactly that.
