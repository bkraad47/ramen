"""Run the official MCP conformance suite against a real worker (CONTRACTS §23.7).

    cd tests && uv run python conformance_suite/run.py            # both wires
    uv run python conformance_suite/run.py --wire 2026-07-28      # one of them
    uv run python conformance_suite/run.py --keep                 # leave the worker + proxy up and print the URL

Starts `ramen-node` on `fixtures/conformance_group` with a generated key, puts `proxy.py` in front of it (the suite
cannot authenticate), and runs the pinned `@modelcontextprotocol/conformance` once per wire:

    2026-07-28   --requirements 2026-07-28      (stateless wire; the frozen requirement set)
    2025-06-18   --spec-version 2025-06-18      (initialize handshake wire; the active scenarios of that revision)

each with `--expected-failures baseline-<wire>.yaml`. The suite exits 1 on a failure that is not in the baseline AND
on a baseline entry that now passes, so the baselines cannot rot. Output: `results/<wire>/` (the suite's own
per-scenario files), `results/<wire>.log` (its console output) and `results/summary.json`. Exit code: 0 when every
wire ran clean against its baseline, 1 otherwise, 2 when a prerequisite is missing."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))

import proxy  # noqa: E402

from ramen_tests import env as E  # noqa: E402
from ramen_tests.localnode import LocalNode, free_port, node_binary  # noqa: E402
from ramen_tests.sidecar import runtime_importable  # noqa: E402

PACKAGE = "@modelcontextprotocol/conformance"
VERSION = "0.2.0-alpha.12"  # pinned: a new alpha can add scenarios; bump it together with the baselines
MIN_NODE = (20, 18, 1)  # undici 7, a dependency of the suite
WIRES = {
    "2026-07-28": ["--requirements", "2026-07-28"],
    "2025-06-18": ["--spec-version", "2025-06-18"],
}
RESULTS = HERE / "results"


def node_version() -> tuple[int, ...] | None:
    exe = shutil.which("node")
    if not exe or not shutil.which("npx"):
        return None
    out = subprocess.run([exe, "--version"], capture_output=True, text=True).stdout.strip()  # noqa: S603
    m = re.match(r"v?(\d+)\.(\d+)\.(\d+)", out)
    return tuple(int(x) for x in m.groups()) if m else None


def missing() -> str | None:
    """Why the suite cannot run here, or None."""
    v = node_version()
    if v is None:
        return "node/npx not on PATH"
    if v < MIN_NODE:
        return f"node {'.'.join(map(str, v))} < {'.'.join(map(str, MIN_NODE))}"
    if node_binary() is None:
        return "ramen-node binary not built (cargo build in node-rs, or RAMEN_NODE_BIN)"
    if not runtime_importable():
        return "ramen_runtime not importable (uv sync in runtime-py, or RAMEN_RUNTIME_PYTHON)"
    return None


def baseline(wire: str) -> Path:
    return HERE / f"baseline-{wire}.yaml"


def run_wire(url: str, wire: str, timeout: int) -> dict:
    out_dir = RESULTS / wire
    shutil.rmtree(out_dir, ignore_errors=True)
    out_dir.mkdir(parents=True)
    cmd = ["npx", "--yes", f"{PACKAGE}@{VERSION}", "server", "--url", url, *WIRES[wire],
           "--expected-failures", str(baseline(wire)), "-o", str(out_dir)]  # fmt: skip
    done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)  # noqa: S603
    log = done.stdout + ("\n--- stderr ---\n" + done.stderr if done.stderr.strip() else "")
    (RESULTS / f"{wire}.log").write_text(log)
    return {"wire": wire, "exit": done.returncode, "log": str(RESULTS / f"{wire}.log"), "tail": log[-3000:]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--wire", choices=[*WIRES, "both"], default="both")
    ap.add_argument("--timeout", type=int, default=900, help="seconds per wire")
    ap.add_argument("--keep", action="store_true", help="start the worker and the proxy, print the URL, wait")
    a = ap.parse_args(argv)
    if why := missing():
        print(f"cannot run the conformance suite: {why}", file=sys.stderr)
        return 2
    RESULTS.mkdir(exist_ok=True)
    key, port = "rmk_" + secrets.token_hex(16), free_port()
    origins = ",".join(f"http://{host}:{port}" for host in ("127.0.0.1", "localhost"))
    env = {"RAMEN_ALLOWED_ORIGINS": origins, "RAMEN_MAX_INFLIGHT": "64"}
    with LocalNode(bucket=E.FIXTURES / "conformance_group", key=key, env=env) as node:
        node.wait_serving()
        server = proxy.serve(f"http://{node.target}", key, "demo", "local", port)
        url = f"http://127.0.0.1:{port}/mcp"
        try:
            if a.keep:
                print(f"{url}\n(worker {node.target}, log {node.log}) — Ctrl-C to stop", flush=True)
                try:
                    node.proc.wait()
                except KeyboardInterrupt:
                    pass
                return 0
            wires = list(WIRES) if a.wire == "both" else [a.wire]
            results = [run_wire(url, w, a.timeout) for w in wires]
        finally:
            server.shutdown()
        shutil.copyfile(node.log, RESULTS / "worker.log")
    (RESULTS / "summary.json").write_text(json.dumps({"suite": f"{PACKAGE}@{VERSION}", "results": results}, indent=1))
    for r in results:
        print(f"=== {r['wire']}: exit {r['exit']} ({r['log']})\n{r['tail']}")
    return 0 if all(r["exit"] == 0 for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
