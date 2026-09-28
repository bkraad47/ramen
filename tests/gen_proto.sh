#!/usr/bin/env bash
# Regenerate tests/src/ramen_proto/* from ../proto/ramen/v1/*.proto (CONTRACTS §11). Run from tests/: ./gen_proto.sh
# Uses grpcio-tools (bundled protoc) so no system protoc is required; imports are rewritten to be package-relative.
set -euo pipefail
cd "$(dirname "$0")"
OUT=src/ramen_proto
uv run --group dev python -m grpc_tools.protoc -I ../proto/ramen/v1 --python_out="$OUT" --pyi_out="$OUT" --grpc_python_out="$OUT" ../proto/ramen/v1/mcp.proto ../proto/ramen/v1/admin.proto
for f in "$OUT"/*_pb2_grpc.py; do
  sed -i.bak -E 's/^import (mcp|admin)_pb2 as /from . import \1_pb2 as /' "$f" && rm -f "$f.bak"
done
cat > "$OUT/__init__.py" <<'PY'
"""Generated gRPC stubs for ramen.v1 (proto/ramen/v1/*.proto). Do not edit; run tests/gen_proto.sh."""

from . import admin_pb2, admin_pb2_grpc, mcp_pb2, mcp_pb2_grpc

__all__ = ["admin_pb2", "admin_pb2_grpc", "mcp_pb2", "mcp_pb2_grpc"]
PY
echo "regenerated $OUT"
