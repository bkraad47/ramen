---
name: deploy-aws
description: Bring Ramen up on AWS (EKS, DynamoDB, S3, Secrets Manager, ECR, ALB, WAF) with Terraform or CloudFormation + Helm, then create the first zone, group and deploy. UNTESTED path — use when asked to deploy Ramen on AWS, and expect to fix IAM details.
---

# Deploy Ramen on AWS (untested path)

The AWS artifacts were built against `docs/CONTRACTS.md` §8 and unit-tested with moto; **they have never been
applied to a real account**. This skill therefore runs everything in plan/dry-run mode first, requires a human OK
before `apply`, and refuses to call the job done without the validator. Narrative: `docs/how-tos/aws.md`.

## Inputs
- AWS credentials with admin on the target account (`aws sts get-caller-identity` works), `REGION` (default `us-east-1`), first zone `AWS_AZ` (default `us-east-1a`).
- Choice: `terraform` (default) or `cloudformation`.
- Tools: `aws`, `terraform >= 1.6` or `aws cloudformation`, `helm 4`, `kubectl`, `docker` + buildx, `uv`, `openssl`, `python3`, `cfn-lint` (for the template path).

## Steps
1. Preflight: `aws sts get-caller-identity`; `aws service-quotas get-service-quota --service-code eks --quota-code L-1194D53C` (clusters per region) is ≥ 1 free; region has ≥ 2 AZs.
2. Plan only: `cd deploy/terraform/aws && cp -n terraform.tfvars.example terraform.tfvars` (region, node size) → `terraform init && terraform validate && terraform plan -out=ramen.plan`; or `cfn-lint deploy/cloudformation/ramen.yaml && aws cloudformation validate-template --template-body file://deploy/cloudformation/ramen.yaml`. Summarise the plan (resource count, IAM roles, estimated cost: EKS control plane ~$0.10/h + node group + ALB + NAT if any) and **ask the human to confirm** before step 3.
3. Apply: `terraform apply ramen.plan` (~20 min) or `aws cloudformation deploy --stack-name ramen --template-file deploy/cloudformation/ramen.yaml --capabilities CAPABILITY_NAMED_IAM --parameter-overrides Region=$REGION`. Record outputs `cluster_name`, `ecr_console`, `ecr_worker`, `console_role_arn`, ACM cert ARN, bucket name.
4. Images: `aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin <account>.dkr.ecr.$REGION.amazonaws.com`; `docker buildx build --platform linux/amd64 -f node-rs/Dockerfile --build-arg VERSION=$(cat VERSION) -t <ecr_worker>:$(cat VERSION) --push .`; `docker buildx build --platform linux/amd64 --build-arg RAMEN_VERSION=$(cat VERSION) -t <ecr_console>:$(cat VERSION) --push console`.
5. Console: `aws eks update-kubeconfig --name ramen --region $REGION`; generate `ADMIN_PW`, `FERNET`, `ADMIN_KEY` as in deploy-gcp; `helm upgrade --install ramen deploy/helm/ramen -n ramen-system --create-namespace --set provider=aws,region=$REGION,image.console=<ecr_console>:<v>,image.worker=<ecr_worker>:<v>,console.roleArn=<console_role_arn>,console.certificateArn=<acm_arn> --set console.secrets.RAMEN_ADMIN_PASSWORD=$ADMIN_PW,console.secrets.RAMEN_FERNET_KEY=$FERNET,console.secrets.RAMEN_ADMIN_KEY=$ADMIN_KEY`. If the chart rejects a value, read `deploy/helm/ramen/values.yaml` — the AWS keys are documented there — and do not guess.
6. Wait: `kubectl -n ramen-system rollout status deploy/console`; `kubectl -n ramen-system get ingress console` until ADDRESS is set (ALB controller, 3–5 min); `curl -k https://<alb-dns>/readyz` → `{"ok":true,...}`. If the Ingress never gets an address, check `kubectl -n kube-system logs deploy/aws-load-balancer-controller` — the most likely first-contact failure is the controller's IAM policy.
7. First zone/group/env/key/deploy through the API exactly as in `deploy-gcp` step 6, with `provider:"aws"`, `region:"$AWS_AZ"`. Expect the first deploy to surface IRSA/S3 permission errors in the job log; fix the role policy in Terraform (path `/ramen/`), re-apply, redeploy.
8. Record every deviation you had to make in `docs/how-tos/aws.md` under "Known unknowns" (as a PR), so the next run is shorter.

## Validate
(read-only sub-agent; `CONSOLE=https://<alb-dns>`, `rmk_` key, viewer `rmn_` key)
- V1 `curl -sk $CONSOLE/readyz` → 200, `"store":"dynamodb"`.
- V2 `curl -sk -H "X-Ramen-Api-Key: $RMN" $CONSOLE/api/v1/groups/<g>/zones/<z>/workers` → 200, ≥1 live stable worker.
- V3 `curl -sk "$CONSOLE/mcp/<g>/<z>" -H "Authorization: Bearer $RMK" -H 'Content-Type: application/json' -d '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"demo_calculator_tool","arguments":{"var1":2,"var2":3,"func":"add"}}}'` → `"text":"5"`, `isError:false`.
- V4 Same without the header → 401.
- V5 `aws logs describe-log-groups --log-group-name-prefix /aws/containerinsights/ramen` lists a group; `curl -sk -H "X-Ramen-Api-Key: $RMN" "$CONSOLE/api/v1/logs?group=<g>&zone=<z>&tail=5"` → 200 with ≥1 line.
- V6 `aws wafv2 list-web-acls --scope REGIONAL` shows no ACL for the group unless IP rules were set; if they were, `curl` from a disallowed IP → 403.

## Boundaries
- No `apply` without the human's confirmation from step 2; no teardown unless asked (then `helm uninstall ramen -n ramen-system`, `terraform destroy` / `delete-stack`, and check for leftover ALBs, target groups, WAF ACLs, log groups).
- Do not widen IAM policies to `*` to make something pass; report the exact missing action instead.
- Never print secrets or keys beyond the one hand-over. Stop after three failed validator rounds.
