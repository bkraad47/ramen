#!/usr/bin/env bash
# Regenerate the vendored gRPC stubs in src/ramen_proto from ../proto (CONTRACTS §11). Needs the dev extra (grpcio-tools).
#   ./gen_proto.sh [out_src_dir]   (default: src) — the same command vendors the package into console/ and tests/.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=${1:-$HERE/src}
STAGE=$(mktemp -d)
mkdir -p "$STAGE/ramen_proto/ramen/v1"
cp "$HERE/../proto/ramen/v1/"*.proto "$STAGE/ramen_proto/ramen/v1/"
PY=${PYTHON:-$HERE/.venv/bin/python}
"$PY" -m grpc_tools.protoc -I "$STAGE" --python_out="$OUT" --grpc_python_out="$OUT" --pyi_out="$OUT" \
  ramen_proto/ramen/v1/mcp.proto ramen_proto/ramen/v1/admin.proto
for d in ramen_proto ramen_proto/ramen ramen_proto/ramen/v1; do
  [ -f "$OUT/$d/__init__.py" ] || printf '"""Generated gRPC stubs for proto/ramen/v1 (see runtime-py/gen_proto.sh). Do not edit."""\n' > "$OUT/$d/__init__.py"
done
rm -rf "$STAGE"
echo "generated into $OUT/ramen_proto"
