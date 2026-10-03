---
title: Ramen
description: Multizone, highly available MCP server for GCP and AWS Kubernetes. A git repo of Python tools becomes a fleet of MCP workers with canary deploys, secrets, roles and OAuth.
hide: [toc]
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@type": "SoftwareApplication",
  "name": "Ramen",
  "alternateName": "Project Ramen",
  "applicationCategory": "DeveloperApplication",
  "applicationSubCategory": "MCP server (Model Context Protocol)",
  "operatingSystem": "Kubernetes (GKE, EKS), Docker",
  "softwareVersion": "0.6.0",
  "license": "https://opensource.org/licenses/BSD-3-Clause",
  "url": "https://bkraad47.github.io/ramen/",
  "codeRepository": "https://github.com/bkraad47/ramen",
  "downloadUrl": "https://github.com/bkraad47/ramen/releases",
  "programmingLanguage": ["Rust", "Python"],
  "description": "Multizone, highly available, enterprise-grade MCP server for GCP and AWS Kubernetes. A Rust MCP node speaks Streamable HTTP and JSON-RPC 2.0 over gRPC; a Python 3.14 runtime runs your tools, resources and prompts from a git repo; a FastAPI console manages groups, environments, zones, secrets, canary deploys, roles, OAuth, IP rules and audit.",
  "keywords": "MCP, Model Context Protocol, MCP server, self-hosted, Kubernetes, GKE, EKS, gRPC, JSON-RPC, agent tools, Rust, Python, canary deploy, multi-zone, high availability, OAuth",
  "author": {"@type": "Person", "name": "Raad", "url": "https://github.com/bkraad47"},
  "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
  "isAccessibleForFree": true
}
</script>

<div class="ramen-hero" markdown>
![Project Ramen](img/logo.png){ width=360 }

# Redefining how MCPs work

<p class="tag">MCP management made easy</p>
</div>

<table class="ramen-badge-table">
<tr>
<td><a href="https://github.com/bkraad47/ramen/releases"><img src="https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release&style=flat" alt="release"></a></td>
<td><a href="https://github.com/bkraad47/ramen/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/ci.yml?branch=main&label=ci&style=flat" alt="ci"></a></td>
<td><a href="https://github.com/bkraad47/ramen/actions/workflows/pages.yml"><img src="https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/pages.yml?branch=main&label=docs&style=flat" alt="docs"></a></td>
<td><a href="https://pypi.org/project/ramen-mcp-bridge/"><img src="https://img.shields.io/pypi/v/ramen-mcp-bridge?color=F26B3A&label=ramen-mcp-bridge&style=flat" alt="PyPI bridge"></a></td>
<td><a href="https://github.com/bkraad47/ramen/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-BSD--3--Clause-F26B3A?style=flat" alt="license"></a></td>
<td><a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-Streamable%20HTTP%20%2B%20gRPC-2B2622?style=flat" alt="MCP"></a></td>
</tr>
</table>

Ramen is a multizone, highly available MCP server for GCP and AWS Kubernetes. Your team keeps **a git repo of
tools, resources and prompts in plain Python**. Ramen turns it into a fleet of MCP workers behind a cloud load
balancer, with canary deploys, secrets, a role per group, an OAuth front door for Claude and Cursor, and a log line
that says who called what. Underneath it is gRPC, JSON-RPC 2.0 and a Rust node. **You code in Python.**

<div class="ramen-grid ramen-repos" markdown>
<div markdown>
### [ramen](https://github.com/bkraad47/ramen)
The server: console, Rust node, Python runtime, Helm charts and Terraform for GCP and AWS. Releases are tagged
and built by CI.
</div>
<div markdown>
### [ramen-mcp-bridge](https://github.com/bkraad47/ramen-mcp-bridge)
`pip install ramen-mcp-bridge`. A stdio MCP server for clients that cannot speak HTTP. Carries a group key or
signs you in through the console. [On PyPI](https://pypi.org/project/ramen-mcp-bridge/).
</div>
<div markdown>
### [ramen-demo-mcp](https://github.com/bkraad47/ramen-demo-mcp-group)
The demo group repo every guide deploys: one tool, one resource, one prompt and an `env.yaml`. Point a group at
it and press Deploy.
</div>
</div>

<p class="ramen-cta" markdown>[Get started :material-arrow-right:](get-started.md){ .md-button .md-button--primary } [How it works](how-it-works.md){ .md-button }</p>

## Why Ramen

- **One deployment per team, not one server per tool.** A worker loads every tool of the group. Thirty tools
  run on one Deployment per zone with a canary, behind one load balancer.
- **Built on how organizations work.** A group is a team. It owns a repo, a bucket, its secrets and its members.
  People hold a role per group: Group Admin, Viewer or MCP User.
- **Canary by default.** Every deploy rolls one canary pod, smoke-tests it, then rolls the stable pods. A failure
  leaves the old version serving.
- **Clients connect as themselves.** Claude Code and the bridge sign a person in through the console with OAuth
  and get a token for one group and zone. Agents and CI use a group key.
- **Secrets never show.** Values live in Secret Manager, Secrets Manager or the Fernet-encrypted store and reach
  tool code only as environment variables on deploy.
- **The edge is locked.** The cloud edges listen on 443 only, IP rules apply per zone at the node and at the cloud
  edge, Redis throttles by address and by token, and every console action leaves an audit line.
- **Multizone from day one.** Group, environment, zone, worker. The load balancer routes on two headers, so one
  client config reaches any zone.
- **Rust where it counts.** The Rust node owns auth, limits and the transport. Your Python runs in a separate
  process it can restart at any time.

**What is verified.** CI proves both transports on real node processes on Linux and Windows, and every release is
deployed to a throwaway GKE project and a real AWS account emptied the same day, each with two zones, OAuth, the
bridge and a teardown. Microsoft Entra ID and Google Workspace sign-in is untested against a real tenant.
[What each run covered, hop by hop.](wiki/transport.md#what-is-verified-and-what-is-not)
Everything in detail is in the [wiki](wiki/index.md).
