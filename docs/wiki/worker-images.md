# Worker images: the release image, one built on deploy, or one you pin

A zone's workers run one of three images. The first that applies wins:

| | Image | Who sets it |
|---|---|---|
| 1 | **A pinned image**: a build you made and recorded on the group | a super admin, on the group page |
| 2 | **An image built on deploy** (1.0): the release image plus the repo's `mcp/requirements.txt` | Ramen, when the group builds and the deployment has a builder |
| 3 | **The release image** (`RAMEN_IMAGE_WORKER`) | the installation |

With the release image a worker pip-installs `mcp/requirements.txt` when it first loads a new ref
([The MCP repo](mcp-repo.md#heavy-dependencies)). The other two exist so that heavy dependencies are in the image
and a new worker pod starts without installing anything.

The group page's **Worker image** card shows which one is current (*pinned*, *built on deploy* or *The release
image*) and the history of recorded and built images.

## What the release images are built on

Every image Ramen builds starts from an official slim image named by language minor only — no patch version, no
Debian codename, no digest — so the security rebuilds upstream publishes under that tag reach Ramen's next build.
As of 1.0.0 these tags are Debian 13 (trixie):

| Image | Build stage | Runtime base |
|---|---|---|
| worker (`node-rs/Dockerfile`) | `rust:1.99-slim` (cross-compiles for the target) | `python:3.12-slim` (the runtime is pinned to 3.12, D45) |
| console (`console/Dockerfile`) | — (`uv` 0.12 copied in) | `python:3.14-slim` |
| SkillSpector (`deploy/docker/skillspector`) | `python:3.12-slim` | `python:3.12-slim` |
| Glama (`glama/Dockerfile`, `glama/Dockerfile.standalone`) | `rust:1.99-slim` | `python:3.12-slim` |

The Rust minor follows `rust-toolchain.toml`. An image built on deploy starts `FROM` the worker image, so it inherits
these. Dependabot proposes updates weekly once a release is 14 days old; a new Python or Rust minor is a deliberate
bump. To freeze an install on one exact build of Ramen's images, use the chart's `image.digest` / `image.workerDigest`.

## Images built on deploy

### When a build happens

A deploy builds first, before any canary, when all of this is true:

- **Build on deploy** is on for the group (below),
- the checkout has an `mcp/requirements.txt` with at least one requirement — **a file of comments and blank lines
  builds nothing**,
- the group has no pinned image (the deploy log then says the pin wins and nothing is built),
- no image with the same tag was already built successfully in the group's own repository.

The tag is

```
b-<group>-<first 16 hex characters of sha256(base image reference + "\n" + the bytes of requirements.txt)>
```

so a build happens again when the requirements file changes by so much as a byte, or when the base image does (a
new Ramen release). A second deploy with neither changed logs `already built for these requirements` and builds
nothing.

What is built:

```dockerfile
FROM <base worker image>
COPY requirements.txt /opt/ramen/requirements.txt
RUN /opt/venv/bin/pip install --no-cache-dir -r /opt/ramen/requirements.txt
ENV RAMEN_DEPS_BAKED=<sha256 of requirements.txt>
```

### Where the image lives

In the deployment's own registry, in **the group's own repository** (1.0.0c, D85), which the console creates on the
group's first build (on ACR the first push creates it):

| Cloud | Image |
|---|---|
| GCP | `<region>-docker.pkg.dev/<project>/ramen-groups-<group>/worker@sha256:…` (an Artifact Registry repository per group) |
| AWS | `<account>.dkr.ecr.<region>.amazonaws.com/ramen/groups/<group>/worker@sha256:…` |
| Azure | `<acr>/ramen/groups/<group>/worker@sha256:…` |

The image is pushed as `…/worker:b-<group>-<hash>` and recorded, and run, by the digest the build reported. A group
name that some registry would refuse as a path component (a doubled, leading or trailing `-`) is replaced by
`g-<16 hex of its sha1>`. `RAMEN_BUILD_REGISTRY` is the prefix beside which the group repositories sit (the
platform prefix `…/ramen`); the chart derives it and `builder.registry` overrides it.

### How it is built

The console starts a Kubernetes Job, `ramen-build-<hash>`, in its own namespace (`ramen-system`). The Job runs as
the group's service account `ramen-build-<group>` and builds with **kaniko**
(`gcr.io/kaniko-project/executor:v1.24.0`), unprivileged; kaniko writes the pushed digest to the container's
termination message, where the console reads it. Its build context is a ConfigMap holding the Dockerfile and
`requirements.txt`. The console waits up to `RAMEN_BUILD_TIMEOUT_SECS` (default 900), records the image on the
group, uses it for this deploy, and deletes the Job and its ConfigMap (the Job also carries a 600 s TTL in case the
console could not). Two deploys that need the same tag at once wait on the same Job.

**The console holds no push right.** On a group's first build it creates the group's repository and an identity
that may push to it and nowhere else, and the service account the Job runs as:

| Cloud | Build identity `ramen-build-<group>` | What it may do |
|---|---|---|
| GCP | the Kubernetes service account `ramen-system/ramen-build-<group>` itself, as a Workload Identity Federation principal (no Google service account) | `roles/artifactregistry.writer` on `ramen-groups-<group>`; read on the platform repository `ramen` through Terraform's binding for the `ramen-system` namespace |
| AWS | IAM role `/ramen/build/ramen-build-<group>`, assumed by `ramen-system/ramen-build-<group>` (IRSA), under the permissions boundary `ramen-build-boundary` | push and pull on `ramen/groups/<group>/worker`, pull on the worker base repository, the registry login |
| Azure | managed identity `ramen-build-<group>` (tag `ramen-build-group`), federated to `ramen-system/ramen-build-<group>` | `Container Registry Repository Writer` on the registry under an ABAC condition naming `ramen/groups/<group>/worker`; `… Reader` likewise on the worker base repository |

Turn the builder on with `builder.enabled=true` (each cloud's Terraform prints it: `builder_set`, or in Azure's
`helm_set`). The chart then sets `RAMEN_BUILDER=1`, `RAMEN_BUILD_REGISTRY` and `RAMEN_BUILD_IDENTITIES` on the
console and gives it a Role in its namespace for the build's ConfigMap, Job, pod log and the `ramen-build-*` service
accounts. `builder.enabled` is `null` by default, meaning "on when a pre-1.0.0c identity value (`builder.gsa`,
`builder.roleArn`, `builder.clientId`) is given"; `true` or `false` forces it. **Without it no builder is
configured and nothing is ever built**: workers install requirements at load as before. Each deploy guide passes it
in its Helm step ([GCP](deploy-gcp.md#4-the-console), [AWS](deploy-aws.md#3-the-console),
[Azure](deploy-azure.md#3-the-console)).

### Turning it on and off

- **Group page → Worker image → Build on deploy**, group admins and up. API:
  `PUT /api/v1/groups/{g} {"build_image": true}`.
- **The default**, for a group nobody set it on: on when the console runs with `RAMEN_CLOUD` `gcp`, `aws` or
  `azure` **and** a builder is configured; off otherwise.
- Turning it on in a deployment without a builder is refused with `422`, and the message names what is missing
  (`RAMEN_BUILDER=1` and `RAMEN_BUILD_REGISTRY`). The button is disabled there.

When a group stops qualifying (turned off, the requirements file removed or emptied), its next deploy goes back to
the release image.

### What `requirements.txt` may hold

Package names from the index, with versions, extras and markers, and the options `--require-hashes`,
`--only-binary`, `--prefer-binary`, `--pre` and a requirement's `--hash`. Anything that makes pip fetch from
elsewhere fails the build **before** it starts, naming the line: option lines such as `-e`, `-i` / `--index-url`,
`--extra-index-url`, `-f` / `--find-links`, `--trusted-host`, `-r`, `-c`, and URL, VCS (`git+…`) and path
requirements (`pkg @ https://…`, `./local`, `x.whl`). The deploy log says
`image: mcp/requirements.txt line <n> is refused: <line>` and the deploy stops before the canary.

A group admin can allow them for the group: `PUT /api/v1/groups/{g} {"build": {"allow_urls": true}}`, audited as
`group.update` with the tag `build.allow_urls:True`. A build still runs whatever an sdist's `setup.py` does; the
identity it runs as can only write the group's own repository.

### A failed build

A build that fails or runs out of time **fails the deploy before the canary**: nothing is rolled and the stable
workers keep serving what they served. The deploy log carries the last 40 lines of the build's output, each
prefixed `image: |`, and the job's error is `BuildError: image build <tag> failed: …`. The attempt is listed on the
Worker image card with a *build failed* badge. The next deploy tries again.

### What the worker does with it

The runtime compares `RAMEN_DEPS_BAKED` with the sha256 of the `mcp/requirements.txt` it finds in the bucket. When
they match it skips `pip install` and logs `deps baked`. Any other value, or none, installs as before — so a
built image that meets a different requirements file (an older image pinned by hand, say) still ends up with the
right packages, only slower.

### Settings

Read by the console.

| Variable | Default | Meaning |
|---|---|---|
| `RAMEN_BUILDER` | unset | `1` says a builder exists. Set by the chart. |
| `RAMEN_BUILD_REGISTRY` | unset | The platform prefix the group repositories sit beside (above). Set by the chart. Both this and `RAMEN_BUILDER=1` are needed. |
| `RAMEN_BUILD_IDENTITIES` | `cloud` | `cloud` creates each group's repository and cloud identity; `none` only its service account (kind: a registry that takes anonymous pushes). `builder.identities` in the chart. |
| `RAMEN_AWS_BUILD_BOUNDARY` | `arn:aws:iam::<account>:policy/ramen/ramen-build-boundary` | The permissions boundary of the AWS build roles (`-` = none). |
| `RAMEN_AZURE_ACR_ID` | the registry of the build registry's host in the install's resource group | The ACR resource id the Azure build role assignments are made on. |
| `RAMEN_BUILDER_IMAGE` | `gcr.io/kaniko-project/executor:v1.24.0` | The image the build Job runs (`builder.image` in the chart). |
| `RAMEN_BUILD_TIMEOUT_SECS` | 900 | How long the console waits for a build. |
| `RAMEN_BUILD_CPU`, `RAMEN_BUILD_MEMORY`, `RAMEN_BUILD_MEMORY_LIMIT` | `250m`, `768Mi`, `2Gi` | The build container's CPU, its memory request (what a node must have free for the Job to be placed) and its memory limit. Raise them for a large requirements file. |
| `RAMEN_BUILD_ARGS` | none | Extra arguments for the kaniko executor. |
| `RAMEN_BUILD_INIT_IMAGE` | the build's base image | The image of the init container that copies the build context into place. |

`RAMEN_AWS_BUILD_BOUNDARY`, `RAMEN_AZURE_ACR_ID`, `RAMEN_BUILD_TIMEOUT_SECS` and the rows after it have no chart value
of their own; set them with `--set console.env.<NAME>=…`.

## A pinned image

For what a requirements file cannot express, such as system packages, build a worker image yourself and record it
on the group page's **Worker image** card (super admins):

```sh
make push-worker PROJECT=<project> REGION=<region>    # tag it yourself if you keep several
```

The image is not tied to one group; each group pins the one it runs. The pin reaches the zones on the next deploy,
**Recall** puts the group back on an older recorded image, and **Use the release image** removes the pin. A pin
wins over a built image, and a pinned group builds nothing.

## Per-group images (1.0.0c, D85)

Before 1.0.0c every build ran as one `ramen-builder` identity that could write the whole registry repository the
console and the worker base image came from, so a group's build could replace the platform's or another group's
image (security finding F2). Now:

- **One repository and one identity per group**, as above. The console creates both on the group's first build and
  deletes them, with the service account, when the group is deleted (`DELETE /api/v1/groups/{g}` answers
  `{"ok": true}`; a failure to remove them never blocks the delete).
- **Platform images** (console, worker base, SkillSpector) stay where they were — `…/ramen/console`, `…/ramen/worker`,
  `…/ramen/skillspector` — and no build identity can write them (AWS: the build boundary allows push only under
  `ramen/groups/`; GCP: the only writer of a group repository is that group's principal and none exists on `ramen`;
  Azure: every build role assignment names one repository).
- **Digests.** Deploys run a built image as `<repo>@sha256:…`. For the platform images set `image.digest` (the
  console) and `image.workerDigest` (the worker base) in the chart; the reference becomes `<repo>@<digest>` and the
  tag is informational. Read a digest after a push with
  `docker buildx imagetools inspect <image> --format '{{json .Manifest.Digest}}'`.
- **The console's own rights** (Terraform): GCP — create and list repositories and tag a new one `ramen-repo=group`,
  and get / update / delete / IAM only on repositories carrying that tag (Artifact Registry ignores IAM Conditions on
  the repository name but evaluates resource tags; the platform repository is tagged `ramen-repo=platform`, see
  [IAM and network on GCP](iam-network-gcp.md)); AWS — create, tag, describe and delete ECR repositories
  under `ramen/groups/`, and create roles only with the worker or build boundary; Azure — a data-plane role to read
  and delete repositories under `ramen/groups/` (an ABAC condition, no write action). None can push.
- The group name `build` is reserved: `ramen-build-<group>` names build identities and `ramen-<group>-<zone>` zone
  identities.

### Upgrading an install from before 1.0.0c

Existing installs keep working: zones keep running the images they run, including built ones in the old shared
repository, until the next deploy of a building group, which rebuilds into the group's own repository (a record
outside it no longer counts as built).

1. `terraform apply` the cloud's module. It removes `ramen-builder` and its grants and adds the console's rights
   above (AWS: the `ramen-build-boundary` policy; GCP: two custom roles and the namespace reader on `ramen`; Azure:
   the registry's repository-permission mode, the kubelet's `Container Registry Repository Reader`, the console's
   cleaner role). **Azure:** check the plan updates the registry in place (`role_assignment_mode`), never replaces
   it; once applied, `AcrPull` / `AcrPush` assignments made by hand no longer count — use the repository roles.
2. `helm upgrade` with `--set builder.enabled=true` (drop `builder.gsa` / `builder.roleArn` / `builder.clientId`;
   they still turn builds on, but the identity they name is gone).
3. Deploy each group that builds. The log shows `image: building …` and then `image: built <repo>@sha256:…`.
4. Optional: delete the old `b-*` tags from `…/ramen/worker` once no zone runs them, and pin the platform images by
   digest (`image.digest`, `image.workerDigest`).

## Limits

- The build uses kaniko; there is no other builder in 1.0.
- **The local compose stack does not build images.** Its console runs without `RAMEN_BUILDER` and
  `RAMEN_BUILD_REGISTRY`, so Build on deploy cannot be turned on there and requirements are installed at load.
- A build adds its time to the first deploy after a requirements change.

## What is verified

By tests:

- `tests/conformance/test_deps_baked.py`: a real runtime and node with `RAMEN_DEPS_BAKED` — a matching digest
  skips pip and logs `deps baked`, any other value or a changed file installs.
- `tests/conformance/test_console_sleep_otel.py`: Build on deploy cannot be turned on without a builder.
- `tests/kind/test_v1_sleep_build_otel.py` holds the cluster proofs (a deploy builds the group's image into the
  cluster's registry and records it, the Job and its context are cleaned up, the worker pods run the image and log
  `deps baked`, a second deploy builds nothing).

On GKE, EKS and AKS (1.0.0, 2026-11-13 to 2026-11-14): a deploy built the group's image in each cloud's own
registry with that cloud's then shared `ramen-builder` identity, on GKE Autopilot too, and the workers served from
the built image. The per-group repositories and identities of 1.0.0c are covered by console tests
(`console/tests/test_d85_builds.py`, against fakes of each cloud) and by the kind proof, and ran on GKE in the 1.0.0c
final pass (a deploy built into `ramen-groups-<group>`, whose only writer is that group's principal; deleting the group
removed the repository and its service account); the EKS and AKS runs are pending. The build Job's default requests (250m CPU, 768Mi memory, with a 2Gi memory limit) are what the small test
nodes on EKS and AKS could place; the 2Gi request it had first was unschedulable there.

The kind numbers and the rest of the cloud run are on the
[Transport page](transport.md#what-is-verified-and-what-is-not).
