# Worker image: ramen-node (Rust) + ramen_runtime (Python 3.14) in one container.
# Build context = ramen repo root (needs node-rs/ and runtime-py/):
#   docker build -f node-rs/Dockerfile -t ramen-worker:$(cat VERSION) .
FROM rust:1.98.1-slim-bookworm AS build
WORKDIR /src/node-rs
COPY node-rs/Cargo.toml node-rs/Cargo.lock ./
RUN mkdir src && echo 'fn main() {}' > src/main.rs && touch src/lib.rs && cargo build --release && rm -rf src
COPY node-rs/src ./src
RUN touch src/main.rs src/lib.rs && cargo build --release && strip target/release/ramen-node

FROM python:3.14-slim
ARG VERSION=0.1.0
LABEL org.opencontainers.image.title="ramen-worker" org.opencontainers.image.version="${VERSION}" org.opencontainers.image.licenses="BSD-3-Clause"
RUN useradd -r -u 10001 -m -d /home/ramen ramen && mkdir -p /buckets /opt/venv && chown -R ramen:ramen /buckets /opt/venv
USER ramen
COPY --chown=ramen:ramen runtime-py /opt/ramen-runtime
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check /opt/ramen-runtime
COPY --from=build /src/node-rs/target/release/ramen-node /usr/local/bin/ramen-node
ENV RAMEN_NODE_PORT=8080 RAMEN_BUCKET=/buckets/default RAMEN_PYTHON=/opt/venv/bin/python PYTHONUNBUFFERED=1
EXPOSE 8080
VOLUME ["/buckets"]
HEALTHCHECK --interval=15s --timeout=3s CMD ["/opt/venv/bin/python", "-c", "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz')"]
ENTRYPOINT ["ramen-node"]
