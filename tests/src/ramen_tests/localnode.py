"""Launch a real `ramen-node` binary on a free port against a fixture bucket, so §11 guards that need a specific node
configuration (CIDR deny, blocked names, TLS, log file, health before load) can be asserted without a deployment.
Skips unless the binary exists: RAMEN_NODE_BIN, else ../node-rs/target/{release,debug}/ramen-node."""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import grpc
import pytest

from . import env as E
from .mcp_client import Node
from .sidecar import runtime_importable, runtime_python


def node_binary() -> Path | None:
    p = E.env("RAMEN_NODE_BIN")
    if p:
        return Path(p)
    for kind in ("release", "debug"):
        c = E.RAMEN_DIR / "node-rs" / "target" / kind / "ramen-node"
        if c.exists():
            return c
    return None


def require_binary() -> Path:
    b = node_binary()
    if b is None or not b.exists():
        pytest.skip("ramen-node binary not built (cargo build --release in node-rs, or RAMEN_NODE_BIN)")
    if not runtime_importable():
        pytest.skip("ramen_runtime not importable (uv sync runtime-py or RAMEN_RUNTIME_PYTHON)")
    return b


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def self_signed(dir: Path, cn: str = "localhost") -> tuple[Path, Path]:
    """openssl self-signed cert for `cn` (SAN DNS:localhost, IP:127.0.0.1) → (cert, key) PEM paths."""
    if not shutil.which("openssl"):
        pytest.skip("openssl not on PATH (needed for the TLS case)")
    cert, key = dir / "tls.crt", dir / "tls.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "2", "-subj", f"/CN={cn}",
            "-addext", "subjectAltName=DNS:localhost,IP:127.0.0.1", "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


class LocalNode:
    """Context manager: start the binary with `env` overrides; `.node` is a Node client, `.log` the RAMEN_LOG_FILE."""

    def __init__(
        self,
        bucket: Path | str = E.FIXTURES / "demo_group",
        env: dict | None = None,
        key: str = "test-key",
        admin_key: str = "test-admin",
        tls: bool = False,
    ):
        self.binary = require_binary()
        self.port = free_port()
        self.dir = Path(tempfile.mkdtemp(prefix="ramen-node-"))
        self.log = self.dir / "worker.log"
        self.key, self.admin_key, self.tls = key, admin_key, tls
        self.env = {
            **os.environ,
            "RAMEN_BUCKET": str(bucket),
            "RAMEN_PYTHON": runtime_python(),
            "RAMEN_NODE_PORT": str(self.port),
            "RAMEN_MCP_KEYS": key,
            "RAMEN_ADMIN_KEY": admin_key,
            "RAMEN_GROUP": "demo",
            "RAMEN_ENV": "dev",
            "RAMEN_ZONE": "local",
            "RAMEN_ALLOWED_CIDRS": "127.0.0.0/8,::1/128",
            "RAMEN_ADMIN_CIDRS": "127.0.0.0/8,::1/128",
            "RAMEN_LOG_FILE": str(self.log),
            "RAMEN_MAX_INFLIGHT": "32",
        }
        for k in (
            "RAMEN_TLS_CERT",
            "RAMEN_TLS_KEY",
            "RAMEN_BLOCKED",
            "RAMEN_TRUST_PROXY",
            "RAMEN_TRUST_PROXY_HOPS",
            "RAMEN_REFLECTION",
            "RAMEN_CONFIG",
        ):
            self.env.pop(k, None)
        self.env.pop("RAMEN_DOTENV", None)
        self.ca = None
        if tls:
            cert, keyf = self_signed(self.dir)
            self.env["RAMEN_TLS_CERT"], self.env["RAMEN_TLS_KEY"] = str(cert), str(keyf)
            self.ca = str(cert)
        self.env.update(env or {})
        self.proc: subprocess.Popen | None = None
        self.stdout = self.dir / "stdout.log"

    @property
    def target(self) -> str:
        return f"127.0.0.1:{self.port}"

    def client(self, key=None, **kw) -> Node:
        kw = {"tls": self.tls, "ca": self.ca, "admin_key": self.admin_key, "group": "demo", "zone": "local", **kw}
        return Node(self.target, self.key if key is None else key, **kw)

    def __enter__(self):
        self.proc = subprocess.Popen(  # noqa: S603
            [str(self.binary)], env=self.env, stdout=open(self.stdout, "ab"), stderr=subprocess.STDOUT, cwd=self.dir
        )
        self.node = self.client()
        return self

    def __exit__(self, *_):
        self.node.close()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def output(self) -> str:
        return self.stdout.read_text(errors="replace")[-4000:] if self.stdout.exists() else ""

    def wait_answering(self, timeout: float = 60) -> str:
        """Until Health/Check answers with any status (the port is open). → status name."""
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            if self.proc and self.proc.poll() is not None:
                raise AssertionError(f"ramen-node exited {self.proc.returncode}:\n{self.output()}")
            try:
                return self.node.health()
            except grpc.RpcError as e:
                last = e.code()
            time.sleep(0.2)
        raise AssertionError(f"{self.target} not answering after {timeout}s: {last}\n{self.output()}")

    def wait_serving(self, timeout: float = 120) -> None:
        deadline, last = time.monotonic() + timeout, None
        while time.monotonic() < deadline:
            try:
                last = self.node.health()
                if last == "SERVING":
                    return
            except grpc.RpcError as e:
                last = e.code()
            time.sleep(0.3)
        raise AssertionError(f"{self.target} not SERVING after {timeout}s: {last}\n{self.output()}")

    def log_lines(self) -> list[dict]:
        import json

        if not self.log.exists():
            return []
        out = []
        for line in self.log.read_text().splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
        return out
