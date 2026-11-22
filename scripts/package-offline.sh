#!/usr/bin/env bash
# Build an offline bundle (linux/amd64 wheels + connectors + runtime Dockerfile) in dist/bundle.
set -euo pipefail
cd "$(dirname "$0")/.."
VERSION=${VERSION:?set VERSION, e.g. VERSION=0.3.0}
rm -f dist/ai_agent_hub-*.whl
bash scripts/build.sh >/dev/null
rm -rf dist/bundle && mkdir -p dist/bundle/wheels
cp dist/ai_agent_hub-*.whl dist/bundle/wheels/
# resolve for linux (so linux-only markers like SecretStorage are included), then fetch wheels
UV=${UV:-uv}
"$UV" pip compile --quiet --python-platform x86_64-manylinux_2_28 --python-version 3.11 hub/pyproject.toml -o dist/bundle/requirements.txt
python3 -m pip download --quiet --dest dist/bundle/wheels --only-binary=:all: --no-deps \
  --platform manylinux2014_x86_64 --platform manylinux_2_17_x86_64 --platform manylinux_2_28_x86_64 \
  --python-version 3.11 --implementation cp -r dist/bundle/requirements.txt
cp -R dist/downloads dist/bundle/downloads
cp deploy/Dockerfile.runtime dist/bundle/Dockerfile
COPYFILE_DISABLE=1 tar -C dist -czf dist/agent-hub-bundle-"$VERSION".tgz bundle
ls -la dist/agent-hub-bundle-"$VERSION".tgz
