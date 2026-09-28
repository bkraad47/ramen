# Worker image: ramen-node (Rust) + ramen_runtime (Python 3.14, gcp+aws extras) in one container.
# Build context = ramen repo root (needs node-rs/ and runtime-py/):
#   docker build -f node-rs/Dockerfile -t ramen-worker:$(cat VERSION) .                       # native
#   docker buildx build --platform linux/amd64 -f node-rs/Dockerfile -t <ar>/worker:<v> --push . # GKE
# The Rust stage runs on the build host and cross-compiles for TARGETARCH (no QEMU for cargo).
FROM --platform=$BUILDPLATFORM rust:1.98.1-slim-bookworm AS build
ARG TARGETARCH
RUN case "$TARGETARCH" in amd64) echo x86_64-unknown-linux-gnu ;; arm64) echo aarch64-unknown-linux-gnu ;; *) echo "unsupported $TARGETARCH" >&2; exit 1 ;; esac > /target.txt \
 && rustup target add "$(cat /target.txt)" \
 && apt-get update && apt-get install -y --no-install-recommends gcc-x86-64-linux-gnu gcc-aarch64-linux-gnu libc6-dev-amd64-cross libc6-dev-arm64-cross && rm -rf /var/lib/apt/lists/*
ENV CARGO_TARGET_X86_64_UNKNOWN_LINUX_GNU_LINKER=x86_64-linux-gnu-gcc CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc
WORKDIR /src/node-rs
COPY node-rs/Cargo.toml node-rs/Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && touch src/lib.rs && cargo build --release --target "$(cat /target.txt)" && rm -rf src
COPY node-rs/src ./src
RUN touch src/main.rs src/lib.rs && cargo build --release --target "$(cat /target.txt)" \
 && cp "target/$(cat /target.txt)/release/ramen-node" /ramen-node

FROM python:3.14-slim
ARG VERSION=0.3.0
LABEL org.opencontainers.image.title="ramen-worker" org.opencontainers.image.version="${VERSION}" org.opencontainers.image.licenses="BSD-3-Clause"
RUN useradd -r -u 10001 -m -d /home/ramen ramen && mkdir -p /buckets /data/bucket /opt/venv && chown -R ramen:ramen /buckets /data /opt/venv
USER ramen
COPY --chown=ramen:ramen runtime-py /opt/ramen-runtime
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check '/opt/ramen-runtime[gcp,aws]'
COPY --from=build /ramen-node /usr/local/bin/ramen-node
ENV RAMEN_NODE_PORT=8080 RAMEN_BUCKET=/buckets/default RAMEN_PYTHON=/opt/venv/bin/python PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/buckets"]
HEALTHCHECK --interval=15s --timeout=3s CMD ["/opt/venv/bin/python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]
ENTRYPOINT ["ramen-node"]
