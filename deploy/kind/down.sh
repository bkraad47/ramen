#!/usr/bin/env bash
# Delete the kind cluster and the kind-only worker image. Leaves ramen-console/ramen-worker images alone.
set -euo pipefail
CLUSTER=${KIND_CLUSTER:-ramen}
VERSION=${VERSION:-$(cat "$(cd "$(dirname "$0")/../.." && pwd)/VERSION")}
kind get clusters 2>/dev/null | grep -qx "$CLUSTER" && kind delete cluster --name "$CLUSTER" || echo "no cluster $CLUSTER"
docker image rm -f "ramen-worker-kind:$VERSION" >/dev/null 2>&1 || true
echo "down"
