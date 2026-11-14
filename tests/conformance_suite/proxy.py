"""A credential-adding reverse proxy for the official MCP conformance suite (CONTRACTS §23.7).

The suite cannot send a bearer key or routing headers, and a Ramen worker serves nothing without them. This proxy
sits in front of one worker on localhost and adds exactly three headers to every request — `Authorization: Bearer
<key>`, `ramen-group`, `ramen-zone` — and changes nothing else:

- `Host` and `Origin` pass through as sent, so the suite's DNS-rebinding scenario tests the *worker's* Origin check.
  The worker's `RAMEN_ALLOWED_ORIGINS` must therefore name this proxy's own origin (`http://127.0.0.1:<port>`),
  which is what the suite sends as the legitimate one.
- The answer is streamed as it arrives (no buffering), so SSE answers — progress, `subscriptions/listen` — reach the
  suite event by event.

Standard library only. Not a production component: it holds a group key and trusts every local caller."""

from __future__ import annotations

import argparse
import http.client
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer",
              "transfer-encoding", "upgrade", "content-length"}  # fmt: skip
ADDED = ("authorization", "ramen-group", "ramen-zone")


def make_handler(upstream: str, key: str, group: str, zone: str) -> type[BaseHTTPRequestHandler]:
    target = urlsplit(upstream)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):  # the worker's access log is the record; keep the suite's output readable
            pass

        def _forward(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else None
            headers = [(k, v) for k, v in self.headers.items() if k.lower() not in HOP_BY_HOP | set(ADDED)]
            headers += [("Authorization", f"Bearer {key}"), ("ramen-group", group), ("ramen-zone", zone)]
            conn = http.client.HTTPConnection(target.hostname, target.port, timeout=300)
            try:
                conn.putrequest(self.command, self.path, skip_host=True, skip_accept_encoding=True)
                for name, value in headers:  # Host included, exactly as the client sent it
                    conn.putheader(name, value)
                if body is not None:
                    conn.putheader("Content-Length", str(len(body)))
                conn.endheaders(body)
                response = conn.getresponse()
                self._relay(response)
            except (BrokenPipeError, ConnectionResetError):
                pass  # the client went away (the suite aborts streams it is done with)
            except OSError as e:
                self.send_error(502, f"upstream: {e}")
            finally:
                conn.close()

        def _relay(self, response: http.client.HTTPResponse):
            self.send_response_only(response.status, response.reason)
            for name, value in response.getheaders():
                if name.lower() not in HOP_BY_HOP:
                    self.send_header(name, value)
            fixed = response.getheader("Content-Length")
            bodiless = self.command == "HEAD" or response.status in (204, 304) or 100 <= response.status < 200
            if bodiless:
                if fixed is not None:
                    self.send_header("Content-Length", fixed)
                self.end_headers()
                return
            if fixed is not None:
                self.send_header("Content-Length", fixed)
                self.end_headers()
                self.wfile.write(response.read())
                self.wfile.flush()
                return
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            while chunk := response.read1(65536):  # returns as soon as the worker flushed something
                self.wfile.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                self.wfile.flush()
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()

        do_GET = do_POST = do_DELETE = do_OPTIONS = do_HEAD = _forward  # noqa: N815 - http.server's naming

    return Handler


def serve(
    upstream: str, key: str, group: str, zone: str, port: int = 0, host: str = "127.0.0.1"
) -> ThreadingHTTPServer:
    """Start the proxy on a daemon thread → the server (`.server_address[1]` is the port; `.shutdown()` stops it)."""
    server = ThreadingHTTPServer((host, port), make_handler(upstream, key, group, zone))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main(argv: list[str] | None = None) -> int:
    import os

    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--upstream", required=True, help="the worker, e.g. http://127.0.0.1:8080")
    ap.add_argument("--port", type=int, default=0)
    ap.add_argument("--group", default="demo")
    ap.add_argument("--zone", default="local")
    a = ap.parse_args(argv)
    key = os.environ.get("RAMEN_MCP_KEY")  # never an argument: a key on a command line ends up in `ps`
    if not key:
        ap.error("RAMEN_MCP_KEY is not set")
    server = serve(a.upstream, key, a.group, a.zone, a.port)
    print(f"http://127.0.0.1:{server.server_address[1]}/mcp", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
