"""D86 (CONTRACTS §39.3): a TLS front for a server that speaks only plaintext on loopback (the promptfoo server).

`python -m ramen_console.tlsproxy --listen 0.0.0.0:3443 --upstream 127.0.0.1:3000 --cert tls.crt --key tls.key`
terminates TLS and pipes bytes; the pair is re-read when it changes (checked every `--reload-secs`), so a renewed
Secret needs no restart."""

import argparse
import asyncio
import contextlib
import logging
import ssl
from pathlib import Path

log = logging.getLogger("ramen.tlsproxy")


def context(cert: str, key: str) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(cert, key)
    return ctx


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, ssl.SSLError):
        pass
    finally:
        with contextlib.suppress(Exception):
            writer.close()


def handler(upstream: tuple[str, int]):
    async def handle(reader, writer):
        try:
            up_reader, up_writer = await asyncio.open_connection(*upstream)
        except OSError as e:
            log.warning("tlsproxy: upstream %s:%s unreachable (%s)", *upstream, e.strerror)
            writer.close()
            return
        await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))

    return handle


def _stamp(*paths: str) -> tuple:
    return tuple(Path(p).read_bytes() for p in paths)


async def reload_forever(ctx: ssl.SSLContext, cert: str, key: str, every: float) -> None:
    seen = _stamp(cert, key)
    while True:
        await asyncio.sleep(every)
        try:
            now = _stamp(cert, key)
            if now != seen:
                ctx.load_cert_chain(cert, key)
                seen = now
                log.info("tlsproxy: certificate reloaded")
        except (OSError, ssl.SSLError) as e:  # a half-written Secret swap: keep the old pair, retry next round
            log.warning("tlsproxy: reload failed (%s)", type(e).__name__)


def _addr(text: str) -> tuple[str, int]:
    host, port = text.rsplit(":", 1)
    return host.strip("[]"), int(port)


async def serve(listen: str, upstream: str, cert: str, key: str, reload_secs: float = 60.0):
    ctx = context(cert, key)
    server = await asyncio.start_server(handler(_addr(upstream)), *_addr(listen), ssl=ctx)
    reloader = asyncio.create_task(reload_forever(ctx, cert, key, reload_secs))
    return server, reloader


async def main(argv: list[str] | None = None) -> None:  # pragma: no cover - the CLI around serve()
    ap = argparse.ArgumentParser(prog="python -m ramen_console.tlsproxy")
    ap.add_argument("--listen", required=True)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--reload-secs", type=float, default=60.0)
    a = ap.parse_args(argv)
    server, _ = await serve(a.listen, a.upstream, a.cert, a.key, a.reload_secs)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
