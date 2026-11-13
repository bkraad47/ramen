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

## Images built on deploy

### When a build happens

A deploy builds first, before any canary, when all of this is true:

- **Build on deploy** is on for the group (below),
- the checkout has an `mcp/requirements.txt` with at least one requirement — **a file of comments and blank lines
  builds nothing**,
- the group has no pinned image (the deploy log then says the pin wins and nothing is built),
- no image with the same tag was already built successfully.

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

In the deployment's own registry, as a tag of **one** repository, `worker`. There is no repository per group to
create:

| Cloud | Image |
|---|---|
| GCP | `<region>-docker.pkg.dev/<project>/ramen/worker:b-<group>-<hash>` |
| AWS | `<account>.dkr.ecr.<region>.amazonaws.com/ramen/worker:b-<group>-<hash>` |
| Azure | `<acr>/ramen/worker:b-<group>-<hash>` |

The prefix before `/worker` is `RAMEN_BUILD_REGISTRY`; the chart derives the values above and `builder.registry`
overrides them.

### How it is built

The console starts a Kubernetes Job, `ramen-build-<hash>`, in its own namespace (`ramen-system`). The Job runs as
the service account `ramen-builder` and builds with **kaniko** (`gcr.io/kaniko-project/executor:v1.24.0`),
unprivileged. Its build context is a ConfigMap holding the Dockerfile and `requirements.txt`. The console waits up
to `RAMEN_BUILD_TIMEOUT_SECS` (default 900), records the image on the group, uses it for this deploy, and deletes
the Job and its ConfigMap (the Job also carries a 600 s TTL in case the console could not). Two deploys that need
the same tag at once wait on the same Job.

**The console holds no registry credential.** Only the `ramen-builder` identity can push, and Terraform creates it:

| Cloud | Builder identity | What it may do | Terraform output → chart value |
|---|---|---|---|
| GCP | service account `ramen-builder`, bound by workload identity to `ramen-system/ramen-builder` | `roles/artifactregistry.writer` on the `ramen` repository | `builder_gsa` → `builder.gsa` |
| AWS | IAM role `ramen-builder`, assumed by `ramen-system/ramen-builder` (IRSA) | pull and push on the ECR repository `ramen/worker`, plus the registry login | `builder_role_arn` → `builder.roleArn` |
| Azure | user-assigned identity `ramen-builder`, federated to `ramen-system/ramen-builder` | `AcrPush` on the registry | `builder_client_id` → `builder.clientId` |

Given that value, the chart creates the `ramen-builder` service account with the cloud's annotation, a Role that
lets the console manage the build's ConfigMap, Job and pod log in its namespace, and sets `RAMEN_BUILDER=1` and
`RAMEN_BUILD_REGISTRY` on the console. `builder.enabled` is `null` by default, meaning "on when the identity is
given"; `true` or `false` forces it. **Without the identity value no builder is configured and nothing is ever
built**: workers install requirements at load as before. Each deploy guide passes it in its Helm step
([GCP](deploy-gcp.md#4-the-console), [AWS](deploy-aws.md#3-the-console), [Azure](deploy-azure.md#3-the-console)).

### Turning it on and off

- **Group page → Worker image → Build on deploy**, group admins and up. API:
  `PUT /api/v1/groups/{g} {"build_image": true}`.
- **The default**, for a group nobody set it on: on when the console runs with `RAMEN_CLOUD` `gcp`, `aws` or
  `azure` **and** a builder is configured; off otherwise.
- Turning it on in a deployment without a builder is refused with `422`, and the message names the missing
  `ramen-builder`. The button is disabled there.

When a group stops qualifying (turned off, the requirements file removed or emptied), its next deploy goes back to
the release image.

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
| `RAMEN_BUILD_REGISTRY` | unset | The repository prefix images are pushed under, as `<prefix>/worker:<tag>`. Set by the chart. Both this and `RAMEN_BUILDER=1` are needed. |
| `RAMEN_BUILDER_IMAGE` | `gcr.io/kaniko-project/executor:v1.24.0` | The image the build Job runs (`builder.image` in the chart). |
| `RAMEN_BUILD_TIMEOUT_SECS` | 900 | How long the console waits for a build. |
| `RAMEN_BUILD_CPU`, `RAMEN_BUILD_MEMORY`, `RAMEN_BUILD_MEMORY_LIMIT` | `250m`, `768Mi`, `2Gi` | The build container's CPU, its memory request (what a node must have free for the Job to be placed) and its memory limit. Raise them for a large requirements file. |
| `RAMEN_BUILD_ARGS` | none | Extra arguments for the kaniko executor. |
| `RAMEN_BUILD_INIT_IMAGE` | the build's base image | The image of the init container that copies the build context into place. |

The last four have no chart value of their own; set them with `--set console.env.<NAME>=…`.

## A pinned image

For what a requirements file cannot express, such as system packages, build a worker image yourself and record it
on the group page's **Worker image** card (super admins):

```sh
make push-worker PROJECT=<project> REGION=<region>    # tag it yourself if you keep several
```

The image is not tied to one group; each group pins the one it runs. The pin reaches the zones on the next deploy,
**Recall** puts the group back on an older recorded image, and **Use the release image** removes the pin. A pin
wins over a built image, and a pinned group builds nothing.

## Limits

- The build uses kaniko; there is no other builder in 1.0.
- **The local compose stack does not build images.** Its console runs without `RAMEN_BUILDER` and
  `RAMEN_BUILD_REGISTRY`, so Build on deploy cannot be turned on there and requirements are installed at load.
- A build adds its time to the first deploy after a requirements change.

## What is verified

Unit and process-level tests only so far:

- `tests/conformance/test_deps_baked.py`: a real runtime and node with `RAMEN_DEPS_BAKED` — a matching digest
  skips pip and logs `deps baked`, any other value or a changed file installs.
- `tests/conformance/test_console_sleep_otel.py`: Build on deploy cannot be turned on without a builder.
- `tests/kind/test_v1_sleep_build_otel.py` holds the cluster proofs (a deploy builds the group's image into the
  cluster's registry and records it, the Job and its context are cleaned up, the worker pods run the image and log
  `deps baked`, a second deploy builds nothing).

The kind and cloud runs are recorded on the
[Transport page](transport.md#what-is-verified-and-what-is-not) once done. Pushing with each cloud's builder
identity has not been run.
<!-- CLOUD-1.0.0-S24 -->
