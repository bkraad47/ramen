"""D86 (CONTRACTS §39): Ramen's per-install CA and the leaf certificates it issues.

`python -m ramen_console.tls init <dir> [--leaf name=san,san …] [--zone name=<namespace>]` writes
`<dir>/ca/ca.{crt,key}` (kept when present) and one `<dir>/<name>/{tls.crt,tls.key,ca.crt}` per leaf (re-issued only
when due) — local compose and the kind harness. `python -m ramen_console.tls <dir> [hostname]` still writes a plain
self-signed pair.
"""

import argparse
import asyncio
import ipaddress
import logging
import os
import ssl
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

WORKER_NAME = "worker.ramen.internal"  # §39.2: the one name every in-install client verifies on a worker
CA_DOC = ("config", "install_ca")
CA_YEARS = 10
log = logging.getLogger("ramen.tls")
ZONE_SERVICES = ("worker", "worker-direct", "worker-canary")


class CaError(RuntimeError):
    pass


def require_tls(env=None) -> bool:
    """`RAMEN_REQUIRE_TLS=1`: a cloud install (the chart sets it) — plaintext is refused (§39.4)."""
    return (env if env is not None else os.environ).get("RAMEN_REQUIRE_TLS", "").strip().lower() in ("1", "true")


def _scheme(url: str) -> str:
    return urlsplit(url.strip()).scheme.lower()


def startup_problems(env=None) -> list[str]:
    """§39.4: what a console under `RAMEN_REQUIRE_TLS=1` refuses to start with (never quoting a URL: it may hold a
    password)."""
    env = env if env is not None else os.environ
    if not require_tls(env):
        return []
    out = []
    if env.get("RAMEN_WORKER_TLS", "").strip() == "0":
        out.append("RAMEN_WORKER_TLS=0: the console dials workers over TLS on a cloud install")
    if (url := env.get("RAMEN_REDIS_URL", "").strip()) and _scheme(url) != "rediss":
        out.append("RAMEN_REDIS_URL must be a rediss:// URL")
    for k in ("RAMEN_CONSOLE_INTERNAL_URL", "RAMEN_EVALS_API_URL"):
        if (url := env.get(k, "").strip()) and _scheme(url) != "https":
            out.append(f"{k} must be an https:// URL")
    return out


def refuse_plaintext() -> None:
    if problems := startup_problems():
        raise RuntimeError("D86: plaintext is refused on a cloud install (RAMEN_REQUIRE_TLS=1): " + "; ".join(problems))


def leaf_days() -> int:
    return max(1, int(os.environ.get("RAMEN_TLS_LEAF_DAYS", "30") or 30))


def _pem_key(key) -> str:
    return key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()


def _san(name: str):
    try:
        return x509.IPAddress(ipaddress.ip_address(name))
    except ValueError:
        return x509.DNSName(name)


@dataclass
class Leaf:
    cert: str
    key: str
    ca: str  # the bundle a peer trusts


@dataclass
class CA:
    """`cert`/`key` issue; `next` is a staged CA (trusted, not yet issuing: §39.1 phase 1); `previous` a retiring
    one (trusted, never issuing again)."""

    cert: str
    key: str
    previous: str = ""
    next: str = ""
    next_key: str = ""

    @property
    def bundle(self) -> str:
        return self.cert + self.next + self.previous

    def which(self, leaf_pem: str) -> str:
        """`current` | `next` | `previous` | `unknown`: the CA of the bundle that issued `leaf_pem`."""
        try:
            leaf = x509.load_pem_x509_certificate((leaf_pem or "").encode())
        except ValueError:
            return "unknown"
        for name in ("cert", "next", "previous"):
            if pem := getattr(self, name):
                try:
                    leaf.verify_directly_issued_by(x509.load_pem_x509_certificate(pem.encode()))
                except Exception:  # noqa: BLE001, S112 - not this CA
                    continue
                return "current" if name == "cert" else name
        return "unknown"

    def issue(self, names: list[str], days: int | None = None) -> Leaf:
        if not self.key:
            raise CaError("this console holds the CA certificate only (no ca.key in RAMEN_TLS_CA_DIR): it cannot issue")
        ca_cert = x509.load_pem_x509_certificate(self.cert.encode())
        ca_key = serialization.load_pem_private_key(self.key.encode(), None)
        key = ec.generate_private_key(ec.SECP256R1())
        now = datetime.now(UTC)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=5))
            .not_valid_after(now + timedelta(days=days or leaf_days()))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(
                x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True
            )  # digitalSignature, keyEncipherment
            .add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False,
            )
            .add_extension(x509.SubjectAlternativeName([_san(n) for n in dict.fromkeys(names)]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_cert.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        return Leaf(cert.public_bytes(serialization.Encoding.PEM).decode(), _pem_key(key), self.bundle)


def new_ca(install_id: str = "") -> CA:
    key = ec.generate_private_key(ec.SECP256R1())
    cn = f"Ramen install CA {install_id or os.urandom(4).hex()}"
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365 * CA_YEARS))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
        .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        .sign(key, hashes.SHA256())
    )
    return CA(cert.public_bytes(serialization.Encoding.PEM).decode(), _pem_key(key))


def needs_renewal(cert_pem: str, ca: CA, now: datetime | None = None) -> bool:
    """True when the leaf is missing, unreadable, not issued by `ca`'s current cert, or has a third of its life left."""
    try:
        c = x509.load_pem_x509_certificate((cert_pem or "").encode())
        c.verify_directly_issued_by(x509.load_pem_x509_certificate(ca.cert.encode()))
    except Exception:  # noqa: BLE001 - anything we cannot read or verify is due
        return True
    now = now or datetime.now(UTC)
    life = c.not_valid_after_utc - c.not_valid_before_utc
    return c.not_valid_after_utc - now < life / 3


def service_names(svc: str, ns: str) -> list[str]:
    return [svc, f"{svc}.{ns}", f"{svc}.{ns}.svc", f"{svc}.{ns}.svc.cluster.local", "localhost", "127.0.0.1"]


def zone_names(ns: str) -> list[str]:
    out = [WORKER_NAME]
    for svc in ZONE_SERVICES:
        out += service_names(svc, ns)[:4]
    return [*out, "localhost", "127.0.0.1"]


def console_names(ns: str) -> list[str]:
    names = service_names("console", ns)
    host = urlsplit(os.environ.get("RAMEN_CONSOLE_INTERNAL_URL", "")).hostname
    return [*names, host] if host and host not in names else names


# ---- the install CA ------------------------------------------------------------------------------------------------
_CURRENT: list[CA | None] = [None]


def current() -> CA | None:
    return _CURRENT[0]


def set_current(ca: CA | None) -> None:
    _CURRENT[0] = ca


def _from_dir(path: str) -> CA:
    """`ca.crt` is required; `ca.key` only to issue (local compose hands the console a pre-issued leaf and keeps the
    key out of its reach)."""
    d = Path(path)
    try:
        cert = (d / "ca.crt").read_text()
    except OSError as e:
        raise CaError(f"RAMEN_TLS_CA_DIR={path}: {e.strerror}") from None
    try:
        key = (d / "ca.key").read_text()
    except OSError:
        key = ""
    return CA(cert, key)


def _from_doc(doc: dict | None) -> CA | None:
    if doc and doc.get("cert") and doc.get("ca_key"):
        staged = doc.get("next") and doc.get("next_key")
        return CA(
            doc["cert"],
            doc["ca_key"],
            doc.get("previous") or "",
            doc["next"] if staged else "",
            doc["next_key"] if staged else "",
        )
    return None


async def ensure_ca(store, cache: bool = True) -> CA:
    """The install CA: `RAMEN_TLS_CA_DIR` when set, else `config/install_ca`, created when absent (or when a restored
    doc came back without its key — backups never carry it). Written only when absent: two consoles starting
    together agree on the one that landed first."""
    if d := os.environ.get("RAMEN_TLS_CA_DIR", "").strip():
        ca = _from_dir(d)
    else:
        async with store.transaction() as tx:
            ca = _from_doc(await tx.get(*CA_DOC))
            if ca is None:
                ca = new_ca()
                await tx.put(*CA_DOC, {"cert": ca.cert, "ca_key": ca.key, "previous": "", "created": _now()})
        ca = _from_doc(await store.get(*CA_DOC)) or ca
    if cache:
        set_current(ca)
    return ca


SETTLE_SECS = 180  # kubelet refreshes a mounted Secret/ConfigMap within ~1–2 min; the node re-reads its files each 60 s
IN_PROGRESS = "a CA rotation is in progress: the leaves switch to the new CA {when}; retry after that"


def rotate_settle() -> float:
    """§39.1: seconds between publishing the two-CA bundle and switching any leaf (`RAMEN_TLS_ROTATE_SETTLE_SECS`)."""
    try:
        return max(0.0, float(os.environ.get("RAMEN_TLS_ROTATE_SETTLE_SECS", SETTLE_SECS)))
    except ValueError:
        return SETTLE_SECS


def _doc_of(ca: CA, **extra) -> dict:
    return {
        "cert": ca.cert,
        "ca_key": ca.key,
        "previous": ca.previous,
        "next": ca.next,
        "next_key": ca.next_key,
        **extra,
    }


def _no_dir() -> None:
    if os.environ.get("RAMEN_TLS_CA_DIR", "").strip():
        raise CaError("the CA comes from RAMEN_TLS_CA_DIR: rotate it there (python -m ramen_console.tls init)")


def _switch_at(doc: dict) -> str:
    try:
        t = datetime.fromisoformat(doc.get("staged") or "") + timedelta(seconds=rotate_settle())
    except ValueError:
        return "shortly"
    return "at " + t.isoformat(timespec="seconds")


async def rotate(store) -> CA:
    """§39.1 phase 1: a new CA is staged as `next` — in the bundle, not issuing. Refused while a rotation is staged or
    a previous CA is still trusted (a bundle holds two CAs at most: retire first)."""
    _no_dir()
    async with store.transaction() as tx:
        doc = await tx.get(*CA_DOC) or {}
        old = _from_doc(doc)
        if old is None:
            old = new_ca()
            doc = _doc_of(old, created=_now())
        if old.next:
            raise CaError(IN_PROGRESS.format(when=_switch_at(doc)))
        if old.previous:
            raise CaError("the previous CA is still trusted: retire it first (POST /api/v1/config/tls/rotate?retire=1)")
        ca = CA(old.cert, old.key, "", *_fresh())
        await tx.put(*CA_DOC, _doc_of(ca, created=doc.get("created") or _now(), staged=_now()))
    set_current(ca)
    return ca


async def unstage(store, staged: str) -> None:
    """Drop the staged CA `staged` again (its bundle did not reach every peer); a CA it never issued with stays harmless
    in the bundles that did get it, until the next rotation rewrites them."""
    async with store.transaction() as tx:
        doc = await tx.get(*CA_DOC) or {}
        if doc.get("next") == staged:
            await tx.put(*CA_DOC, {**doc, "next": "", "next_key": "", "staged": ""})
    set_current(_from_doc(await store.get(*CA_DOC)))


async def promote(store) -> CA | None:
    """§39.1 phase 2: the staged CA issues from now on; the old one stays trusted as `previous` (its key is dropped).
    None when nothing is staged (another replica got there first)."""
    async with store.transaction() as tx:
        old = _from_doc(doc := await tx.get(*CA_DOC))
        if old is None or not old.next:
            return None
        ca = CA(old.next, old.next_key, old.cert)
        await tx.put(*CA_DOC, _doc_of(ca, created=doc.get("created") or _now(), rotated=_now()))
    set_current(ca)
    return ca


async def retire(store) -> CA:
    """Drops `previous` from the bundle; refused while a rotation is staged."""
    _no_dir()
    async with store.transaction() as tx:
        old = _from_doc(doc := await tx.get(*CA_DOC))
        if old is None:
            raise CaError("no install CA yet")
        if old.next:
            raise CaError(IN_PROGRESS.format(when=_switch_at(doc)))
        ca = CA(old.cert, old.key)
        await tx.put(*CA_DOC, _doc_of(ca, created=doc.get("created") or _now(), retired=_now()))
    set_current(ca)
    return ca


# ---- the console's own leaf (registered by the launcher, `serve.register`) ------------------------------------------
_CONSOLE: dict = {"renew": None, "read": None}


def set_console_leaf(renew, read) -> None:
    _CONSOLE.update(renew=renew, read=read)


def console_leaf() -> str | None:
    return _CONSOLE["read"]() if _CONSOLE["read"] else None


def renew_console_leaf() -> bool:
    """Re-issue the console's serving leaf now when it is not from the issuing CA (or due); False without a launcher."""
    return bool(_CONSOLE["renew"]()) if _CONSOLE["renew"] else False


# ---- rotation, in phases (§39.1) ------------------------------------------------------------------------------------
async def start_rotation(store, cloud) -> dict:
    """Phase 1: stage the new CA and publish the two-CA bundle to every peer (the console's trust at once, every
    zone's `ramen-tls`/`ramen-ca`, the chart services' mounts); no leaf changes, nothing restarts."""
    ca = await rotate(store)
    try:
        published = await cloud.publish_tls_bundle()
    except Exception as e:  # noqa: BLE001 - a peer without the bundle must never see a new leaf: unstage
        await unstage(store, ca.next)
        raise CaError(
            f"the new CA's bundle could not be published ({type(e).__name__}: {str(e)[:200]}): nothing was switched; "
            "retry the rotation"
        ) from None
    log.info("tls: CA %s staged; the two-CA bundle is published", fingerprint(ca.next))
    return {
        "fingerprint": fingerprint(ca.next),
        "previous": True,
        "stage": "bundle",
        "switch_after": rotate_settle(),
        "zones": published.get("zones", []),
        "system": [],
    }


async def finish_rotation(store, cloud) -> dict | None:
    """Phases 2–3, once every peer trusts the new CA: it issues; the console's leaf and every zone's leaf are
    re-issued, then the chart services' (restarted). None when nothing is staged."""
    ca = await promote(store)
    if ca is None:
        return None
    console = renew_console_leaf()
    zones = await cloud.renew_zone_tls()
    system = await cloud.ensure_system_tls()
    log.info(
        "tls: CA %s issues now; re-issued: %s",
        fingerprint(ca.cert),
        ", ".join([*(["console"] if console else []), *zones, *system]) or "nothing",
    )
    return {
        "fingerprint": fingerprint(ca.cert),
        "stage": "rotated",
        "console": console,
        "zones": zones,
        "system": system,
    }


async def finish_later(store, cloud, settle: float) -> None:
    """The rotate call's job: wait the settle time, then finish (logged; the renewal loop retries a failure)."""
    await asyncio.sleep(settle)
    try:
        await finish_rotation(store, cloud)
    except Exception as e:  # noqa: BLE001 - `advance` in the renewal loop finishes it
        log.warning("tls: rotation not finished (%s: %s); the renewal loop retries", type(e).__name__, str(e)[:200])


async def advance(store, cloud, now: datetime | None = None) -> dict | None:
    """Finish a rotation staged at least the settle time ago (any replica: e.g. the one that staged it restarted)."""
    doc = await store.get(*CA_DOC) or {}
    if not doc.get("next"):
        return None
    try:
        staged = datetime.fromisoformat(doc.get("staged") or "")
    except ValueError:
        staged = datetime.min.replace(tzinfo=UTC)
    if ((now or datetime.now(UTC)) - staged).total_seconds() < rotate_settle():
        return None
    return await finish_rotation(store, cloud)


async def leaves(cloud) -> list[tuple[str, str]]:
    """(name, PEM) of every leaf this install serves: the console's, the chart services', every zone's."""
    out = [("console", pem)] if (pem := console_leaf()) else []
    return out + await cloud.tls_leaves()


async def retire_previous(store, cloud) -> dict:
    """`?retire=1`: refused (CaError → 409) while a rotation is staged or any leaf is still from another CA; then
    only the bundles shrink — no leaf is re-issued, nothing restarts."""
    ca = await ensure_ca(store)
    if ca.next:
        raise CaError(IN_PROGRESS.format(when=_switch_at(await store.get(*CA_DOC) or {})))
    stale = [name for name, pem in await leaves(cloud) if ca.which(pem) != "current"]
    if stale:
        raise CaError(
            f"{len(stale)} leaf certificate(s) are not from the current CA yet ({', '.join(stale[:10])}): redeploy "
            "those zones (or wait for the renewal) before retiring the previous CA"
        )
    ca = await retire(store)
    published = await cloud.publish_tls_bundle()
    return {"fingerprint": fingerprint(ca.cert), "previous": False, "zones": published.get("zones", []), "system": []}


async def status(store, cloud) -> dict:
    """`GET /api/v1/config/tls`: the CAs (fingerprints), the stage, and which CA issued each leaf."""
    ca = await ensure_ca(store)
    doc = await store.get(*CA_DOC) or {}
    return {
        "fingerprint": fingerprint(ca.cert),
        "next": fingerprint(ca.next) if ca.next else None,
        "previous": fingerprint(ca.previous) if ca.previous else None,
        "stage": "bundle" if ca.next else ("rotated" if ca.previous else "single"),
        "switch_after": _switch_at(doc).removeprefix("at ") if ca.next else None,
        "leaves": [{"name": n, "ca": ca.which(pem)} for n, pem in await leaves(cloud)],
    }


def _fresh() -> tuple[str, str]:
    ca = new_ca()
    return ca.cert, ca.key


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def fingerprint(cert_pem: str) -> str:
    return x509.load_pem_x509_certificate(cert_pem.encode()).fingerprint(hashes.SHA256()).hex()[:32]


async def startup(store, cloud) -> None:
    """The console's start on a TLS install: the CA (unless the launcher loaded it) and the chart services' leaves."""
    if not require_tls():
        return
    if current() is None:
        await ensure_ca(store)
    try:
        await cloud.ensure_system_tls()
    except Exception as e:  # noqa: BLE001 - the renewal loop retries; the console itself serves on its own leaf
        log.warning("tls: chart service certificates not written (%s: %s)", type(e).__name__, str(e)[:200])


async def renew_forever(store, cloud, every: float) -> None:
    """§39.2: every `every` seconds, re-read the CA (another replica may have rotated it) and re-issue due leaves."""
    while True:
        await asyncio.sleep(every)
        try:
            await ensure_ca(store)
            await advance(store, cloud)  # a rotation whose job died with its console
            out = await cloud.renew_tls()
            if out.get("zones") or out.get("system"):
                log.info("tls: renewed %s", ", ".join([*out.get("system", []), *out.get("zones", [])]))
        except Exception as e:  # noqa: BLE001 - the leaves in place keep serving; the next round retries
            log.warning("tls: renewal failed (%s: %s)", type(e).__name__, str(e)[:200])


def bundle() -> str:
    """The CA bundle peers verify against; empty when no CA is loaded in this process."""
    ca = current()
    return ca.bundle if ca else ""


def client_verify() -> ssl.SSLContext:
    """What every console HTTPS client verifies with: the system roots plus the install CA when one is loaded, so the
    same client reaches a public provider and an in-cluster service (Phoenix, an in-cluster LLM server) — GKE b32."""
    ctx = ssl.create_default_context()
    if b := bundle():
        ctx.load_verify_locations(cadata=b)
    return ctx


# ---- files (local compose, kind) -----------------------------------------------------------------------------------
def _write(path: Path, text: str, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)


def init_dir(out: Path, leaves: dict[str, list[str]]) -> list[str]:
    """CA in `<out>/ca` (kept when present), leaves in `<out>/<name>` (kept while fresh). Leaf keys are 0644: on a
    laptop the containers that read them run as different users; the CA key stays 0600."""
    out = Path(out)
    ca_dir = out / "ca"
    if (ca_dir / "ca.crt").exists() and (ca_dir / "ca.key").exists():
        ca = _from_dir(str(ca_dir))
    else:
        ca = new_ca("local")
        _write(ca_dir / "ca.crt", ca.cert, 0o644)
        _write(ca_dir / "ca.key", ca.key, 0o600)
    for name, sans in leaves.items():
        d = out / name
        crt = d / "tls.crt"
        if crt.exists() and not needs_renewal(crt.read_text(), ca) and (d / "tls.key").exists():
            continue
        leaf = ca.issue(sans)
        _write(d / "tls.key", leaf.key, 0o644)
        _write(crt, leaf.cert, 0o644)
        _write(d / "ca.crt", leaf.ca, 0o644)
    return list(leaves)


def self_signed(out: Path, hostname: str = "localhost") -> tuple[Path, Path]:
    out.mkdir(parents=True, exist_ok=True)
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(hostname)]), critical=False)
        .sign(key, hashes.SHA256())
    )
    key_path, cert_path = out / "key.pem", out / "cert.pem"
    key_path.write_text(_pem_key(key))
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return key_path, cert_path


def main(argv: list[str]) -> None:
    if not argv or argv[0] != "init":
        k, c = self_signed(Path(argv[0]), argv[1] if len(argv) > 1 else "localhost")
        print(k, c)
        return
    ap = argparse.ArgumentParser(prog="python -m ramen_console.tls init")
    ap.add_argument("dir")
    ap.add_argument("--leaf", action="append", default=[], help="name=san,san,…")
    ap.add_argument("--zone", action="append", default=[], help="name=<namespace>: a worker leaf (§39.2 names)")
    a = ap.parse_args(argv[1:])
    leaves = {n: s.split(",") for n, s in (x.split("=", 1) for x in a.leaf)}
    leaves |= {n: zone_names(ns) for n, ns in (x.split("=", 1) for x in a.zone)}
    print(" ".join(init_dir(Path(a.dir), leaves)))


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
