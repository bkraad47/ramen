"""§24.2 worker images built on deploy: the tag, the Dockerfile and the `images` record of a group's own image —
the base worker image plus the checkout's `mcp/requirements.txt`. The console starts the build through the adapter
(`Cloud.build_image`) and holds no registry credential itself.

`images` record of a build: `{"group", "seq", "tag": "b-<group>-<hash16>", "ref": <image the adapter pushed, by
digest when the adapter read one: `<repo>@sha256:…`>, "digest", "source": "build", "status": "ok" | "failed", "base",
"deps": <sha256 of requirements.txt>, "note", "created", "created_by": "deploy"}`. The group doc carries
`built_image: {"id", "ref", "tag"}` while a built image is the one its zones run (a manual pin in `image` wins, §13.3).

§38.2 (D85, F2): each group builds into its own repository (`Cloud.build_repository`) as its own identity; a record
outside that repository (the pre-D85 shared `ramen/worker`) no longer counts as built, so the next deploy rebuilds
into the group's repository. `requirements.txt` may not carry option lines or URL / VCS / path requirements unless the
group admin sets `build.allow_urls` — they would fetch and run code from outside the package index during the build.
"""

import hashlib
import inspect
import os
import re

from .util import now, uid

REQUIREMENTS = "mcp/requirements.txt"
LOG_TAIL = 40
CLOUDS = ("gcp", "aws", "azure")
# one registry path component every registry takes (ECR is the strictest: no leading, trailing or doubled `-`)
COMPONENT = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
# pip options a requirements file may carry without `build.allow_urls`: none of them names a source
SAFE_OPTIONS = frozenset({"--require-hashes", "--only-binary", "--prefer-binary", "--pre"})
SAFE_REQ_OPTIONS = frozenset({"--hash"})
DIRECT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*\s*(\[[^\]]*\])?\s*@")  # `name @ <url>` (PEP 508 direct reference)
VCS = re.compile(r"^(git|hg|svn|bzr)\+", re.I)
ARCHIVE = re.compile(r"\.(whl|zip|tar\.gz|tgz|tar\.bz2|tar)$", re.I)


class BuildError(RuntimeError):
    """The group image could not be built; the deploy stops before any canary."""


async def builder_available(cloud) -> bool:
    fn = getattr(cloud, "builder_available", None)
    if fn is None:
        return False
    r = fn()
    return bool(await r if inspect.isawaitable(r) else r)


def missing_builder() -> str:
    return (
        "build_image needs a builder and this deployment has none: RAMEN_BUILDER=1 and RAMEN_BUILD_REGISTRY on the "
        "console (the chart's builder.enabled; Terraform lets the console create each group's ramen-build-<group> "
        "identity and repository)"
    )


async def enabled(cloud, group_doc: dict) -> bool:
    """`build_image` as set on the group, else the default: on for gcp/aws/azure when the deployment has a builder."""
    if group_doc.get("build_image") is not None:
        return bool(group_doc["build_image"])
    return os.environ.get("RAMEN_CLOUD", "local") in CLOUDS and await builder_available(cloud)


def base_image(cloud) -> str:
    return getattr(cloud, "image", "") or os.environ.get("RAMEN_IMAGE_WORKER", "")


def deps_hash(requirements: bytes) -> str:
    return hashlib.sha256(requirements).hexdigest()


def tag_for(group: str, base: str, requirements: bytes) -> str:
    return f"b-{group}-{hashlib.sha256(base.encode() + b'\n' + requirements).hexdigest()[:16]}"


def dockerfile(base: str, requirements: bytes) -> str:
    return (
        f"FROM {base}\n"
        "COPY requirements.txt /opt/ramen/requirements.txt\n"
        "RUN /opt/venv/bin/pip install --no-cache-dir -r /opt/ramen/requirements.txt\n"
        f"ENV RAMEN_DEPS_BAKED={deps_hash(requirements)}\n"
    )


def packages(requirements: bytes | str) -> list[str]:
    """The lines pip would act on: a file of comments and blank lines has nothing to bake (the runtime agrees)."""
    text = requirements.decode("utf-8", "replace") if isinstance(requirements, bytes) else requirements
    return [ln.strip() for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]


def repo_component(group: str) -> str:
    """The group as one registry path component: itself when every registry accepts it, else `g-<sha1[:16]>`."""
    return group if COMPONENT.match(group) else "g-" + hashlib.sha1(group.encode()).hexdigest()[:16]


def repository(cloud, group: str) -> str | None:
    """§38.2: the group's own image repository (no tag) when the adapter builds per group, else None."""
    fn = getattr(cloud, "build_repository", None)
    return fn(group) if fn is not None else None


def repo_of(ref: str) -> str:
    """`host[:port]/path` of an image reference, without its tag and digest."""
    name = ref.split("@", 1)[0]
    return name.rsplit(":", 1)[0] if ":" in name.rsplit("/", 1)[-1] else name


def _logical_lines(text: str):
    """(first line number, line) as pip reads them: `\\` continuations joined, comments dropped."""
    buf, start = "", 0
    for n, raw in enumerate(text.splitlines(), 1):
        if not buf:
            start = n
        if raw.endswith("\\"):
            buf += raw[:-1]
            continue
        line = re.sub(r"(^|\s)#.*$", "", buf + raw).strip()
        buf = ""
        if line:
            yield start, line
    if line := re.sub(r"(^|\s)#.*$", "", buf).strip():
        yield start, line


def _option(token: str) -> str:
    return token.split("=", 1)[0]


def refused(requirements: bytes | str) -> tuple[int, str] | None:
    """§38.2: the first line pip would turn into code from outside the package index — an option line other than
    `SAFE_OPTIONS` (`-e`, `-i`/`--index-url`, `--extra-index-url`, `-f`/`--find-links`, `--trusted-host`, `-r`, `-c`,
    ...), a URL, VCS or path requirement, or a per-requirement option other than `--hash` — as (line number, line).
    None when every line is a plain index requirement."""
    text = requirements.decode("utf-8", "replace") if isinstance(requirements, bytes) else requirements
    for n, line in _logical_lines(text):
        if line.startswith("-"):
            if _option(line.split()[0]) not in SAFE_OPTIONS:
                return n, line
            continue
        spec, *opts = re.split(r"\s+(?=--)", line)
        spec = spec.strip()
        if any(_option(o.split()[0]) not in SAFE_REQ_OPTIONS for o in opts):
            return n, line
        if (
            "://" in spec
            or spec.lower().startswith("file:")
            or DIRECT.match(spec)
            or VCS.match(spec)
            or spec.startswith((".", "/", "~", "\\"))
            or ARCHIVE.search(spec.split(";", 1)[0].strip())
        ):
            return n, line
    return None


def refusal(n: int, line: str) -> str:
    return (
        f"{REQUIREMENTS} line {n}: `{line[:200]}` is refused: option lines and URL, VCS or path requirements fetch "
        "and run code from outside the package index during the build (§38.2). A group admin can allow them with "
        "`build.allow_urls: true` (audited)"
    )


def tail(text: str | None, n: int = LOG_TAIL) -> list[str]:
    return [ln for ln in (text or "").splitlines() if ln.strip()][-n:]


async def _record(store, group, tag, ref, status, base, deps, note, digest=None) -> dict:
    doc = {
        "group": group,
        "seq": len(await store.list("images", {"group": group})) + 1,
        "tag": tag,
        "digest": digest,
        "ref": ref,
        "source": "build",
        "status": status,
        "base": base,
        "deps": deps,
        "note": note,
        "created": now(),
        "created_by": "deploy",
    }
    return await store.put("images", uid(), doc)


async def _use(store, group: str, built: dict | None) -> None:
    g = await store.get("groups", group)
    if g is None or g.get("built_image") == built:
        return
    if built is None:
        g.pop("built_image", None)
    else:
        g["built_image"] = built
    await store.put("groups", group, g)


async def ensure(svc, group: str, log) -> str | None:
    """Before the canary: the image this deploy runs when the group builds its own, building it if no record says it
    exists. None = nothing built applies (off, no requirements, or a manual pin wins). Raises BuildError."""
    g = await svc.get_group(group)
    if not await enabled(svc.cloud, g):
        await _use(svc.store, group, None)
        return None
    raw = await svc.cloud.read_file(group, REQUIREMENTS)
    if raw is None or not packages(raw):
        await _use(svc.store, group, None)
        return None
    if (g.get("image") or {}).get("ref"):
        log(f"{now()} image: {g['image']['ref']} is pinned for this group, nothing is built")
        return None
    base = base_image(svc.cloud)
    if not base:
        raise BuildError("no base worker image is configured (RAMEN_IMAGE_WORKER), so no group image can be built")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise BuildError(f"{REQUIREMENTS} is not UTF-8 text") from None
    if not (g.get("build") or {}).get("allow_urls") and (bad := refused(text)):
        log(f"{now()} image: {REQUIREMENTS} line {bad[0]} is refused: {bad[1][:200]}")
        raise BuildError(refusal(*bad))
    tag, deps = tag_for(group, base, raw), deps_hash(raw)
    repo = repository(svc.cloud, group)
    for d in await svc.store.list("images", {"group": group}):
        if repo and repo_of(d.get("ref") or "") != repo:
            continue  # §38.2: built before the group had its own repository — rebuilt there
        if d.get("tag") == tag and d.get("source") == "build" and d.get("status") == "ok":
            log(f"{now()} image: {d['ref']} already built for these requirements")
            await _use(svc.store, group, {"id": d["id"], "ref": d["ref"], "tag": tag})
            return d["ref"]
    log(f"{now()} image: building {tag} from {base} + {REQUIREMENTS}")
    out, error = {}, None
    try:
        out = await svc.cloud.build_image(group, tag, dockerfile(base, raw), {"requirements.txt": text}) or {}
    except Exception as e:  # noqa: BLE001 - whatever the adapter raised is the reason the deploy stops
        error = f"{type(e).__name__}: {e}"
        out = {"log": getattr(e, "log", "") or ""}
    if error is None and (out.get("ok") is False or not out.get("image")):
        error = str(out.get("error") or "the build produced no image")
    if error is not None:
        for ln in tail(out.get("log")):
            log(f"{now()} image: | {ln}")
        await _record(svc.store, group, tag, "", "failed", base, deps, error[:300])
        raise BuildError(f"image build {tag} failed: {error}")
    note = f"built on deploy from {REQUIREMENTS}"
    doc = await _record(svc.store, group, tag, out["image"], "ok", base, deps, note, out.get("digest"))
    log(f"{now()} image: built {doc['ref']}")
    await _use(svc.store, group, {"id": doc["id"], "ref": doc["ref"], "tag": tag})
    return doc["ref"]
