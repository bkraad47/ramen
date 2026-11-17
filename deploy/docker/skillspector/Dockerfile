# §30.0: ghcr.io/bkraad47/ramen-skillspector:<tag> — NVIDIA SkillSpector (Apache-2.0) at the pinned tag, built the
# upstream way (venv in a builder stage, slim runtime with git + ca-certificates), as the console's scan Job image.
# The tag is pinned in one place per repo: this ARG, the chart's `skillspector.image` and CONTRACTS §30 (check_versions.py).
ARG SKILLSPECTOR_REF=v2.13.4

FROM python:3.12-slim-bookworm AS builder
ARG SKILLSPECTOR_REF
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN git clone --depth 1 --branch "${SKILLSPECTOR_REF}" https://github.com/NVIDIA/SkillSpector.git src \
    && python -m venv .venv && .venv/bin/pip install --no-cache-dir ./src && rm -rf src

FROM python:3.12-slim-bookworm
RUN apt-get update && apt-get install --no-install-recommends -y git ca-certificates && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin skillspector && mkdir -p /scan /out \
    && chown skillspector:skillspector /scan /out
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH" HOME=/home/skillspector
USER skillspector
WORKDIR /scan
ENTRYPOINT ["skillspector"]
