#!/usr/bin/env bash
# Bring up the kind cluster that proves the v0.4.0 multi-zone claims (CONTRACTS §12.3). Idempotent.
#   make kind-up      then   make kind-test      then   make kind-down
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
CLUSTER=${KIND_CLUSTER:-ramen}
VERSION=${VERSION:-$(cat "$ROOT/VERSION")}
GATEWAY_API_VERSION=${GATEWAY_API_VERSION:-v1.2.1}
METRICS_SERVER_VERSION=${METRICS_SERVER_VERSION:-v0.8.0}
KCTX="kind-$CLUSTER"
k() { kubectl --context "$KCTX" "$@"; }
say() { printf '\n== %s\n' "$*"; }

for tool in kind kubectl helm docker; do command -v "$tool" >/dev/null || { echo "missing $tool" >&2; exit 2; }; done

say "cluster $CLUSTER"
if kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  echo "already exists"
else
  kind create cluster --config "$HERE/cluster.yaml" --name "$CLUSTER" --wait 120s
fi

say "images"
docker image inspect "ramen-console:$VERSION" >/dev/null 2>&1 || { echo "build ramen-console:$VERSION first (make build)" >&2; exit 2; }
docker image inspect "ramen-worker:$VERSION" >/dev/null 2>&1 || { echo "build ramen-worker:$VERSION first (make build)" >&2; exit 2; }
docker build -q -t "ramen-worker-kind:$VERSION" --build-arg "BASE=ramen-worker:$VERSION" -f "$HERE/worker.Dockerfile" "$HERE"
kind load docker-image "ramen-console:$VERSION" "ramen-worker-kind:$VERSION" --name "$CLUSTER"

say "CRDs (Gateway API $GATEWAY_API_VERSION + the two GKE-only kinds the console renders)"
k apply -f "https://github.com/kubernetes-sigs/gateway-api/releases/download/$GATEWAY_API_VERSION/standard-install.yaml"
k apply -f "$HERE/crds-gke.yaml"

say "metrics-server $METRICS_SERVER_VERSION (the HPA reads CPU from it)"
k apply -f "https://github.com/kubernetes-sigs/metrics-server/releases/download/$METRICS_SERVER_VERSION/components.yaml"
k -n kube-system get deploy metrics-server -o json \
  | grep -q kubelet-insecure-tls \
  || k -n kube-system patch deployment metrics-server --type=json \
       -p '[{"op":"add","path":"/spec/template/spec/containers/0/args/-","value":"--kubelet-insecure-tls"}]'
k -n kube-system rollout status deployment/metrics-server --timeout=180s

say "namespace ramen-system + fake GCS (groups bucket stand-in)"
k create namespace ramen-system --dry-run=client -o yaml | k apply -f -
k label namespace ramen-system "ramen.io/routes=true" --overwrite
k apply -f "$HERE/fake-gcs.yaml"
k -n ramen-system rollout status deployment/fake-gcs --timeout=180s

say "console chart"
helm --kube-context "$KCTX" upgrade --install ramen "$ROOT/deploy/helm/ramen" \
  -n ramen-system -f "$HERE/values.yaml" --set "image.tag=$VERSION" --wait --timeout 5m
k apply -f "$HERE/nodeports.yaml"
k -n ramen-system rollout status deployment/console --timeout=300s

say "zone namespaces demo/a and demo/b"
k apply -f "$HERE/zones.yaml"
# kind enforces NetworkPolicy and has no Google front end in front of the workers: add the host-network hole
# (see netpol-host.yaml) so the NodePorts that stand in for the Gateway can reach the worker port.
NODE_CIDR=$(docker network inspect kind -f '{{range .IPAM.Config}}{{.Subnet}}{{"\n"}}{{end}}' | grep -v ':' | tr '\n' ' ')
NODE_CIDR=${NODE_CIDR:-172.18.0.0/16}
CIDRS="[$(for c in $(echo "$NODE_CIDR" | xargs) 10.244.0.0/16; do printf '{ipBlock: {cidr: %s}}, ' "$c"; done | sed 's/, $//')]"
for ns in ramen-demo-a ramen-demo-b; do
  sed -e "s|__NS__|$ns|" -e "s|__CIDRS__|$CIDRS|" "$HERE/netpol-host.yaml" | k apply -f -
done

say "ready"
cat <<TXT
console   http://localhost:18000            (admin@ramen.local / Kind-Console-1!)
zone a    localhost:18081  (gRPC h2c)       namespace ramen-demo-a, node label topology.kubernetes.io/zone=kind-a
zone b    localhost:18082  (gRPC h2c)       namespace ramen-demo-b, node label topology.kubernetes.io/zone=kind-b
stable    localhost:18084 / 18085          the same zones, stable track only (see zones.yaml)
fake GCS  http://localhost:18083/storage/v1/b/ramen-kind-groups/o
next      make kind-test
TXT
