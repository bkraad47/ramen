# Groups, zones and regions

| Term | What it is | Who manages it |
|---|---|---|
| **Group** | A team. One git repo at a ref, one bucket prefix, its secrets, keys, members and service-account rules. Deleting a group destroys its cloud resources. | Super admins create and delete; group admins run it |
| **Environment** | A name shared by the groups it applies to: each at the git ref, deployed to the zones attached to it, with a per-group verbose flag, blocked-package list and last deploy result. | Super admins and group admins (for their groups) |
| **Zone** | Where workers run. A Kubernetes namespace `ramen-<group>-<zone>` pinned to a cloud zone, with its own service account, load-balancer route and IP rules. `local` is the compose worker. Names the environments it serves; its groups follow. | Super admins, and group admins for their own environments |
| **Worker** | One pod: the Rust node and the Python runtime. Two tracks per zone, `worker` and `worker-canary`. | Count and max by admins, scaled by Ramen in between; size by super admins |


## Environments across groups, zones attached to environments (1.0.0b)
*A group has many environments, an environment has many zones, a zone has workers.* An **environment** is a name
shared by the groups it applies to: `prod` for `demo` and `analytics` is one environment with one ref, run by each
group in the same zones. A **zone** names the environments it serves; the groups it serves follow from them. Every
group × zone pair the derivation yields has a worker record, a namespace and workers, and is **deployed the moment
it appears** (a new zone, a zone gaining an environment, an environment gaining a group, a region change) — one
canary deploy job per group/environment, visible on the group page. A pair that disappears is torn down (namespace,
workers, zone identity, record). Deleting an environment deletes the zones, with their workers, that no other
environment uses; a shared zone only loses it.

Everything is on one page, **Environments, Zones and Workers** (`/environments`), for super admins and group admins
(`/zones` redirects there): *New environment* (name, ref, groups), *Environments* (ref, groups, Save / Delete), *New
zone* (name, environments, location), *Zones* (location, environments, Save / Delete zone) and *Workers per group*
(the live cell per pair, with the environments that put it there). A group admin edits what is wholly their groups':
an environment whose groups are all theirs, a zone whose derived groups are all theirs; anything else is read-only,
and a zone that serves no group yet is a super admin's.

API: `GET /api/v1/environments/by-name` (`{name, ref, ref_differs, groups, zones, last_deploy: {group: …}}`),
`POST /api/v1/environments {"name": "prod", "ref": "main", "groups": ["demo"]}` (`409` for a name that exists —
add the group with `PUT`), `PUT /api/v1/environments/prod {"ref": …, "groups": […]}`, `DELETE
/api/v1/environments/prod` (`204`); `POST /api/v1/zones {"name": "a", "provider": "gcp", "region": "us-central1-a",
"environments": ["prod"]}` and `PUT /api/v1/zones/a {"region": …, "environments": […]}` answer the zone with
`environments`, the derived `groups` and the `jobs` started; the provider cannot change (`422`). A group's zones
are still `GET /api/v1/groups/<group>/zones`; the per-group `POST`/`PUT /api/v1/groups/<group>/environments…` keep
working as a shortcut (`zones` there attaches or detaches the environment on those zones), and `DELETE` there
removes only that group's doc. Per-group state — verbose, blocked packages, tool access, the last deploy — stays
per group on the group page. Zones from before 1.0.0b learn their environments at the first start.

## Zones and regions

A zone has a name, a provider and a region. The region is the cloud location the workers pin to: a GCP zone such
as `us-central1-a`, an AWS availability zone such as `us-east-1a`, or an Azure zone such as `westus2-1` (on an AKS
cluster without availability zones a Ramen zone there is a logical placement, see [Deploy on
Azure](deploy-azure.md#prerequisites)). The cluster is regional; a zone is one
location inside it. Two zones in two locations is what makes a group highly available: a client that fails on
`ramen-zone: a` retries with `ramen-zone: b` and reaches a different set of pods behind the same hostname.

**Environments, Zones and Workers → New zone** creates the record with the environments it serves, and deploys each
derived group's code there at once. Since 0.7.5 the form offers only the cloud this console runs on (`RAMEN_CLOUD`),
with that cloud's zones grouped by region. The page lists every zone with its environments and derived groups, then
one row per group × zone with the live workers, their load, and for super admins and group admins the **max workers**
and the last autoscale decision.

<figure markdown>
![The Environments, Zones and Workers page](../img/environments.png){ .ramen-shot }
<figcaption>Environments, Zones and Workers on a seeded local console: environments with their groups, zones with their environments, live workers per pair.</figcaption>
</figure>

A zone that is deleted, or dropped from the last environment that used it, is torn down for real: the namespace,
its pods and routes, and the zone's service account or IAM role.

### Blocked regions (0.7.0)

A super admin can keep workers out of whole regions. **Config → Blocked regions** lists the regions of the cloud the
console runs on (since 0.7.5 only that one; a save keeps the lists of the others). Group admins cannot change it.

- A blocked region refuses new zones: `403 Region <r> is blocked by a super admin`, on the page and at
  `POST /api/v1/zones`.
- A zone already in a region blocked later fails its next deploy with the same message in the job log and a server
  warning, which the warning-email digest carries to the notify list. Other zones of the environment still deploy.
- `GET|PUT /api/v1/config/regions` with `{"gcp": [...], "aws": [...], "azure": [...]}`. A GCP zone `us-west1-a`
  belongs to `us-west1`, an AWS availability zone `eu-west-1a` to `eu-west-1`.

On the Environments, Zones and Workers page the location is a dropdown of the provider's zones grouped by region, minus the blocked ones;
`local` zones keep a free-text location.

## Creating a group

**Groups → Create group** with a name, the repo URL and the ref. A private repo needs a secret named `GITHUB_TOKEN`
on the group, or a GitHub App installation id with the App configured on the Config page. The console clones;
workers never do.

The group page is where the group lives: environments with their zones and workers, deploy jobs, the packages each
zone serves, service-account permissions, MCP keys, the worker image and restrictions.

## Environments

**New environment** on [Environments, Zones and
Workers](#environments-across-groups-zones-attached-to-environments-100b): a name, a ref and the groups it applies
to; zones are attached to it from the same page. `dev` can follow `main` while `prod` sits on a tag. Rolling back is
deploying the older ref.

The **Environments** table on the group page is the one place for a group's zones: under each environment, one row
per zone with the zone (and its namespace on GKE), the live workers (refreshed as you watch), the size, the sleep
state, the IP allow-list and the service account, and under that **Zone actions** — a disclosure with the count and
max, sleep, IP rules, throttle, service account, rebalance and the logs. A zone two environments share is shown
under both.

The **Environments** page lists every environment you can see, across the groups you belong to, with the last
deploy flattened into the row: the outcome, when it ran, and the error if it failed. *Open group* goes to where
deploys are started.

<figure markdown>
![The environments page](../img/environments.png){ .ramen-shot }
</figure>

## Deploying

**Deploy (canary)** on an environment runs, per zone:

1. **Sync** the repo at the ref into the group's bucket prefix.
2. **Write config**: the zone's Secret with the MCP keys, the secrets scoped to this environment and zone, the
   blocked list, the verbose flag and the IP rules.
3. **Canary**: restart `worker-canary` in place and wait for it to be ready. A new worker image or size goes to
   the canary only; the stable Deployment keeps its pod template until the canary has passed.
4. **Reload and smoke**: the canary pip-installs if `requirements.txt` changed, loads the packages, then answers
   `tools/list`.
5. **Gates (0.7.0)**: the canary's tools, resources and prompts are compared with what the zone's stable workers
   serve. A removed tool, resource or prompt, a removed input, a new required input, a changed type or a narrowed
   choice list is **breaking** and stops the deploy; additive changes pass and are listed. Then the repo's **golden
   cases** (`mcp/tests.yaml`, see [The MCP repo](mcp-repo.md#golden-cases)) run against the canary with the group's
   first MCP key; one failing case stops the deploy and the log names the case and the difference.
6. **Stable**: restart `worker` with the new template and wait.

Any failure in steps 3 to 6 scales the canary to zero and leaves the stable pods untouched, a bad image pin
included. The job log on the group page shows each step; a stuck rollout names the pod that is not running and
the scheduler's reason. Since 1.0.0a deploy jobs are kept in the state store (`jobs`, the last 50 per environment),
so they are there after a console restart and on every replica; `GET /api/v1/jobs/{id}` reads the store. A job the
console was restarted under stays `running` in the store until `RAMEN_JOB_STALE_SECS` (default 1800) pass without a
change; then it is shown as `error: lost (console restarted)` and no longer blocks the next deploy. Backups do not
include jobs.

A deploy refused as breaking offers **Deploy again, accept breaking changes** on its job card (the API takes
`"breaking": true`). Clients that hold a session keep talking to the stable workers meanwhile and see the new set on
their next `tools/list`. The first deploy of a zone has nothing to compare against.

On AWS the canary's share of traffic moves to the stable track before its pods change and comes back once it has
passed. To deploy from a pipeline, see [Deploy from GitHub Actions](deploy-github-actions.md).

## Scaling and sizes

Since 0.7.5 Ramen scales the workers itself, between **1** and a **max** per zone that super admins and group
admins set under the zone's **Zone actions** on the group page (*Count (min 1)*, *Max (autoscale up to)*, **Scale workers**) or
on the Environments, Zones and Workers page. There is no HPA any more. How it decides, the cooldowns and the load tests are on
[Autoscaling](autoscaling.md). Super admins set the **size** (`s` 250m CPU and 512Mi, `m` 500m and 1Gi, `l` 1 CPU and 2Gi)
and the sizes a zone allows.

## Rebalance

The dashboard shows a load cell per zone and group: `low`, `even`, `high` or `down`, from each node's in-flight
count, where `low` is under 30% of the node's in-flight cap and `high` is over 80%. **Rebalance and autoscale** in
*Zone actions* adjusts the load balancer (on GCP the backend's capacity scaler, on AWS the target-group weights) and
makes the [autoscaling](autoscaling.md) decision for the zone. While the Gateway is
still reconciling it answers `applied: false` with a note and retries in the background. A super admin can turn on
the **auto-rebalance scheduler** on the Config page; since 0.7.5 it visits every group × zone each interval, and each
run is audited as `scheduler`.

## Packages per zone

After a deploy the group page lists what each zone reported: tools, resources and prompts with their schemas.
Since 0.7.0 each zone also shows its **manifest**: the hash of everything the workers serve (the same hash the
worker reports on its metrics) with the names and the exact schemas a client sees in a collapsed section. The live
workers cell shows each worker's hash and flags a **manifest mismatch** when the workers of one zone disagree, which
is what a half-finished rollout looks like.

**Disable** a package on one zone, or add it to **Blocked everywhere** on the environment. A blocked name
disappears from `tools/list` and answers the JSON-RPC error `-32601` until it is enabled again. The node enforces
it, so a bad tool can be pulled without a code change.

### Tool access (0.7.2)

Per environment, a group admin can restrict single tools: who may **see** a tool in `tools/list` and who may **call**
it, by kind of caller: group keys, group admins, viewers, MCP users (super admins always may; a kind that may call may
list). A tool left out of the table is open to everyone with access to the group, as before. A caller who may not list
a tool gets `-32601 tool not found`; one who may list but not call gets `-32003 forbidden`. The worker enforces it on
both transports on the next deploy. Blocking (above) still removes a tool for everyone.

Since 0.7.5 the kinds include the [custom roles](users-access.md#custom-roles) a super admin defined: a caller whose
token carries `analyst` matches an entry naming `analyst` or its base. Access is decided before any
[guardrail](guardrails.md) runs, so a caller who may not call a tool gets `-32003` and no rail ever sees the payload.

### Drift (0.7.2)

The group page's **Drift** card shows, per zone and tool, how often the same caller repeated the same call (same
arguments, hashed, never stored) within a minute, from the worker's call log. A high rate is what an agent retrying
or abandoning a tool looks like. Above the threshold on the Config page (default 30 %) the console logs a warning the
digest email carries. `GET /api/v1/groups/{g}/zones/{z}/drift`.

## IP rules

**IP rules** under the zone's *Zone actions* on the group page takes a CIDR list. It becomes the node's allow-list and an edge
rule: a Cloud Armor policy per group on GCP, a WAF IP set per group on AWS, an Envoy Gateway `SecurityPolicy` per
zone route on AKS. **The Cloud Armor policy is one per group**, so two zones of the same group share the edge rule
even though their node allow-lists stay separate. An empty list allows every source. Include the console's own range, because a deploy smoke-tests the
worker as an ordinary caller. Changing rules rolls the zone's pods.

## Deleting

Deleting an environment tears down its zones' deployments for the group. Deleting a group tears down everything
it owned, in every zone, including its Cloud Armor policy on GCP and its WAF IP sets on AWS. On GKE a namespace can
sit in `Terminating` for a while; [Deploy on GCP](deploy-gcp.md#gotchas) says what to do if it never clears.
