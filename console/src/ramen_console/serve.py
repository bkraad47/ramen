"""D86 (CONTRACTS §39.2): the console's launcher on a TLS install — `python -m ramen_console.serve`.

Ensures the install CA, issues the console's own leaf into a private temp dir, then runs uvicorn on TLS; while it runs
the leaf is renewed in place (`load_cert_chain` on the live context: new handshakes get the new pair)."""

import asyncio
import contextlib
import logging
import os
import tempfile
from pathlib import Path

from . import tls

log = logging.getLogger("ramen.tls")
SA_NAMESPACE = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")


def namespace() -> str:
    if ns := os.environ.get("RAMEN_NAMESPACE", "").strip():
        return ns
    try:
        return SA_NAMESPACE.read_text().strip() or "ramen-system"
    except OSError:
        return "ramen-system"


def build_store():
    """The store the app builds (secrets vaulted in the cloud's secret manager on a cloud), without the app."""
    from .secrets import make_secrets_backend
    from .storage import make_store
    from .storage.vaulted import VaultedStore

    store, secrets = make_store(), make_secrets_backend()
    return VaultedStore(store, secrets) if secrets.kind != "store" else store


def write_leaf(out: Path, ca: tls.CA) -> tuple[Path, Path]:
    leaf = ca.issue(tls.console_names(namespace()))
    out.mkdir(parents=True, exist_ok=True, mode=0o700)
    cert, key = out / "tls.crt", out / "tls.key"
    key.touch(mode=0o600)
    key.write_text(leaf.key)
    cert.write_text(leaf.cert)
    return cert, key


async def prepare(store, out: Path) -> tuple[Path, Path]:
    ca = await tls.ensure_ca(store)
    return write_leaf(out, ca)


def renew_once(ssl_context, out: Path) -> bool:
    """Re-issue the console's leaf when due (a third of its life left, or the CA rotated) and load it into the live
    context. True when it renewed."""
    ca = tls.current()
    cert = out / "tls.crt"
    if ca is None or ssl_context is None or not tls.needs_renewal(cert.read_text() if cert.exists() else "", ca):
        return False
    c, k = write_leaf(out, ca)
    ssl_context.load_cert_chain(str(c), str(k))
    log.info("tls: the console's certificate was renewed")
    return True


def register(ssl_context, out: Path) -> None:
    """§39.1: a rotation re-issues the console's leaf at once (`tls.renew_console_leaf`), and retire reads it.
    `ssl_context` may be a getter (uvicorn builds the context only when the server starts)."""
    get = ssl_context if callable(ssl_context) else lambda: ssl_context
    tls.set_console_leaf(lambda: renew_once(get(), out), lambda: (out / "tls.crt").read_text())


def poll_secs(every: float) -> float:
    """The leaf loop re-reads the CA doc at least each minute: a rotation another replica finished reaches this one's
    trust (and its own leaf) well within the rotation's settle time."""
    return min(every, 60.0)


async def renew_forever(config, out: Path, store, every: float) -> None:
    while True:
        await asyncio.sleep(poll_secs(every))
        try:
            await tls.ensure_ca(store)  # a rotation by another replica lands here
            renew_once(getattr(config, "ssl", None), out)
        except Exception as e:  # noqa: BLE001 - the current pair keeps serving; the next round retries
            log.warning("tls: console certificate renewal failed (%s)", type(e).__name__)


async def run(port: int, host: str = "0.0.0.0") -> None:  # noqa: S104 - the pod's own listener
    import uvicorn

    tls.refuse_plaintext()
    store = build_store()
    out = Path(tempfile.mkdtemp(prefix="ramen-tls-"))
    given = (os.environ.get("RAMEN_TLS_CERT", "").strip(), os.environ.get("RAMEN_TLS_KEY", "").strip())
    if all(given):  # local compose: a leaf `python -m ramen_console.tls init` issued; renewed by re-running it
        await tls.ensure_ca(store)
        cert, key = Path(given[0]), Path(given[1])
    else:
        cert, key = await prepare(store, out)
    config = uvicorn.Config(
        "ramen_console.app:app",
        host=host,
        port=port,
        ssl_certfile=str(cert),
        ssl_keyfile=str(key),
        proxy_headers=True,
    )
    server = uvicorn.Server(config)
    if not all(given):
        register(lambda: getattr(config, "ssl", None), out)
    every = float(os.environ.get("RAMEN_TLS_RENEW_SECS", "3600"))
    renew = asyncio.create_task(renew_forever(config, out, store, every) if not all(given) else asyncio.sleep(0))
    try:
        await server.serve()
    finally:
        tls.set_console_leaf(None, None)
        renew.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await renew


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=os.environ.get("RAMEN_LOG_LEVEL", "INFO"))
    asyncio.run(run(int(os.environ.get("RAMEN_CONSOLE_PORT", "8000"))))
