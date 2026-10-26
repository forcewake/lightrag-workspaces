# Build the patched UI from immutable upstream source; no host Bun installation.
FROM oven/bun:1.3.3-slim AS frontend
USER root
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /build
COPY build_frontend.sh frontend.patch ./
RUN bash ./build_frontend.sh /build/webui

# The worker preservation hooks deliberately support this LightRAG release only.
FROM ghcr.io/hkuds/lightrag@sha256:5bdbd524931b011df246fe20888d110cef691e6804c12cde636a2b746d7de27e AS runtime
ARG VERSION=0.3.3
ARG REVISION=development
LABEL org.opencontainers.image.source="https://github.com/forcewake/lightrag-workspaces" \
      org.opencontainers.image.version=$VERSION \
      org.opencontainers.image.revision=$REVISION \
      org.opencontainers.image.licenses="MIT"
COPY gateway.py worker.py table_headers.py postgres_backend.py postgres_startup.py postgres_inventory.py workspace_management.py migrate_postgres.py /opt/lightrag-multiworkspace/
COPY scripts/init_workspace.py scripts/lightrag_client.py scripts/lightrag_batch.py scripts/lightrag_check.py scripts/workspace_ops.py /opt/lightrag-multiworkspace/scripts/
COPY LICENSE THIRD_PARTY_NOTICES.md /opt/lightrag-multiworkspace/
COPY --from=frontend /build/webui/ /opt/lightrag-multiworkspace/webui/
ENV PYTHONPATH=/opt/lightrag-multiworkspace:/app \
    LIGHTRAG_MULTIWORKSPACE_STATIC=/opt/lightrag-multiworkspace/webui \
    LIGHTRAG_MULTIWORKSPACE_RUNTIME=/app/data/workspace-runtime
WORKDIR /app
EXPOSE 9621
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9621/livez', timeout=5).close()"]
ENTRYPOINT ["python", "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "9621", "--workers", "1"]
CMD []
