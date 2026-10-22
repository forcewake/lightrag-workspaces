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
COPY gateway.py worker.py /opt/lightrag-multiworkspace/
COPY scripts/init_workspace.py /opt/lightrag-multiworkspace/scripts/init_workspace.py
COPY LICENSE THIRD_PARTY_NOTICES.md /opt/lightrag-multiworkspace/
COPY --from=frontend /build/webui/ /opt/lightrag-multiworkspace/webui/
ENV PYTHONPATH=/opt/lightrag-multiworkspace:/app \
    LIGHTRAG_MULTIWORKSPACE_STATIC=/opt/lightrag-multiworkspace/webui \
    LIGHTRAG_MULTIWORKSPACE_RUNTIME=/app/data/workspace-runtime
WORKDIR /app
EXPOSE 9621
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9621/workspaces', timeout=5).close()"]
ENTRYPOINT ["python", "-m", "uvicorn", "gateway:app", "--host", "0.0.0.0", "--port", "9621", "--workers", "1"]
CMD []
