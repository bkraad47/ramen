FROM python:3.14-slim
ARG RAMEN_VERSION=0.5.3
LABEL org.opencontainers.image.title="ramen-console" org.opencontainers.image.version="${RAMEN_VERSION}" org.opencontainers.image.licenses="BSD-3-Clause"
ENV PYTHONUNBUFFERED=1 UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_CACHE=1 UV_PROJECT_ENVIRONMENT=/app/.venv PATH="/app/.venv/bin:$PATH"
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates && rm -rf /var/lib/apt/lists/* \
 && useradd -r -u 10001 -d /app ramen
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra gcp --extra aws --no-install-project
COPY src ./src
COPY entrypoint.sh ./
RUN uv sync --frozen --no-dev --extra gcp --extra aws && echo "$RAMEN_VERSION" > /app/VERSION \
 && mkdir -p /data/buckets /data/backups && chown -R ramen:ramen /app /data
USER ramen
ENV RAMEN_BUCKET_ROOT=/data/buckets RAMEN_BACKUP_ROOT=/data/backups RAMEN_CONSOLE_PORT=8000
EXPOSE 8000 8443
HEALTHCHECK --interval=30s --timeout=3s CMD python -c "import urllib.request,os,ssl;tls=os.environ.get('RAMEN_TLS')=='self';u=('https://127.0.0.1:8443' if tls else 'http://127.0.0.1:'+os.environ.get('RAMEN_CONSOLE_PORT','8000'))+'/healthz';urllib.request.urlopen(u,context=ssl._create_unverified_context() if tls else None)" || exit 1
ENTRYPOINT ["/app/entrypoint.sh"]
