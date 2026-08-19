#!/usr/bin/env bash
# Queue every scanned test file for HEVC conversion.
set -euo pipefail
cd "$(dirname "$0")"
PATHS=$(curl -s http://127.0.0.1:8420/api/library | python3 -c '
import json,sys
print(json.dumps([f["path"] for f in json.load(sys.stdin)]))')
curl -s -X POST http://127.0.0.1:8420/api/queue \
  -H "Content-Type: application/json" \
  -d "{\"paths\":$PATHS,\"spec\":{\"codec\":\"hevc\",\"quality\":22,\"audio\":\"copy\",\"container\":\"mkv\"}}"
echo
