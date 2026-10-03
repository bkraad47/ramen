# Changelog

One line per release, newest first. The full notes are in
[`CHANGELOG.md`](https://github.com/bkraad47/ramen/blob/main/CHANGELOG.md), and the
[version tracker](versions.md) lists every release with its tag date and its release page.

Dates are the dates the tag was made. A few versions were folded forward into the next one and never tagged;
they are marked.

| Version | Date | What changed |
|---|---|---|
| **0.6.0** | 2026-10-03 | Deleting a zone or an environment tears the deployment down for real. HTTPS only at the edge. `mcp/env.yaml` rendered from secrets. A stuck rollout names the pending pod, and the canary rolls in place. The AWS edge IP rule fixed to match the group header. |
| 0.5.95 | 2026-10-03 | Roles per group: one person can be a Group Admin of one group and an MCP User of another. Bridge 0.2.0 signs a person in with `--oauth`. |
| 0.5.94 | 2026-10-03 | Zones and blocked packages became checkbox dropdowns on the group page. |
| 0.5.93 | 2026-10-03 | Role mapping from an identity provider, the `mcp_user` role, scoped service-account permissions, and every settings form fixed to work under the console's content-security policy. |
| 0.5.8 | 2026-10-03 | The published bridge verified live against AWS and GCP; the AWS certificate's name fixed to cover the load balancer. |
| 0.5.7 | 2026-10-03 | The bridge became its own repository and a package on PyPI. |
| 0.5.6 | 2026-10-03 | Auto-rebalance, email alerts, Redis throttling, GitHub App authentication, and the AWS path applied to a real account for the first time. |
| 0.5.5 | folded into 0.5.6 | Google-managed TLS on a free hostname, Postgres as a third store, the demo repo and the bridge docs as public repositories. |
| 0.5.1 to 0.5.4 | 2026-09-30 | The end-to-end guide, 0.5.0 proven on GKE, the logo mark, the add-a-tool guide, honest deploy pictures. |
| 0.5.0 | 2026-09-29 | Streamable HTTP at the edge next to gRPC inside, stateless sessions, and the console as an OAuth 2.1 server. |
| 0.4.1 to 0.4.3 | folded into 0.5.0 | Restore, revocation, per-group worker images, console polish, screenshots that cannot go stale. |
| 0.4.0 | 2026-09-29 | Console, documentation and evidence. |
| 0.3.2 | 2026-09-28 | The gRPC path verified end to end on a live GKE Gateway. |
| 0.3.1 | 2026-09-28 | JSON-RPC over gRPC, the stdio bridge, routing on headers, and narrowed cloud IAM. |
| 0.3.0 | 2026-09-28 | AWS Terraform and CloudFormation, OAuth sign-in, the policy engine, tool blocking, the documentation site. |
| 0.2.0 | 2026-09-28 | GCP: GKE Autopilot, Firestore, the Gateway, Cloud Armor. |
| 0.1.0 | 2026-09-27 | The local core: console, Rust node, Python runtime, compose stack. |
