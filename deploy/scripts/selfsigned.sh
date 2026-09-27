#!/usr/bin/env bash
# Self-signed TLS Secret for the console Ingress (D17). Usage: selfsigned.sh <ip-or-host> [namespace] [secret] [days]
set -euo pipefail
host="${1:?usage: selfsigned.sh <ip-or-host> [namespace] [secret] [days]}"; ns="${2:-ramen-system}"; name="${3:-ramen-console-tls}"; days="${4:-825}"
tmp="$(mktemp -d)"; trap 'rm -rf "$tmp"' EXIT
if [[ "$host" =~ ^[0-9.]+$ ]]; then san="IP:$host"; else san="DNS:$host"; fi
openssl req -x509 -newkey rsa:2048 -nodes -days "$days" -keyout "$tmp/key.pem" -out "$tmp/cert.pem" \
  -subj "/CN=$host/O=Ramen" -addext "subjectAltName=$san" >/dev/null 2>&1
kubectl create namespace "$ns" --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n "$ns" create secret tls "$name" --cert="$tmp/cert.pem" --key="$tmp/key.pem" --dry-run=client -o yaml | kubectl apply -f -
