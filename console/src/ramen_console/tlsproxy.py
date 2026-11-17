"""D86 (CONTRACTS §39.3): a TLS front for a server that speaks only plaintext on loopback (promptfoo, Jaeger's query).

`python -m ramen_console.tlsproxy --listen 0.0.0.0:3443 --upstream 127.0.0.1:3000 --cert tls.crt --key tls.key`
terminates TLS and pipes bytes; the pair is re-read when it changes (checked every `--reload-secs`), so a renewed
Secret needs no restart.

D92 (§39.7): with `RAMEN_PROXY_PASSWORD` set, the first HTTP request of every connection must carry the operator's
credential (`Authorization: Basic <RAMEN_PROXY_USER:password>` or `Bearer <password>`) — then the connection is that
client's and is piped as before — or the upload credential (`RAMEN_PROXY_UPLOAD_USER` / `_PASSWORD`), which may send
exactly one request matching an `--upload-route "<METHOD> <path regex>"` with a Content-Length body, forwarded with
`Connection: close`; nothing else on that connection reaches the upstream. No path is open without a credential."""

import argparse
import asyncio
import base64
import binascii
import contextlib
import hmac
import logging
import os
import re
import ssl
from dataclasses import dataclass, field
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


HEAD_LIMIT = 64 * 1024
BODY_LIMIT = 32 * 1024 * 1024  # a share upload chunk is ~1 MB
HEAD_TIMEOUT = 30.0


def routes(specs: list[str]) -> list[tuple[str, re.Pattern]]:
    out = []
    for spec in specs:
        method, _, pattern = spec.strip().partition(" ")
        if not pattern or not method.isalpha():
            raise ValueError(f"--upload-route {spec!r}: expected '<METHOD> <path regex>'")
        out.append((method.upper(), re.compile(pattern.strip())))
    return out


@dataclass
class Auth:
    full: list[tuple[str, str]]
    upload: list[tuple[str, str]] = field(default_factory=list)
    routes: list[tuple[str, re.Pattern]] = field(default_factory=list)
    realm: str = "ramen"

    def check(self, header: str | None) -> str | None:
        """`full` | `upload` | None for an Authorization header value."""
        scheme, _, value = (header or "").strip().partition(" ")
        value = value.strip()
        if scheme.lower() == "bearer":
            return "full" if any(hmac.compare_digest(value.encode(), pw.encode()) for _, pw in self.full) else None
        if scheme.lower() != "basic":
            return None
        try:
            user, _, pw = base64.b64decode(value, validate=True).decode().partition(":")
        except (binascii.Error, UnicodeDecodeError):
            return None
        for kind, creds in (("full", self.full), ("upload", self.upload)):
            for u, p in creds:  # every pair compared: no early exit on the user name
                ok = hmac.compare_digest(user.encode(), u.encode()) & hmac.compare_digest(pw.encode(), p.encode())
                if ok:
                    return kind
        return None

    def allowed(self, method: str, path: str) -> bool:
        return ".." not in path and any(m == method and r.fullmatch(path) for m, r in self.routes)


def auth_from_env(env, upload_routes: list[str]) -> Auth | None:
    if not (pw := env.get("RAMEN_PROXY_PASSWORD", "").strip()):
        return None
    upload = []
    if up := env.get("RAMEN_PROXY_UPLOAD_PASSWORD", "").strip():
        upload = [(env.get("RAMEN_PROXY_UPLOAD_USER", "evals").strip() or "evals", up)]
    return Auth([(env.get("RAMEN_PROXY_USER", "admin").strip() or "admin", pw)], upload, routes(upload_routes))


def _answer(code: int, reason: str, extra: str = "") -> bytes:
    body = f"{code} {reason}\n".encode()
    return (
        f"HTTP/1.1 {code} {reason}\r\n{extra}Content-Type: text/plain\r\nContent-Length: {len(body)}\r\n"
        "Connection: close\r\n\r\n"
    ).encode() + body


def _parse(head: bytes) -> tuple[str, str, list[tuple[str, str]]] | None:
    try:
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ")
    except ValueError:
        return None
    if not (method.isalpha() and version.startswith("HTTP/1.")):
        return None
    headers = []
    for line in lines[1:]:
        if line:
            name, sep, value = line.partition(":")
            if not sep or not name.strip() or name != name.strip():
                return None
            headers.append((name.lower(), value.strip()))
    return method.upper(), target.split("?", 1)[0], headers


async def _refuse(writer, data: bytes) -> None:
    with contextlib.suppress(Exception):
        writer.write(data)
        await writer.drain()
    with contextlib.suppress(Exception):
        writer.close()


async def _gate(reader, writer, auth: Auth) -> tuple[bytes, bool] | None:
    """The first request's head (and, for an upload, its body): (bytes to forward, whole connection?) or None."""
    try:
        head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), HEAD_TIMEOUT)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError, ConnectionError, ssl.SSLError):
        await _refuse(writer, _answer(400, "Bad Request"))
        return None
    parsed = _parse(head[:-4])
    if parsed is None:
        await _refuse(writer, _answer(400, "Bad Request"))
        return None
    method, path, headers = parsed
    values = dict(headers)
    kind = auth.check(values.get("authorization"))
    if kind is None:
        await _refuse(writer, _answer(401, "Unauthorized", f'WWW-Authenticate: Basic realm="{auth.realm}"\r\n'))
        return None
    if kind == "full":
        return head, True
    if not auth.allowed(method, path):
        await _refuse(writer, _answer(403, "Forbidden"))
        return None
    length = values.get("content-length", "")
    if "transfer-encoding" in values or not length.isdigit():
        await _refuse(writer, _answer(411, "Length Required"))
        return None
    if int(length) > BODY_LIMIT:
        await _refuse(writer, _answer(413, "Content Too Large"))
        return None
    try:
        body = await asyncio.wait_for(reader.readexactly(int(length)), HEAD_TIMEOUT)
    except (asyncio.IncompleteReadError, TimeoutError, ConnectionError, ssl.SSLError):
        await _refuse(writer, _answer(400, "Bad Request"))
        return None
    lines = head[:-4].split(b"\r\n")
    kept = [lines[0]] + [
        h for h in lines[1:] if h.split(b":", 1)[0].strip().lower() not in (b"connection", b"keep-alive")
    ]
    return b"\r\n".join([*kept, b"Connection: close", b"", b""]) + body, False


def handler(upstream: tuple[str, int], auth: Auth | None = None):
    async def handle(reader, writer):
        first, whole = b"", True
        if auth is not None:
            if (gated := await _gate(reader, writer, auth)) is None:
                return
            first, whole = gated
        try:
            up_reader, up_writer = await asyncio.open_connection(*upstream)
        except OSError as e:
            log.warning("tlsproxy: upstream %s:%s unreachable (%s)", *upstream, e.strerror)
            writer.close()
            return
        if first:
            up_writer.write(first)
            await up_writer.drain()
        if whole:
            await asyncio.gather(_pipe(reader, up_writer), _pipe(up_reader, writer))
        else:  # one upload: only the answer comes back, the client's further bytes never leave this process
            await _pipe(up_reader, writer)
            with contextlib.suppress(Exception):
                up_writer.close()

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


async def serve(listen: str, upstream: str, cert: str, key: str, reload_secs: float = 60.0, auth: Auth | None = None):
    ctx = context(cert, key)
    server = await asyncio.start_server(handler(_addr(upstream), auth), *_addr(listen), ssl=ctx, limit=HEAD_LIMIT)
    reloader = asyncio.create_task(reload_forever(ctx, cert, key, reload_secs))
    return server, reloader


async def main(argv: list[str] | None = None) -> None:  # pragma: no cover - the CLI around serve()
    ap = argparse.ArgumentParser(prog="python -m ramen_console.tlsproxy")
    ap.add_argument("--listen", required=True)
    ap.add_argument("--upstream", required=True)
    ap.add_argument("--cert", required=True)
    ap.add_argument("--key", required=True)
    ap.add_argument("--reload-secs", type=float, default=60.0)
    ap.add_argument(
        "--upload-route", action="append", default=[], help="'<METHOD> <path regex>' (D92 upload credential)"
    )
    ap.add_argument("--require-auth", action="store_true", help="exit when RAMEN_PROXY_PASSWORD is empty (D92)")
    a = ap.parse_args(argv)
    auth = auth_from_env(os.environ, a.upload_route)
    if a.require_auth and auth is None:
        raise SystemExit(
            "tlsproxy: --require-auth and no RAMEN_PROXY_PASSWORD (D92): refusing to serve without a credential"
        )
    log.info("tlsproxy: %s", "a credential is required on every connection" if auth else "no credential (plain pipe)")
    server, _ = await serve(a.listen, a.upstream, a.cert, a.key, a.reload_secs, auth)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
