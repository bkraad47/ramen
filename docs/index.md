---
title: Governed MCP for teams
description: Ramen is a self-hosted MCP platform that enforces your organisation's security and structure on every tool call — SSO roles per group, NeMo guardrails per tool, promptfoo evals and SkillSpector scans gating deploys, Phoenix traces — on a laptop or across zones on GKE, EKS and AKS.
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
      "description": "Governed MCP for teams: a self-hosted MCP (Model Context Protocol) platform that enforces an organisation's security and structure on every tool call, multi-zone on Kubernetes (GKE, EKS, AKS) or one worker on a laptop. A Rust MCP node speaks Streamable HTTP and JSON-RPC 2.0 over gRPC, MCP 2026-07-28 (tasks, MCP Apps, Skills over MCP) and 2025-06-18 on one endpoint; a Python 3.12 runtime runs your tools, resources, prompts, apps and skills from a git repo, with NeMo Guardrails per tool; a FastAPI console manages groups, environments, zones, secrets, canary deploys with golden cases, promptfoo evals and NVIDIA SkillSpector scans, roles per group, OAuth 2.1 sign-in with Entra ID and Google Workspace groups, client policies enforced on every sign-in, telemetry per group and user in Phoenix, autoscaling, sleeping workers, satellite clusters, IP rules, throttling and audit.",
      "keywords": "MCP, MCP server, Model Context Protocol, MCP governance, governed MCP, shadow MCP, AI governance, agent guardrails, NeMo Guardrails, promptfoo, LLM evals, NVIDIA SkillSpector, skill scanning, Arize Phoenix, OpenTelemetry, self-hosted MCP, MCP 2026-07-28, MCP tasks, multi-zone, Kubernetes, GKE, EKS, AKS, GCP, AWS, Azure, SSO, OAuth 2.1, Streamable HTTP, gRPC, canary deploy, Rust, Python",
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
      "isAccessibleForFree": true,
      "featureList": [
        "Sign-in with Microsoft Entra ID, Google or any OIDC provider; roles per group, custom roles, per-tool access; OAuth 2.1 authorization server",
        "NVIDIA NeMo Guardrails per tool, before the call and after the result, static or LLM-judged, fail-closed",
        "promptfoo evals and NVIDIA SkillSpector scans against a canary on every deploy, optionally gating it",
        "Group LLM configs and policies versioned, accepted per person and checked by the worker on every call",
        "Arize Phoenix / OpenTelemetry traces per group and person; per-call access log; audit log of console actions",
        "MCP spec 2026-07-28 and 2025-06-18 on one endpoint: stateless requests, tasks, multi round-trip requests, MCP Apps, Skills over MCP",
        "Autoscaling on load and latency, sleeping zones at zero workers, satellite clusters, TLS on every hop"
      ],
      "discussionUrl": "https://github.com/bkraad47/ramen/discussions"
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
      "keywords": "MCP, MCP server, Model Context Protocol, MCP governance, governed MCP, shadow MCP, AI governance, agent guardrails, NeMo Guardrails, promptfoo, LLM evals, NVIDIA SkillSpector, skill scanning, Arize Phoenix, OpenTelemetry, self-hosted MCP, MCP 2026-07-28, MCP tasks, multi-zone, Kubernetes, GKE, EKS, AKS, GCP, AWS, Azure, SSO, OAuth 2.1, Streamable HTTP, gRPC, canary deploy, Rust, Python",
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
<td><a href="https://modelcontextprotocol.io"><img src="https://img.shields.io/badge/MCP-2026--07--28%20%2B%202025--06--18-2B2622?style=flat" alt="MCP"></a></td>
</tr>
</table>

<p class="ramen-glama"><a href="https://glama.ai/mcp/servers/bkraad47/ramen"><img width="380" height="200" src="https://glama.ai/mcp/servers/bkraad47/ramen/badge" alt="Ramen on Glama: score, tools and listing"></a></p>

**Governed MCP for teams.** Ramen is a self-hosted MCP platform that enforces your organisation's security and
structure on every tool call — the sanctioned home for your team's tools instead of shadow MCP servers on laptops.
Your team keeps **a git repo of tools, resources, prompts, apps and skills in plain Python**. Ramen signs people in
with a role per group, checks every call with [guardrails](wiki/guardrails.md) (NVIDIA NeMo Guardrails, static and
LLM-judged), gates every deploy with golden cases, [promptfoo evals](wiki/evals.md) and an
[NVIDIA SkillSpector scan](wiki/skillspector.md), and records every call — in the access log, the audit log and
[Arize Phoenix](wiki/telemetry.md). It runs on a laptop or across zones on GKE, EKS and AKS, and speaks MCP
2026-07-28. **You code in Python.**

<figure markdown>
![How Ramen governs your MCP tools: sign-in and roles, guardrails per tool, evals and scans before a tool goes live, tools on workers, logs and traces; the answer and the group's rules go back to the client](img/how-ramen-governs.svg){ .ramen-shot }
</figure>

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
<span class="ramen-path__title">:material-rocket-launch-outline: Try the demo</span>
<span class="ramen-path__text">From nothing to a governed MCP server on your laptop, step by step with a picture per step: guardrails, sign-in, traces, a client calling a tool.</span>
<span class="ramen-path__go">Start the walkthrough :material-arrow-right:</span>
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

## Why teams pick it

- **Sanctioned MCP, not shadow MCP.** Tools in a repo you review, on your cluster, answering only people who signed
  in with your identity provider and hold a role in the group.
- **No eval, no deploy.** A canary first; golden cases always, promptfoo evals and a SkillSpector scan when the engines
  are on, and a group admin can make either stop the deploy.
- **Guardrails per tool, not per chat.** Rails run inside the worker for every client, before the tool and after it.
- **Every tool call has a name.** A person's sign-in or a named key on every call; access log, audit log, Phoenix.
- **It directs the LLM.** The group's rules and client configs are versioned, accepted per person and checked on
  every call.
- **Scales for what you need now**, and **follows MCP's 2026 direction**: stateless requests, tasks, multi
  round-trip requests, MCP Apps and Skills over MCP.

## Built-in governance and dev tools

| Tool | What it does in Ramen | Where it runs |
|---|---|---|
| [NVIDIA NeMo Guardrails](https://github.com/NVIDIA-NeMo/Guardrails) | rails per tool, before the call and after the result | in every worker, on the laptop too |
| [promptfoo](https://github.com/promptfoo/promptfoo) | the repo's eval suite against the canary on every deploy | a Job in the cluster |
| [NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) | scans the repo's skills and the served tools on every deploy | a Job in the cluster |
| [Arize Phoenix](https://github.com/Arize-ai/phoenix) | traces, one project per group, the person on every span | next to the console, and on the laptop |

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

**What is verified.** The 1.0.0 release candidate ran live on GKE (with a satellite cluster), EKS and AKS: a demo
deploy with evals 6 passed and a SkillSpector SAFE verdict, a tool call through each load balancer, the full
verification suite and load tests with p95 under 1.5 s on all three, after two independent security passes on GKE; what
each covered and what is open: [Transport → What is verified](wiki/transport.md#what-is-verified-and-what-is-not) and the
[Threat model](threat-model.md#findings-and-their-status). Everything else is in the [wiki](wiki/index.md); to let a coding
agent deploy and run it, install the [agent skills](wiki/skills.md).

## Fork it, shape it, talk to us

Ramen is BSD-3-Clause and made to be forked: copy the
[demo group repo](https://github.com/bkraad47/ramen-demo-mcp-group) for your own tools, or fork Ramen and change it
for your organisation. Collaboration is welcome — questions, ideas and what you built go to
**[GitHub Discussions](https://github.com/bkraad47/ramen/discussions)**; how changes get in is in
[Contributing](contributing.md).
