# SkillSpector scans (1.0.0c Phase 2)

[NVIDIA SkillSpector](https://github.com/NVIDIA/SkillSpector) (Apache-2.0) is a command-line scanner for agent
skills: static rules for prompt injection, exfiltration, supply chain, MCP tool poisoning and least privilege, plus an
optional LLM semantic pass, summed into a 0–100 risk score — `SAFE` (≤ 20), `CAUTION` (≤ 50), `DO_NOT_INSTALL`.
Since 1.0.0c Phase 2 Ramen runs it on every deploy over the skills a group repo ships *and* over what the zone really
serves, keeps the report on a page per zone, and can gate the deploy on it. The same release teaches the bridge to
scan the skills on the developer's machine before Ramen is used. Ramen pins tag **v2.13.4**.

Contract: [§30](../CONTRACTS.md). Everything here is off until the engine is turned on in the chart.

## Turning it on

```yaml title="values.yaml"
skillspector:
  engine: skillspector        # none (default) | skillspector
  image: ghcr.io/bkraad47/ramen-skillspector:2.13.4
```

SkillSpector has no PyPI package, no image and no server, so Ramen publishes one: `deploy/docker/skillspector/Dockerfile`
clones the pinned tag and runs the upstream multi-stage build (non-root, `WORKDIR /scan`, `ENTRYPOINT ["skillspector"]`),
released as `ghcr.io/bkraad47/ramen-skillspector:2.13.4` and `:latest` next to the console and worker images
(`scripts/check_versions.py` keeps the tag in one place). `engine: skillspector` hands the console
`RAMEN_SKILLSPECTOR_IMAGE` (+ `RAMEN_SKILLSPECTOR_TIMEOUT_SECS`, 600). Nothing else runs; the Config page shows the
engine and the image under **Evals & scans**.

## What is scanned, and when

A deploy of an environment, per zone, after the [evals](evals.md) of that zone, when the engine is on:

1. The console builds a scan directory with every immediate subdirectory of the repo's `mcp/skills/` that has a
   `SKILL.md` (the same synced copy the image build uses) and one synthesized skill, `ramen-served-<zone>/SKILL.md`,
   written from the canary's `tools/list` and `prompts/list` — name, title, description and input schema as fenced
   JSON — so the instruction-text rules run against what the zone actually serves, not only against what the repo
   says.
2. A Job `ramen-scan-<group>-<zone>-<rand>` in `ramen-system` (ServiceAccount `ramen-evals`, the evals Jobs' identity,
   never the builder's) runs `skillspector scan /scan --recursive --format json -o /out/report.json` (upstream caps a
   recursive scan at 32 skills), with `--no-llm` or
   `SKILLSPECTOR_PROVIDER=<llm>` from the group setting below. The directory travels as a ConfigMap (≤ 900 KiB; a
   larger `mcp/skills` tree is an error of the run naming the size); the report is read back from the pod log.
3. The job records `job.result[<zone>].skillspector = {score, severity, recommendation, skills, findings, url}`
   (`findings` is a count) and keeps the full report (≤ 256 KiB, else `truncated: true`) on the job for the report
   page.

An optional `mcp/skillspector-baseline.yaml` in the repo is applied to single-skill scans only — upstream rejects it
with `--recursive`, so with a baseline Ramen scans skill by skill and merges the results.

### The report page

`GET /groups/<g>/scans/<job>/<zone>` (group members) is a table per skill with each finding's rule, severity,
`file:line` and message. The tool has no UI of its own, so this page is where the group page's Prompts/Packages
section and the per-zone badge point.

### Gate or report

Group admins set `skillspector: {gate, required, llm}` on the group page next to evals; the API is
`PUT /api/v1/groups/{g}/skillspector`.

| Setting | Means |
|---|---|
| `gate: false` (default) | Report only: the result on the job, the badge on the group page, the report page. |
| `gate: true` | `DO_NOT_INSTALL` or a run error fails the deploy exactly like a failed golden case (canary scaled down, stable workers untouched). `CAUTION` is a warning line. |
| `required` | The client policy switch, [below](#client-policy-the-bridges-local-scan). |
| `llm` | The name of a configured LLM provider for the semantic pass, or `""` for the static rules only (`--no-llm`). |

## LLM providers, part 2

The scan's semantic pass and the evals share **Config → LLM providers**. Phase 2 added four providers, which since
D82 are rows of that one card: Vertex AI (`project`, `location`; the Job's ServiceAccount through Workload Identity, no key), Bedrock
(`region`; the Job's IRSA identity, no key), Azure AI Foundry (`endpoint`, `api_key`, `deployment`) and any
OpenAI-compatible server (`base_url`, `api_key`, `headers`). Values show as set / not set; only providers that are set
are exported to the Jobs (`GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`, `AWS_REGION`, `AZURE_OPENAI_*`,
`SKILLSPECTOR_COMPAT_*`).

!!! note "Vertex and Bedrock are the operator's grant"
    The Jobs run as `ramen-evals` (§33.4a), which holds no AI role. For an evals provider on Vertex AI give that
    identity `roles/aiplatform.user`; for Bedrock (evals or a scan's LLM pass), `bedrock:InvokeModel`. SkillSpector
    has no Vertex provider: a scan with `llm: vertex` runs the static rules only (`--no-llm`). Documented, not automated. A group's
    guardrails are different: they reach Vertex and Bedrock through the console's judge proxy as the console's
    identity, which Terraform grants (`console_vertex`, `console_bedrock`).

## Client policy: the bridge's local scan

When the chart runs the engine or the group sets `required`, the `/oauth/token` answer gains
`skillspector: {version, required, llm, baseline}` next to `telemetry` and `evals`; `version` is the first 12 hex
digits of a SHA-256 over `required`, `llm` and the baseline text, and `/.well-known/ramen-policy` answers
`skillspector: {version, required}`. With `required` the worker gate applies: a call must carry
`_meta["io.ramen/skillspector"] = {"version"}` (a 2025-06-18 body through the bridge: `params.skillspector`) —
missing → `-32027 skillspector policy required` (HTTP 428), another version → `-32027 skillspector policy changed:
sign in again`; it runs after the evals gate.

**Bridge 1.0.0**, the bridge for Ramen 1.0.0, keeps `skillspector` in its policy bundle, carries the version on every call and signs in again
once on `-32027`. When the group requires it, `policy check` — the Claude Code `SessionStart` hook, the Cursor rule,
`policy show` — also runs `skillspector scan <dir> --recursive --no-llm --format json` over the client's own skill
directories (`~/.claude/skills`, `<cwd>/.claude/skills`, `~/.codex/skills` when present) with a local `skillspector`
(`uv tool install 'git+https://github.com/NVIDIA/SkillSpector.git@v2.13.4'` is the hint when it is missing). A
`DO_NOT_INSTALL` for any skill, or no binary, is **exit 3** with the skills named and `Ramen policy: fix or remove
these skills — Ramen cannot be used with them`; `CAUTION` is printed. Results are cached per directory content hash in
`~/.ramen/scans/<host>/` for 24 h so hooks stay fast. `--no-client-scan` turns the local scan off; the bundle and the
version contract stay. The same bundle carries the group's `guardrails` policy since 1.0.0c Phase 2.5 — the rails
the workers run, which the bridge runs locally too and the workers gate with `-32029` ([Guardrails](guardrails.md)).

### The honest limit

A scan reads instruction text. Ramen blocks what SkillSpector's rules recognise in the repo's skills, in the tools
and prompts a zone serves, and in the skill directories of a machine that goes through the bridge; it cannot see a
skill a client loads from elsewhere, nor a malicious instruction phrased past the rules (the LLM pass narrows that
gap, it does not close it). As with the [evals policy](evals.md#the-honest-limit), what runs through Ramen is covered;
the rest is a convention the hook and the rule state.

## What is verified

- `tests/conformance/test_console_skillspector.py` — a real console with a fake scan runner and a group repo the test
  owns: the group setting, a SAFE scan with the synthesized skill and the report page for members only, a planted
  `SKILL.md` that reads `~/.ssh` and `curl`s it away reported with the gate off and blocked with it on, CAUTION
  through the gate, a run error, the baseline, `skillspector` in the token answer and the policy endpoint.
- `tests/cloud/test_release_features.py` (`RAMEN_SKILLSPECTOR=1`) — on a cluster with the real image: a deploy of
  the demo repo reports `SAFE` or `CAUTION`, the report page renders, the policy endpoint matches the token answer,
  the Config page names the engine and the providers.
- `scripts/cloud_smoke.sh` prints `skillspector: <recommendation>, score <n>, <n> findings` after the deploy.
- The node's `-32027` gate and the bridge's local scan (a fake `skillspector` on PATH: clean / caution / block /
  missing, the cache, `--no-client-scan`) are covered by their own unit tests.
