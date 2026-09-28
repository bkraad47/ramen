# Migrate 0.3.0 → 0.3.1: the HTTP MCP endpoint is gone, use the bridge

0.3.1 changes **one thing for clients**: the worker no longer serves MCP over HTTP. It serves the same JSON-RPC 2.0
messages over gRPC (`ramen.v1.Mcp/Call`, [contract §11](../CONTRACTS.md), decision D19). Nothing in your group
repos, protos, keys, secrets, console API or Terraform inputs changes. This page is the checklist.

## What stopped working
| 0.3.0 | 0.3.1 |
|---|---|
| `POST http://<worker>:8080/mcp` (Streamable HTTP) | `ramen.v1.Mcp/Call` on `<worker>:8080` (h2c) or the LB (TLS) |
| `https://<lb>/mcp/<group>/<zone>` path routing | `<lb>:443` with gRPC metadata `ramen-group: <g>`, `ramen-zone: <z>` |
| `GET /healthz`, `GET /readyz` on the worker | `grpc.health.v1.Health/Check` (`SERVING` once code is loaded) |
| `GET /metrics`, `POST /admin/reload` (`X-Ramen-Admin-Key`) | `ramen.v1.Admin/Metrics`, `Admin/Reload` (metadata `x-ramen-admin-key`) |
| HTTP 401 + JSON-RPC `-32001` on a bad key | gRPC `UNAUTHENTICATED` (16) |
| HTTP 403 on a blocked CIDR | gRPC `PERMISSION_DENIED` (7) |
| HTTP 429 on `RAMEN_MAX_INFLIGHT` | gRPC `RESOURCE_EXHAUSTED` (8) |
| `RAMEN_MCP_PATH_PREFIX` (AWS path alias) | removed — no path in play |
| `RAMEN_GCP_POD_PROXY` / `RAMEN_AWS_POD_PROXY` (console) | removed — the console dials pods over gRPC |

Unchanged: `rmk_` keys and `Authorization: Bearer` semantics (now gRPC metadata `authorization`), blocked tools
(`-32601` in the body), `RAMEN_ALLOWED_CIDRS` / `RAMEN_ADMIN_CIDRS` / `RAMEN_TRUST_PROXY`, the console's
`/api/v1`, `/readyz` **on the console** (it is still an HTTP service), Cloud Armor / WAF IP rules.

## 1. Clients: switch to `ramen-mcp-bridge`
Every MCP client that had a URL now runs a command. The bridge is a stdio MCP server; it holds the key and the
routing metadata and forwards each message to `Mcp/Call`.

```sh
uv tool install './runtime-py[grpc]'     # from a checkout; or run it inside the worker image
```

=== "Before (0.3.0)"
    ```json
    {"mcpServers": {"ramen-demo": {
      "url": "https://<lb>/mcp/demo/a",
      "headers": {"Authorization": "Bearer rmk_…"}}}}
    ```

=== "After (0.3.1)"
    ```json
    {"mcpServers": {"ramen-demo": {
      "command": "ramen-mcp-bridge",
      "args": ["--target", "<lb>:443", "--tls", "--ca", "/path/ramen-lb.pem",
               "--key", "rmk_…", "--group", "demo", "--zone", "a"]}}}
    ```
    Locally: `--target localhost:8080 --insecure` (plaintext h2c). `--ca` is only needed while the LB uses the
    self-signed certificate (D17); drop it once a managed certificate is in place.

`curl` scripts become `grpcurl` calls or an `mcp` SDK snippet; both are in the
[local quickstart](local-quickstart.md#5-call-the-worker-raw-with-grpcurl). Anything that polled `/readyz` on a
worker should use `grpc_health_probe -addr <host:port>` or the `Health/Check` RPC.

## 2. Operators: roll the edge and the console together
Order matters only in that old clients stop working as soon as the workers are on 0.3.1; the bridge works against
0.3.1 only.

**GCP**
```sh
cd deploy/terraform/gcp && terraform apply        # console GSA: custom role replaces projectIamAdmin; resource-level bindings
cd ../../.. && make push PROJECT=… REGION=…       # 0.3.1 images
helm upgrade ramen deploy/helm/ramen -n ramen-system --reuse-values   # header-routed HTTPRoutes, reduced ClusterRole
kubectl -n ramen-system rollout status deploy/console
```
Then, per zone: **Deploy (canary)** from the console. The 0.3.1 console re-renders each zone's Service
(`appProtocol: kubernetes.io/h2c`), HTTPRoute (header matches, no rewrite), `HealthCheckPolicy` (`GRPC`) and a
namespaced Role/RoleBinding, and rolls the workers. The route flips from `/mcp/<g>/<z>` to header matching in one
reconcile (a few minutes on the Gateway); old-path clients get 404 from then on.

**AWS (untested path)**: `terraform apply` (narrowed WAF/IAM), push images, `helm upgrade`, then deploy each
zone; the console re-annotates the zone Ingress with `backend-protocol-version: GRPC`, header conditions and a
gRPC health check (`success-codes: 0`). If the ALB rejects the gRPC target group, check that the listener is HTTPS
(gRPC on ALB requires TLS on the listener).

**Local**: `git pull && make up` (rebuilds both images), then `make demo`. `deploy/local/.env` keeps working;
`deploy/local/mcp-client-config.example.json` is now a bridge config.

## 3. Verify
```sh
grpc_health_probe -addr localhost:8080                                 # local worker → SERVING
grpc_health_probe -addr <lb>:443 -tls -tls-ca-cert ramen-lb.pem \
  -rpc-header 'ramen-group: demo' -rpc-header 'ramen-zone: a'          # through the LB → SERVING
ramen-mcp-bridge --target <lb>:443 --tls --ca ramen-lb.pem --key rmk_… --group demo --zone a   # then tools/list from your client
```
The console's group page shows the same thing: worker rows read `SERVING` and the deploy job's smoke step
prints the tool list. Logs now carry a `grpc_code` field per call.

## 4. Also in 0.3.1 (no action needed, but read before re-applying IAM)
The console's cloud permissions were narrowed (GCP: no `projectIamAdmin`; AWS: `wafv2` and `iam:PutRolePolicy`
scoped to `ramen` resources; Kubernetes: no cluster-wide `secrets`/`serviceaccounts`), git tokens never enter a
remote URL, OIDC login uses PKCE + `nonce`, and node key compares are constant-time. If you hand-edited any of
those roles on 0.3.0, re-apply from Terraform/Helm rather than merging by hand. Details:
[Security](security.md#what-changed-in-031), [Architecture v0.3.1](../architecture/v0.3.1.md).
