---
title: Ramen
description: Multizone, highly available, enterprise-grade MCP server for GCP and AWS Kubernetes. JSON-RPC 2.0 over gRPC.
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
  "description": "Multizone, highly available, enterprise-grade MCP server for GCP and AWS Kubernetes. A Rust MCP node speaks JSON-RPC 2.0 over gRPC and a Python 3.14 runtime runs your tools, resources and prompts from a git repo; a stdio bridge serves standard MCP clients; a FastAPI console manages groups, environments, zones, secrets, canary deploys, IP rules and audit.",
  "keywords": "MCP, Model Context Protocol, MCP server, self-hosted, Kubernetes, GKE, EKS, gRPC, JSON-RPC, agent tools, Rust, Python, canary deploy, multi-zone, high availability",
  "author": {"@type": "Person", "name": "Raad", "url": "https://github.com/bkraad47"},
  "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
  "isAccessibleForFree": true
}
</script>

<div class="ramen-hero" markdown>
![Project Ramen](img/logo.png){ width=360 }

# Redefining how MCPs work

<p class="tag">MCP management made easy</p>

<div class="ramen-badges" markdown>
[![release](https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release&style=flat)](https://github.com/bkraad47/ramen/releases)
[![ci](https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/ci.yml?branch=main&label=ci&style=flat)](https://github.com/bkraad47/ramen/actions/workflows/ci.yml)
[![docs](https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/pages.yml?branch=main&label=docs&style=flat)](https://github.com/bkraad47/ramen/actions/workflows/pages.yml)
[![PyPI bridge](https://img.shields.io/pypi/v/ramen-mcp-bridge?color=F26B3A&label=ramen-mcp-bridge&style=flat)](https://pypi.org/project/ramen-mcp-bridge/)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-F26B3A?style=flat)](https://github.com/bkraad47/ramen/blob/main/LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP%20%2B%20gRPC-2B2622?style=flat)](https://modelcontextprotocol.io)
</div>
</div>

Ramen is a multizone, highly available MCP server for GCP and AWS Kubernetes. You keep **a git repo of tools,
resources and prompts in plain Python**. Ramen turns it into a fleet of workers behind a cloud load balancer, with
canary deploys, per-group secrets, identities and roles, an OAuth front door for standard MCP clients, and an audit
trail of who called what. Underneath it is gRPC, JSON-RPC and a Rust node. **You code in Python.**

The traditional way to ship an MCP server is one process per team, a shared key in a config file, and nobody
sure who can reach what. The AI-native way is a platform: groups own their tools, environments pin a ref and a set
of zones, people hold a role per group, and agents and clients sign in as themselves. A deploy is a canary, a
smoke test and a rollout, the same whether the caller is Claude Code, a browser agent or a CI job.

!!! tip "Start here: the demo repo and the bridge"
    - **[ramen-demo-mcp-group](https://github.com/bkraad47/ramen-demo-mcp-group)** — the group repo every guide
      deploys: one tool, one resource, one prompt, an `env.yaml`. Point a group at it and press Deploy.
    - **[ramen-mcp-bridge on PyPI](https://pypi.org/project/ramen-mcp-bridge/)** — `pip install ramen-mcp-bridge`
      for clients that only speak stdio; it signs you in through the console (`--oauth`) or carries a group key.
    - Everything that speaks Streamable HTTP (Claude Code, Claude Desktop, Cursor, the `mcp` SDK) connects to
      `https://<edge>/mcp` directly — [Connect an MCP client](how-tos/mcp-clients.md).

**What is verified today.** CI proves both transports on real node processes. Throwaway GKE projects have run
the gRPC path, Streamable HTTP, OAuth with Claude Code and the bridge, and the per-group roles engine through one
load balancer. The AWS path has been applied to a real account since 0.5.6.
[The full list, kept current →](wiki/transport.md#what-is-verified-and-what-is-not)

<div class="ramen-grid" markdown>
<div markdown>
### Git → bucket → worker
Push `mcp/tools/<name>/<name>.py` + `<name>.json` to a repo. Deploy syncs it to a bucket; workers pip-install and load it. [Write an MCP repo →](how-tos/develop-mcp-repo.md)
</div>
<div markdown>
### One front door, two transports
`POST /mcp` — a URL and a bearer header, nothing to install — next to `ramen.v1.Mcp/Call` for gRPC inside, on one port, through one set of guards. HTTPS only in the cloud. [Transport and security →](wiki/transport.md)
</div>
<div markdown>
### Groups, roles, identities
Groups own tools. People hold a role per group: Group Admin, Viewer or MCP User. A zone's workers run as their own cloud identity with approved, scoped permissions. [Features →](features.md)
</div>
<div markdown>
### Canary by default
Every deploy rolls a canary first, smoke-tests `tools/list`, then rolls the stable track. Failure = canary scaled to 0, stable untouched. [Canary →](wiki/canary.md)
</div>
</div>

## Quickstart (local, 5 commands)

Needs Docker (compose v2), `uv`, `git`, `make`. About 3–5 minutes on the first run (image builds).

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up          # Firestore emulator + console (https://localhost:8443) + one worker (localhost:8080, HTTP + gRPC)
make demo        # creates group `demo` from the demo repo, generates an rmk_ key, deploys, calls the tool over http://localhost:8080/mcp
# -> demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5
open https://localhost:8443   # self-signed cert; login admin@ramen.local / changeme-ramen
make down        # stop and remove volumes
```

The console is `https://localhost:8443` (accept the self-signed certificate), login **admin@ramen.local** /
**changeme-ramen** (from `deploy/local/.env`). The worker serves `http://localhost:8080/mcp` and takes a
**`rmk_` MCP key** generated on the group page. Put the key in `RAMEN_MCP_KEY` and point Claude Desktop, Cursor or
the `mcp` SDK at it:

```json
{"mcpServers": {"ramen-demo": {"url": "http://localhost:8080/mcp",
  "headers": {"Authorization": "Bearer ${RAMEN_MCP_KEY}", "ramen-group": "demo", "ramen-zone": "local"}}}}
```

Full walkthrough with a `curl` call, an `mcp` SDK snippet, the stdio bridge for clients that need it, and a raw
`grpcurl` call: [Local quickstart](how-tos/local-quickstart.md). The 0.3.1 → 0.4.x gRPC-only period is over; a
client written against 0.3.0's `/mcp` works again, and one written for the bridge keeps working.

<figure markdown>
![Ramen console dashboard](img/dashboard.png){ .ramen-shot }
<figcaption>Dashboard: load per zone × group — low, even, high or down, labelled on every cell.</figcaption>
</figure>

## Deploy to the cloud

| Target | Status | Guide |
|---|---|---|
| GCP (GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB with header routing, Cloud Armor, Google-managed TLS) | verified on throwaway projects at 0.3.2, 0.5.1 and 0.5.95: infrastructure, gRPC and Streamable HTTP through the Gateway, OAuth with Claude Code and the bridge, per-group roles | [Deploy on GCP](how-tos/gcp.md) · [End to end](how-tos/end-to-end.md) |
| AWS (EKS, DynamoDB, S3, Secrets Manager, ALB with gRPC target groups, WAF) | applied to a real account since 0.5.6; the published bridge server-tested against it in 0.5.8 | [Deploy on AWS](how-tos/aws.md) |
| Local (docker compose) | CI end-to-end on every push | [Local quickstart](how-tos/local-quickstart.md) · [Write an MCP repo](how-tos/develop-mcp-repo.md) |

## Where next

- **Understand it**: [How it works and why](how-it-works.md) · [Features, and how each is managed](features.md) · [Architecture](architecture/index.md) · [Threat model](threat-model.md)
- **Run it**: [Configuration](how-tos/configuration.md) · [Service accounts and secrets](how-tos/service-accounts.md) · [Back up and restore](how-tos/backup-restore.md) · [Sign in with Entra ID / Google Workspace](how-tos/sso.md) · [DevOps with the API](how-tos/devops-api.md)
- **Use it**: [Connect an MCP client](how-tos/mcp-clients.md) · [The console, page by page](how-tos/console.md) · [Cloud-ops agent skills](wiki/skills.md)
- **Follow it**: [Versions](versions.md) · [Related versions (bridge, demo)](related-versions.md) · [Releases](https://github.com/bkraad47/ramen/releases) · [Contributing](contributing.md)

<!-- Machine-readable index for agents and crawlers: https://bkraad47.github.io/ramen/llms.txt (llmstxt.org) -->
