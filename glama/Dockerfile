# Self-contained Ramen for MCP directories (Glama): the real worker (ramen-node + Python runtime) serving the demo
# group (github.com/bkraad47/ramen-demo-mcp-group) inside the container, with ramen-mcp-bridge on stdio in front.
# No console, cloud or network at runtime. Build context = ramen repo root:
#   docker build -f glama/Dockerfile -t ramen-glama . && docker run -i --rm ramen-glama
FROM rust:1.98.1-slim-bookworm AS build
COPY proto /src/proto
WORKDIR /src/node-rs
COPY node-rs/Cargo.toml node-rs/Cargo.lock node-rs/build.rs ./
COPY node-rs/src ./src
RUN cargo build --release && cp target/release/ramen-node /ramen-node

FROM python:3.14-slim
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && git clone --depth 1 https://github.com/bkraad47/ramen-demo-mcp-group /buckets/default \
 && useradd -r -u 10001 -m -d /home/ramen ramen && chown -R ramen:ramen /buckets
COPY runtime-py /opt/ramen-runtime
RUN python -m venv /opt/venv && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check /opt/ramen-runtime \
 && /opt/venv/bin/pip install --no-cache-dir --disable-pip-version-check 'ramen-mcp-bridge==0.2.2'
COPY --from=build /ramen-node /usr/local/bin/ramen-node
COPY glama/start.sh /usr/local/bin/ramen-stdio
USER ramen
ENV RAMEN_NODE_PORT=8080 RAMEN_BUCKET=/buckets/default RAMEN_PYTHON=/opt/venv/bin/python PYTHONUNBUFFERED=1
ENTRYPOINT ["sh", "/usr/local/bin/ramen-stdio"]
