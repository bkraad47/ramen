---
title: Self-hosted MCP for teams
description: Multizone, highly available MCP server for GCP, AWS and Azure Kubernetes. A git repo of Python tools becomes a fleet of MCP workers with canary deploys, secrets, roles and OAuth.
hide: [toc]
---

<script type="application/ld+json">
{
  "@context": "https://schema.org",
  "@graph": [
    {
      "@type": "SoftwareApplication",
      "@id": "https://bkraad47.github.io/ramen/#app",
      "name": "Ramen",
      "alternateName": "Project Ramen",
      "applicationCategory": "DeveloperApplication",
      "applicationSubCategory": "MCP server (Model Context Protocol)",
      "operatingSystem": "Kubernetes (GKE, EKS, AKS), Docker",
      "softwareVersion": "0.7.6",
      "license": "https://opensource.org/licenses/BSD-3-Clause",
      "url": "https://bkraad47.github.io/ramen/",
      "downloadUrl": "https://github.com/bkraad47/ramen/releases",
      "releaseNotes": "https://bkraad47.github.io/ramen/changelog/",
      "softwareHelp": "https://bkraad47.github.io/ramen/get-started/",
      "screenshot": "https://bkraad47.github.io/ramen/img/dashboard.png",
      "description": "Self-hosted, multi-zone, highly available MCP (Model Context Protocol) server for Kubernetes on GKE, EKS and AKS. A Rust MCP node speaks Streamable HTTP and JSON-RPC 2.0 over gRPC; a Python 3.12 runtime runs your tools, resources and prompts from a git repo, with NeMo Guardrails per tool; a FastAPI console manages groups, environments, zones, secrets, canary deploys, roles per group and custom roles, OAuth 2.1 sign-in with Entra ID and Google Workspace groups, autoscaling, IP rules, throttling and audit.",
      "keywords": "MCP, MCP server, Model Context Protocol, MCP gateway, self-hosted MCP, multi-zone, high availability, Kubernetes, GKE, EKS, AKS, GCP, AWS, Azure, OAuth, OAuth 2.1, gRPC, JSON-RPC, Streamable HTTP, canary deploy, agent tools, Rust, Python",
      "author": {
        "@type": "Person",
        "name": "Raad",
        "url": "https://github.com/bkraad47"
      },
      "offers": {
        "@type": "Offer",
        "price": "0",
        "priceCurrency": "USD"
      },
      "isAccessibleForFree": true
    },
    {
      "@type": "SoftwareSourceCode",
      "@id": "https://github.com/bkraad47/ramen#code",
      "name": "ramen",
      "codeRepository": "https://github.com/bkraad47/ramen",
      "programmingLanguage": [
        "Rust",
        "Python"
      ],
      "runtimePlatform": [
        "Kubernetes",
        "Docker"
      ],
      "license": "https://opensource.org/licenses/BSD-3-Clause",
      "version": "0.7.6",
      "keywords": "MCP, MCP server, Model Context Protocol, MCP gateway, self-hosted MCP, multi-zone, high availability, Kubernetes, GKE, EKS, AKS, GCP, AWS, Azure, OAuth, OAuth 2.1, gRPC, JSON-RPC, Streamable HTTP, canary deploy, agent tools, Rust, Python",
      "targetProduct": {
        "@id": "https://bkraad47.github.io/ramen/#app"
      },
      "author": {
        "@type": "Person",
        "name": "Raad",
        "url": "https://github.com/bkraad47"
      }
    }
  ]
}
</script>

<div class="ramen-hero" markdown>
![Project Ramen](img/logo.png){ width=360 }
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

<p class="ramen-glama"><a href="https://glama.ai/mcp/servers/bkraad47/ramen"><img width="380" height="200" src="https://glama.ai/mcp/servers/bkraad47/ramen/badge" alt="Ramen on Glama: score, tools and listing"></a></p>

Ramen is a multizone, highly available MCP server for GCP, AWS and Azure Kubernetes. Your team keeps **a git repo of
tools, resources and prompts in plain Python**. Ramen turns it into a fleet of MCP workers behind a cloud load
balancer, with canary deploys, secrets, a role per group, an OAuth front door for Claude Code and the bridge, and a log line
that says who called what. Underneath it is gRPC, JSON-RPC 2.0 and a Rust node. **You code in Python.**

<div class="ramen-grid ramen-repos" markdown>
<div markdown>
### [ramen](https://github.com/bkraad47/ramen)
The server: console, Rust node, Python runtime, Helm charts and Terraform for GCP, AWS and Azure. Releases are tagged
and built by CI.
</div>
<div markdown>
### [ramen-mcp-bridge](https://github.com/bkraad47/ramen-mcp-bridge)
`pip install ramen-mcp-bridge`. A stdio MCP server for clients that cannot speak HTTP. Carries a group key or
signs you in through the console. [On PyPI](https://pypi.org/project/ramen-mcp-bridge/).
</div>
<div markdown>
### [ramen-demo-mcp](https://github.com/bkraad47/ramen-demo-mcp-group)
The demo group repo every guide deploys: three tools, a resource, a prompt, an `env.yaml`, golden cases and NeMo rails. Point a group at
it and press Deploy.
</div>
</div>

<p class="ramen-cta" markdown>[Get started :material-arrow-right:](get-started.md){ .md-button .md-button--primary } [How it works](how-it-works.md){ .md-button }</p>

## Why Ramen

- **One deployment per team, not one server per tool.** A worker loads every tool of the group. Thirty tools
  run on one Deployment per zone with a canary, behind one load balancer.
- **Built on how organizations work.** A group is a team. It owns a repo, a bucket, its secrets and its members.
  People hold a role per group: Group Admin, Viewer, MCP User, or a role you define. Entra ID or Google Workspace
  groups can decide the roles at sign-in, and what an admin sets by hand stays.
- **Guardrails where the tool runs.** Opt a tool into NeMo Guardrails rails, or a policy file of your own, checked
  in the worker before the call and after the result. A blocked call is a readable tool error; the access log names
  the rail.
- **Build once, deploy on three clouds.** Terraform and Helm for GKE, EKS and AKS, each verified live in 0.7.5 with
  a publicly trusted certificate on GKE and AKS. The same console, routes and guards on all three.
- **It scales itself.** Ramen adds workers under load and removes them when it is quiet, between 1 and a max that
  group admins set per zone. No cloud autoscaler config, every step in the audit log.
- **Canary by default.** Every deploy rolls one canary pod, smoke-tests it, then rolls the stable pods. A failure
  leaves the old version serving.
- **Clients connect as themselves.** Claude Code over HTTP, and Cursor, VS Code or Claude Desktop through the bridge,
  sign a person in through the console (a Ramen password, Entra ID or Google) and get a token for one group and zone.
  Agents and CI use a group key. The group page prints the exact commands.
- **Secrets never show.** Values live in Secret Manager, Secrets Manager, Key Vault or the Fernet-encrypted store and reach
  tool code only as environment variables on deploy.
- **The edge is locked.** The cloud edges listen on 443 only, IP rules apply per zone at the node and at the cloud
  edge, Redis throttles by address and by token, and every console action leaves an audit line.
- **Multizone from day one.** Group, environment, zone, worker. The load balancer routes on two headers, so one
  client config reaches any zone.
- **Rust where it counts.** The Rust node owns auth, limits and the transport. Your Python runs in a separate
  process it can restart at any time.

**What is verified.** 0.7.5 ran live on GKE, EKS and AKS, with the same 33 checks, Entra ID sign-in with group
lookup and autoscaling load tests on each, and CI proves both transports on Linux and Windows; the single record,
including what is tested only, is [Transport → What is verified](wiki/transport.md#what-is-verified-and-what-is-not).
Everything in detail is in the [wiki](wiki/index.md).
