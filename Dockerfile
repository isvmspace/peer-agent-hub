FROM node:22-slim AS web
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY frontend ./
RUN npm run build

FROM golang:1.27 AS connector
WORKDIR /build/connector
COPY connector/go.mod connector/go.sum ./
RUN go mod download
COPY connector ./
ARG VERSION=0.9.14
RUN for t in windows/amd64 darwin/arm64 darwin/amd64 linux/amd64; do \
      os=${t%/*}; arch=${t#*/}; ext=""; [ "$os" = windows ] && ext=.exe; \
      CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -ldflags "-s -w -X main.version=${VERSION}" \
        -o /out/agent-hub-connector-$os-$arch$ext . ; \
    done && printf '{"version": "%s"}\n' "$VERSION" > /out/manifest.json

FROM python:3.11-slim AS wheel
WORKDIR /build/hub
COPY hub ./
COPY --from=web /build/hub/src/agent_hub/web ./src/agent_hub/web
RUN pip install --no-cache-dir build && python -m build --wheel --outdir /dist

FROM python:3.11-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    AGENT_HUB_DATA_DIR=/data \
    AGENT_HUB_DOWNLOAD_DIR=/app/downloads
COPY --from=wheel /dist/*.whl /tmp/
RUN pip install --no-cache-dir /tmp/*.whl && rm -f /tmp/*.whl && useradd --create-home --uid 10001 hub \
    && mkdir -p /data && chown hub /data
COPY --from=connector /out /app/downloads
USER hub
VOLUME ["/data"]
EXPOSE 8790
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8790/healthz', timeout=3)"
CMD ["agent-hub", "--host", "0.0.0.0", "--port", "8790"]
