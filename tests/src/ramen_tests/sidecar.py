"""Spawn `python -m ramen_runtime --bucket <dir>` and speak CONTRACTS §2 over stdin/stdout."""
import json
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

from .env import RAMEN_DIR, env


class SidecarError(Exception):
    def __init__(self, error: dict):
        super().__init__(error.get("message", str(error)))
        self.error = error


def runtime_python() -> str:
    p = env("RAMEN_RUNTIME_PYTHON")
    if p:
        return p
    venv = RAMEN_DIR / "runtime-py" / ".venv" / "bin" / "python"
    return str(venv) if venv.exists() else sys.executable


def runtime_importable() -> bool:
    try:
        r = subprocess.run([runtime_python(), "-c", "import ramen_runtime"], capture_output=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0


class Sidecar:
    def __init__(self, bucket: Path | str, extra_env: dict | None = None):
        self.bucket = str(bucket)
        self.env = {**os.environ, **(extra_env or {})}
        self.proc: subprocess.Popen | None = None
        self._out: queue.Queue = queue.Queue()
        self._err: list[str] = []
        self._id = 0

    def __enter__(self):
        self.proc = subprocess.Popen(
            [runtime_python(), "-m", "ramen_runtime", "--bucket", self.bucket],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=self.env, bufsize=1,
        )
        threading.Thread(target=self._pump, args=(self.proc.stdout, self._out.put), daemon=True).start()
        threading.Thread(target=self._pump, args=(self.proc.stderr, self._err.append), daemon=True).start()
        return self

    def __exit__(self, *_):
        self.close()

    @staticmethod
    def _pump(stream, sink):
        for line in stream:
            sink(line.rstrip("\n"))

    def raw(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        self._id += 1
        req = {"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}
        assert self.proc and self.proc.stdin
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"{method}: no response in {timeout}s; stderr tail: {self.stderr[-2000:]}")
            try:
                line = self._out.get(timeout=min(left, 1))
            except queue.Empty:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"sidecar exited {self.proc.returncode}; stderr: {self.stderr[-2000:]}")
                continue
            if not line.strip():
                continue
            msg = json.loads(line)
            if msg.get("id") == self._id:
                return msg

    def call(self, method: str, params: dict | None = None, timeout: float = 60):
        msg = self.raw(method, params, timeout)
        if "error" in msg:
            raise SidecarError(msg["error"])
        return msg["result"]

    @property
    def stderr(self) -> str:
        return "\n".join(self._err)

    def wait_exit(self, timeout: float = 10) -> int | None:
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return None

    def close(self):
        if not self.proc:
            return
        if self.proc.poll() is None:
            try:
                self.proc.stdin.close()
            except OSError:
                pass
            if self.wait_exit(5) is None:
                self.proc.kill()
                self.proc.wait()
