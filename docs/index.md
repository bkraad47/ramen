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
  "softwareVersion": "0.4.0",
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

# Multizone MCP server for GCP and AWS Kubernetes

<p class="tag">Rust MCP node + Python 3.14 runtime workers · JSON-RPC 2.0 over gRPC · one FastAPI console</p>

<div class="ramen-badges" markdown>
[![release](https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release&style=flat)](https://github.com/bkraad47/ramen/releases)
[![ci](https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/ci.yml?branch=main&label=ci&style=flat)](https://github.com/bkraad47/ramen/actions/workflows/ci.yml)
[![docs](https://img.shields.io/github/actions/workflow/status/bkraad47/ramen/pages.yml?branch=main&label=docs&style=flat)](https://github.com/bkraad47/ramen/actions/workflows/pages.yml)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-F26B3A?style=flat)](https://github.com/bkraad47/ramen/blob/main/LICENSE)
[![MCP](https://img.shields.io/badge/MCP-JSON--RPC%202.0%20over%20gRPC-2B2622?style=flat)](https://modelcontextprotocol.io)
</div>
</div>

Ramen turns a **git repo of tools, resources and prompts** into a fleet of MCP workers behind a cloud load balancer.
Each worker is a **Rust MCP node** (JSON-RPC 2.0 over gRPC, auth, IP allow-lists, metrics) paired 1:1 with a
**Python 3.14 runtime** that runs your code. Standard MCP clients (Claude Desktop, Cursor, the `mcp` SDK) connect
through the **`ramen-mcp-bridge`** stdio bridge. A single **FastAPI console** manages groups, environments, zones,
secrets, canary deploys, rebalancing, logs and audit, and every action is also available through an API key.

<div class="ramen-grid" markdown>
<div markdown>
### Git → bucket → worker
Push `mcp/tools/<name>/<name>.py` + `<name>.json` to a repo. Deploy syncs it to a bucket; workers pip-install and load it. [Protos →](wiki/protos.md)
</div>
<div markdown>
### gRPC transport (v0.3.1)
One `ramen.v1.Mcp/Call` per JSON-RPC message: binary framing, HTTP/2 multiplexing, first-class health and deadlines. The LB routes on `ramen-group` / `ramen-zone` metadata. [Transport and security →](wiki/transport.md)
</div>
<div markdown>
### Canary by default
Every deploy rolls a canary first, smoke-tests `tools/list`, then rolls the stable track. Failure = canary scaled to 0, stable untouched. [Canary →](wiki/canary.md)
</div>
<div markdown>
### Enterprise controls
Super admin / group admin / viewer, `rmk_` MCP keys, `rmn_` API keys, IP rules (per zone at the node, one Cloud Armor policy per group at the edge), secrets never shown, full audit log. [Security →](how-tos/security.md)
</div>
</div>

## Quickstart (local, 5 commands)

Needs Docker (compose v2), `uv`, `git`. About 3–5 minutes on the first run (image builds).

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up          # Firestore emulator + console (https://localhost:8443) + one worker (gRPC localhost:8080, h2c)
make demo        # creates group `demo` from the demo repo, generates an rmk_ key, deploys, calls the tool via the bridge
# → demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5
open https://localhost:8443   # self-signed cert; login admin@ramen.local / changeme-ramen
make down        # stop and remove volumes
```

The console is `https://localhost:8443` (accept the self-signed certificate), login **admin@ramen.local** /
**changeme-ramen** (from `deploy/local/.env`). The worker is a **gRPC** endpoint on `localhost:8080` that takes a
**`rmk_` MCP key** generated on the group page. Point Claude Desktop, Cursor or the `mcp` SDK at it with the bridge:

```sh
ramen-mcp-bridge --target localhost:8080 --insecure --key rmk_… --group demo --zone local
```

Full walkthrough with a Claude Desktop config, an `mcp` SDK snippet and a raw `grpcurl` call:
[Local quickstart](how-tos/local-quickstart.md). Coming from 0.3.0? The HTTP `/mcp` endpoint is gone:
[migration note](how-tos/migrate-0.3.1.md).

<figure markdown>
![Ramen console dashboard](img/dashboard.png){ .ramen-shot }
<figcaption>Dashboard: load per zone × group — low, even, high or down, labelled on every cell.</figcaption>
</figure>

## Deploy to the cloud

| Target | Status | Guide |
|---|---|---|
| GCP (GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB with gRPC header routing, Cloud Armor) | verified on a throwaway project: 0.3.0 infrastructure, then 0.3.2 gRPC header routing over h2c end to end through the Gateway | [GCP how-to](how-tos/gcp.md) |
| AWS (EKS, DynamoDB, S3, Secrets Manager, ALB gRPC target groups, WAF) | **built and unit-tested only; never applied to a real account** | [AWS how-to](how-tos/aws.md) |
| Local (docker compose) | CI e2e on every push | [Local quickstart](how-tos/local-quickstart.md) |

## Where next

- [How it works and why](how-it-works.md) · [Transport and security](wiki/transport.md) · [Architecture](architecture/index.md) · [Interface contracts](CONTRACTS.md)
- [Secrets](how-tos/secrets.md) · [DevOps with the API](how-tos/devops-api.md) · [Cloud-ops agent skills](wiki/skills.md)
- [Versions](versions.md) · [Releases](https://github.com/bkraad47/ramen/releases) · [Demo group repo](https://github.com/bkraad47/ramen-demo-mcp-group)

<!-- Machine-readable index for agents and crawlers: https://bkraad47.github.io/ramen/llms.txt (llmstxt.org) -->
