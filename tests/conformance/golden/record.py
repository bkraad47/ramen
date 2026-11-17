"""Record `legacy-0.8.0.json` (CONTRACTS §23.7). Run once against a node built from a clean v0.8.0 checkout:

    git worktree add /tmp/ramen-0.8.0 v0.8.0 && (cd /tmp/ramen-0.8.0/node-rs && cargo build)
    RAMEN_NODE_BIN=/tmp/ramen-0.8.0/node-rs/target/debug/ramen-node PYTHONPATH=/tmp/ramen-0.8.0/runtime-py/src \
      uv run python conformance/golden/record.py

`PYTHONPATH` puts the 0.8.0 `ramen_runtime` ahead of the editable install, so the node's sidecar is the 0.8.0 runtime.
Not a test: re-recording is a deliberate act (the file is the 0.8 contract), so nothing here runs under pytest."""

import json
import subprocess
import sys

from ramen_tests import golden
from ramen_tests.localnode import LocalNode, node_binary
from ramen_tests.sidecar import runtime_python


def main() -> int:
    where = subprocess.run(  # noqa: S603
        [runtime_python(), "-c", "import ramen_runtime, importlib.metadata as m; print(ramen_runtime.__file__)"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    out = {"recorded_from": "v0.8.0", "groups": {}}
    for group, bucket in golden.GROUPS.items():
        if not bucket.exists():
            print(f"skip {group}: {bucket} missing", file=sys.stderr)
            continue
        with LocalNode(bucket=bucket) as n:
            n.wait_serving()
            out["groups"][group] = {t: golden.record_transport(n, t, golden.CASES[group]) for t in ("grpc", "http")}
    golden.GOLDEN.write_text(json.dumps(out, indent=1, sort_keys=True, ensure_ascii=False) + "\n")
    print(f"node {node_binary()}\nruntime {where}\nwrote {golden.GOLDEN}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
