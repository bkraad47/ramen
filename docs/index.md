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
      "softwareVersion": "1.0.0",
      "license": "https://opensource.org/licenses/BSD-3-Clause",
      "url": "https://bkraad47.github.io/ramen/",
      "downloadUrl": "https://github.com/bkraad47/ramen/releases",
      "releaseNotes": "https://bkraad47.github.io/ramen/changelog/",
      "softwareHelp": "https://bkraad47.github.io/ramen/get-started/",
      "screenshot": "https://bkraad47.github.io/ramen/img/dashboard.png",
      "description": "Self-hosted, multi-zone, highly available MCP (Model Context Protocol) server for Kubernetes on GKE, EKS and AKS. A Rust MCP node speaks Streamable HTTP and JSON-RPC 2.0 over gRPC, MCP 2026-07-28 (tasks, MCP Apps, Skills over MCP) and 2025-06-18 on one endpoint; a Python 3.12 runtime runs your tools, resources, prompts, apps and skills from a git repo, with NeMo Guardrails per tool; a FastAPI console manages groups, environments, zones, secrets, canary deploys with golden cases, promptfoo evals and NVIDIA SkillSpector scans, roles per group, OAuth 2.1 sign-in with Entra ID and Google Workspace groups, client policies enforced on every sign-in, telemetry per group and user in Phoenix, autoscaling, sleeping workers, satellite clusters, IP rules, throttling and audit.",
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
      "version": "1.0.0",
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

Ramen is a self-hosted, multizone, highly available MCP server for GCP, AWS and Azure Kubernetes. Your team keeps
**a git repo of tools, resources, prompts, apps and skills in plain Python**. Ramen turns it into a fleet of MCP
workers behind a cloud load balancer, gates every deploy with golden cases, evals and a skill scan, checks every call
with [guardrails](wiki/guardrails.md) (NeMo rails, static and LLM-judged), signs people in with a role per group, and
enforces the group's client policies on every call. Underneath it is gRPC, JSON-RPC 2.0
and a Rust node. **You code in Python.**

<section class="ramen-family" aria-label="The Ramen project" markdown>
<p class="ramen-family__lead"><strong>One project, three repositories</strong> — the server you install, the bridge
your people run, and a group repo to copy.</p>

<div class="ramen-family__grid" markdown>

<div class="ramen-repo ramen-repo--server" markdown>
<span class="ramen-repo__role">:material-server-network: Ramen · the server</span>
<h3 class="ramen-repo__name"><a href="https://github.com/bkraad47/ramen">ramen</a></h3>
<p class="ramen-repo__text">What you install: the console, the Rust MCP node and the Python runtime, Helm charts and
Terraform for GCP, AWS and Azure. Releases are tagged and built by CI.</p>
<p class="ramen-repo__links"><a href="https://github.com/bkraad47/ramen">GitHub</a> · <a href="https://hub.docker.com/u/bkraad47">Docker Hub</a> · <a href="changelog/">Changelog</a></p>
</div>

<div class="ramen-repo ramen-repo--bridge" markdown>
<span class="ramen-repo__role">:material-bridge: Ramen · the client bridge</span>
<h3 class="ramen-repo__name"><a href="https://github.com/bkraad47/ramen-mcp-bridge">ramen-mcp-bridge</a></h3>
<p class="ramen-repo__text">What your people run: <code>pip install ramen-mcp-bridge</code>, a stdio MCP server for
clients that cannot speak HTTP. Signs in through the console or carries a group key, keeps the group's policy and
writes its client configs. Bridge 1.0.0 is the bridge for Ramen 1.0.0: TLS by default, plaintext only with an
explicit <code>--insecure</code> for local development.</p>
<p class="ramen-repo__links"><a href="https://github.com/bkraad47/ramen-mcp-bridge">GitHub</a> · <a href="https://pypi.org/project/ramen-mcp-bridge/">PyPI</a> · <a href="wiki/connect-oauth/">Connect a client</a></p>
</div>

<div class="ramen-repo ramen-repo--demo" markdown>
<span class="ramen-repo__role">:material-bowl-mix-outline: Ramen · the demo group</span>
<h3 class="ramen-repo__name"><a href="https://github.com/bkraad47/ramen-demo-mcp-group">ramen-demo-mcp</a></h3>
<p class="ramen-repo__text">What you copy: the group repo every guide deploys — tools (one asks before it acts, one
runs as a task, Iris data tools), resources, prompts, an MCP App, skills, golden cases, NeMo rails, promptfoo evals and
LLM configs.</p>
<p class="ramen-repo__links"><a href="https://github.com/bkraad47/ramen-demo-mcp-group">GitHub</a> · <a href="wiki/mcp-features/">MCP features</a> · <a href="wiki/mcp-repo/">The MCP repo</a></p>
</div>

</div>
</section>

<nav class="ramen-paths" aria-label="Where to start" markdown>

<a class="ramen-path ramen-path--primary" href="get-started/" markdown="span">
<span class="ramen-path__step">1 · Try it</span>
<span class="ramen-path__title">:material-rocket-launch-outline: Get started</span>
<span class="ramen-path__text">Ramen on your laptop in a minute: start it, connect a repo of tools, connect a client.</span>
<span class="ramen-path__go">Start locally :material-arrow-right:</span>
</a>

<a class="ramen-path" href="first-group/" markdown="span">
<span class="ramen-path__step">2 · Deploy</span>
<span class="ramen-path__title">:material-cloud-upload-outline: Deploy your first group</span>
<span class="ramen-path__text">On GCP, AWS or Azure: a group, a zone, the deploy gates, evals in promptfoo, traces in Phoenix.</span>
<span class="ramen-path__go">Walk through it :material-arrow-right:</span>
</a>

<a class="ramen-path" href="how-it-works/" markdown="span">
<span class="ramen-path__step">3 · Understand</span>
<span class="ramen-path__title">:material-sitemap-outline: How it works</span>
<span class="ramen-path__text">Console, node and runtime; environments, zones and workers; what is enforced on every call.</span>
<span class="ramen-path__go">Read the overview :material-arrow-right:</span>
</a>

</nav>

## Key features

<div class="ramen-grid" markdown>
<div markdown>
### The protocol
- **MCP 2026-07-28** next to 2025-06-18 on one endpoint, per request: stateless requests, standard headers,
  `server/discover`, subscriptions ([Transport](wiki/transport.md)).
- **Tasks**, **multi round-trip requests** (elicitation, sampling, roots), progress, completion.
- **MCP Apps** (`ui://`) and **Skills over MCP** (`skills/list`, `skill://`) from folders in the repo.
- **Streamable HTTP and gRPC** on one port, one set of guards; a stdio **bridge** for every other client.
</div>
<div markdown>
### Quality and safety gates
- **Canary deploys** with a schema compatibility gate and **golden cases**.
- **promptfoo evals** from the repo, run on every deploy, report or gate per group.
- **NVIDIA SkillSpector** scans of the repo's skills and the served tools, with a report page.
- **Guardrails** in the worker: NeMo rails, static or **LLM-backed**, or your own `policy.py`.
</div>
<div markdown>
### Enforced on every sign-in
- **Client policies**: telemetry, evals, scans, LLM configs and guardrails carry versions every call must hold
  (`-32024`…`-32029`), on every sign-in path and every client.
- **LLM configs** from the repo, accepted per person and version, written to their tools by the bridge.
- **Telemetry by group and user**: one Phoenix project per group, `ramen.user` on every span, client telemetry
  through the console's OTLP ingest.
- **LLM judge proxy**: provider keys never leave the console.
</div>
<div markdown>
### Run it
- **GCP, AWS and Azure** from Terraform and Helm; optional Phoenix, promptfoo and SkillSpector with one switch each.
- **Roles per group** (Super admin, Group admin, Viewer, MCP user, custom roles), Entra ID and Google Workspace.
- **Autoscaling** by Ramen on load and latency; **sleeping workers**; **Redis throttling**.
- **Per-group image builds** on deploy; **satellite clusters** for zones in other regions.
- **TLS on every hop** with a per-install CA; plaintext refused on cloud installs.
</div>
</div>

**What is verified.** The 1.0.0c final runs passed on GKE, EKS and AKS (smoke and the policy, guardrail, access
and security suites), with load tests on all three (every miss recorded) and two independent security passes on GKE; what each covered
and what is open: [Transport → What is verified](wiki/transport.md#what-is-verified-and-what-is-not) and the
[Threat model](threat-model.md#findings-and-their-status). Everything else is in the [wiki](wiki/index.md); to let a coding
agent deploy and run it, install the [agent skills](wiki/skills.md).
