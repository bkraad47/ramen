# Routing and limiting on the MCP standard headers

A 2026-07-28 request to `POST /mcp` says what it is in two HTTP headers: `Mcp-Method` (the JSON-RPC method) and
`Mcp-Name` (the tool or prompt name, the resource URI, or the task id). A load balancer or a firewall can read them
without parsing the body. These files show how.

**These are examples. None of them has been applied to a live cluster, project or account.** They parse (YAML,
`terraform fmt`), and that is all that has been checked. Names (`demo`, `a`, `slow_report`, `worker-reports`,
namespace `ramen-demo-a`, gateway `ramen`) are placeholders for yours.

| File | What it shows |
|---|---|
| [`gke-httproute.yaml`](gke-httproute.yaml) | GKE Gateway `HTTPRoute`: one tool's calls routed to their own Service |
| [`aws-alb-ingress.yaml`](aws-alb-ingress.yaml) | AWS ALB Ingress `conditions` annotation: the same routing on an ALB |
| [`envoy-gateway-ratelimit.yaml`](envoy-gateway-ratelimit.yaml) | Envoy Gateway `HTTPRoute` + `BackendTrafficPolicy`: a rate limit for one tool |
| [`cloud-armor-rule.tf`](cloud-armor-rule.tf) | Cloud Armor rule: `403` at the Google edge for one tool |
| [`aws-waf-rule.tf`](aws-waf-rule.tf) | AWS WAFv2 rule group: block one tool at the ALB |

## What a header rule does not do

Read this before using a header rule as a control.

- **Only 2026-07-28 requests carry the headers.** A 2025-06-18 request has no `Mcp-Method` and no `Mcp-Name`, so a
  rule that blocks or limits by header does not see it, and the worker still serves that client. The same goes for
  gRPC (`ramen.v1.Mcp/Call`, what the stdio bridge speaks): no standard headers there.
- **A name can be sent encoded.** The worker accepts `Mcp-Name: =?base64?<base64 of the name>?=` and decodes it
  before comparing it with the body. An exact-match rule on the plain name does not match the encoded form.
- **So an edge rule is a routing or cost tool, not a block.** To stop a tool from being called, block it in the
  console (group page → Blocked packages): the worker then hides it and refuses it for every client, on both
  transports and both protocol versions.

## What the worker guarantees

On a 2026-07-28 HTTP request the worker requires `Mcp-Method`, and `Mcp-Name` where the method has a name, and
refuses the request when a header disagrees with the JSON-RPC body (error `-32020`, HTTP `400`, access-log reason
`header_mismatch`). The body is what runs; a header is never used in its place. A client therefore cannot send one
name in the header to pass an edge rule and another in the body: the worker refuses the pair. Details:
[Transport → Standard headers](https://bkraad47.github.io/ramen/wiki/transport/#standard-headers).

## Notes per file

- The two routing examples send traffic to a Service named `worker-reports`. Ramen does not create it; the chart's
  own route (`worker`, matching `ramen-group` and `ramen-zone`) keeps serving everything else.
- The Envoy Gateway limit is `type: Local`: each Envoy replica counts on its own.
- The console writes its own IP rules into the group's Cloud Armor policy and the group's WAF web ACL. Whether a
  hand-added rule survives the console's next change to that policy has not been tested; keep yours in a policy or
  rule group you own.
