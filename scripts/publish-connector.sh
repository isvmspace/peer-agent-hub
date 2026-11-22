#!/usr/bin/env bash
# Publish connector builds to a hub: scripts/publish-connector.sh <hub-url> <version> [dir] [OPTIONAL|FORCED] [notes]
# Prompts for an admin username/password; binaries default to dist/downloads.
set -euo pipefail
HUB=${1:?hub url}; VERSION=${2:?version x.y.z}; DIR=${3:-dist/downloads}; POLICY=${4:-OPTIONAL}; NOTES=${5:-}
read -rp "管理员用户名: " USER_NAME
read -rsp "密码: " PASSWORD; echo
TOKEN=$(curl -fsS "$HUB/api/auth/login" -H 'Content-Type: application/json' \
  -d "$(python3 -c 'import json,sys;print(json.dumps({"username":sys.argv[1],"password":sys.argv[2]}))' "$USER_NAME" "$PASSWORD")" \
  | python3 -c 'import json,sys;print(json.load(sys.stdin)["token"])')
for f in "$DIR"/agent-hub-connector-*; do
  name=$(basename "$f"); plat=${name#agent-hub-connector-}; plat=${plat%.exe}; plat=${plat/-//}
  q=$(python3 -c 'import sys,urllib.parse as u;print(u.urlencode({"version":sys.argv[1],"platform":sys.argv[2],"policy":sys.argv[3],"notes":sys.argv[4]}))' "$VERSION" "$plat" "$POLICY" "$NOTES")
  printf '%-16s ' "$plat"
  curl -fsS -X POST "$HUB/api/releases?$q" -H "Authorization: Bearer $TOKEN" \
    -H 'Content-Type: application/octet-stream' --data-binary "@$f" \
    | python3 -c 'import json,sys;r=json.load(sys.stdin)["release"];print(r["version"],r["sha256"][:12])'
done
