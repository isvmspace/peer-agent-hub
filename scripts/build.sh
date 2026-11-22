#!/usr/bin/env bash
# Local build without Docker: console -> hub/src/agent_hub/web, connectors -> dist/downloads, wheel -> dist/
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${VERSION:-0.9.15}
(cd frontend && npm ci --no-audit --no-fund && npm run build)
mkdir -p dist/downloads
(cd connector && for t in windows/amd64 darwin/arm64 darwin/amd64 linux/amd64; do
  os=${t%/*}; arch=${t#*/}; ext=""; [ "$os" = windows ] && ext=.exe
  CGO_ENABLED=0 GOOS=$os GOARCH=$arch go build -ldflags "-s -w -X main.version=$VERSION" -o ../dist/downloads/agent-hub-connector-$os-$arch$ext .
done)
# macOS keeps a connector's privacy permissions (Full Disk Access, keychain) only while its signing identity stays
# the same, so every release is signed with the same certificate (docs/connector-signing.md)
SIGN_IDENTITY=${SIGN_IDENTITY:-Agent Hub Connector Signing}
if command -v codesign >/dev/null && security find-identity -p codesigning | grep -q "\"$SIGN_IDENTITY\""; then
  for f in dist/downloads/agent-hub-connector-darwin-*; do
    codesign -f -s "$SIGN_IDENTITY" -i dev.agent-hub.connector "$f"
  done
else
  echo "警告：没有签名证书「$SIGN_IDENTITY」，macOS 连接器未签名，升级后用户要重新授权" >&2
fi
printf '{"version": "%s"}\n' "$VERSION" > dist/downloads/manifest.json
python3 -m pip wheel --no-deps -w dist ./hub
ls -la dist dist/downloads
