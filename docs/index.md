---
title: Ramen
description: Multizone, highly available, enterprise-grade MCP server for GCP and AWS Kubernetes.
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
  "softwareVersion": "0.3.0",
  "license": "https://opensource.org/licenses/BSD-3-Clause",
  "url": "https://bkraad47.github.io/ramen/",
  "codeRepository": "https://github.com/bkraad47/ramen",
  "downloadUrl": "https://github.com/bkraad47/ramen/releases",
  "programmingLanguage": ["Rust", "Python"],
  "description": "Multizone, highly available, enterprise-grade MCP server for GCP and AWS Kubernetes. A Rust MCP node and a Python 3.14 runtime run your tools, resources and prompts from a git repo; a FastAPI console manages groups, environments, zones, secrets, canary deploys, IP rules and audit.",
  "keywords": "MCP, Model Context Protocol, MCP server, self-hosted, Kubernetes, GKE, EKS, JSON-RPC, agent tools, Rust, Python, canary deploy, multi-zone, high availability",
  "author": {"@type": "Person", "name": "Raad", "url": "https://github.com/bkraad47"},
  "offers": {"@type": "Offer", "price": "0", "priceCurrency": "USD"},
  "isAccessibleForFree": true
}
</script>

<div class="ramen-hero" markdown>
![Ramen logo](img/logo.png){ width=180 }

# Ramen

<p class="tag">Multizone, highly available, enterprise-grade <b>MCP server</b> for GCP and AWS Kubernetes.</p>

<div class="ramen-badges" markdown>
[![release](https://img.shields.io/github/v/release/bkraad47/ramen?color=F26B3A&label=release)](https://github.com/bkraad47/ramen/releases)
[![ci](https://github.com/bkraad47/ramen/actions/workflows/ci.yml/badge.svg)](https://github.com/bkraad47/ramen/actions/workflows/ci.yml)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-F26B3A)](https://github.com/bkraad47/ramen/blob/main/LICENSE)
[![MCP](https://img.shields.io/badge/MCP-Streamable%20HTTP%202025--06--18-2B2622)](https://modelcontextprotocol.io)
</div>
</div>

Ramen turns a **git repo of tools, resources and prompts** into a fleet of MCP workers behind a cloud load balancer.
Each worker is a **Rust MCP node** (protocol, auth, IP allow-lists, metrics) paired 1:1 with a **Python 3.14 runtime**
that runs your code. A single **FastAPI console** manages groups, environments, zones, secrets, canary deploys,
rebalancing, logs and audit, and every action is also available through an API key.

<div class="ramen-grid" markdown>
<div markdown>
### Git → bucket → worker
Push `mcp/tools/<name>/<name>.py` + `<name>.json` to a repo. Deploy syncs it to a bucket; workers pip-install and load it. [Protos →](wiki/protos.md)
</div>
<div markdown>
### Canary by default
Every deploy rolls a canary first, smoke-tests `tools/list`, then rolls the stable track. Failure = canary scaled to 0, stable untouched. [Canary →](wiki/canary.md)
</div>
<div markdown>
### Multi-zone from day one
Group → Environment → Zone → Worker. One zone today, N tomorrow, same data model, same LB. [Concepts →](wiki/concepts.md)
</div>
<div markdown>
### Enterprise controls
Super admin / group admin / viewer, `rmk_` MCP keys, `rmn_` API keys, per-zone IP rules (Cloud Armor / WAF), secrets never shown, full audit log. [Security →](how-tos/security.md)
</div>
</div>

## Quickstart (local, 5 commands)

Needs Docker (compose v2), `uv`, `git`. About 3–5 minutes on the first run (image builds).

```sh
git clone https://github.com/bkraad47/ramen && cd ramen
make up          # Firestore emulator + console (https://localhost:8443) + one worker (http://localhost:8080)
make demo        # creates group `demo` from the demo repo, mints an rmk_ key, deploys, calls the tool
# → demo_calculator_tool({"var1": 2, "var2": 3, "func": "add"}) -> 5
open https://localhost:8443   # self-signed cert; login admin@ramen.local / changeme-ramen
make down        # stop and remove volumes
```

The console is `https://localhost:8443` (accept the self-signed certificate), login **admin@ramen.local** /
**changeme-ramen** (from `deploy/local/.env`). The worker's MCP endpoint is `http://localhost:8080/mcp` with a
**`rmk_` MCP key** minted on the group page. Full walkthrough with a `curl` example and the difference between
`rmk_` and `rmn_` keys: [Local quickstart](how-tos/local-quickstart.md).

<figure markdown>
![Ramen console dashboard](img/dashboard.png){ .ramen-shot }
<figcaption>Dashboard: load per zone × group (blue = low, green = even, red = high).</figcaption>
</figure>

## Deploy to the cloud

| Target | Status | Guide |
|---|---|---|
| GCP (GKE Autopilot, Firestore, GCS, Secret Manager, global HTTPS LB, Cloud Armor) | verified on a throwaway project, 89/89 cloud tests | [GCP how-to](how-tos/gcp.md) |
| AWS (EKS, DynamoDB, S3, Secrets Manager, ALB, WAF) | **built and unit-tested only; never applied to a real account** | [AWS how-to](how-tos/aws.md) |
| Local (docker compose) | CI e2e on every push | [Local quickstart](how-tos/local-quickstart.md) |

## Where next

- [How it works and why](how-it-works.md) · [Architecture](architecture/index.md) · [Interface contracts](CONTRACTS.md)
- [Secrets](how-tos/secrets.md) · [DevOps with the API](how-tos/devops-api.md) · [Cloud-ops agent skills](wiki/skills.md)
- [Versions](versions.md) · [Releases](https://github.com/bkraad47/ramen/releases) · [Demo group repo](https://github.com/bkraad47/ramen-demo-mcp-group)
- For LLM agents: [`llms.txt`](llms.txt)
