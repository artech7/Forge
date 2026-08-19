#!/usr/bin/env bash
# Checks that a running Forge server is complete and self-consistent.
# Run while Forge is up:  ./verify.sh
B="${1:-http://127.0.0.1:8420}"

echo "Checking $B"
echo

if ! curl -s -o /dev/null --max-time 5 "$B/api/state"; then
  echo "Forge isn't responding. Is it running?"
  exit 1
fi

# The server reports its own mismatches, which catches files that are
# present but out of date — the case that shows up as a 500, not a 404.
HEALTH=$(curl -s --max-time 5 "$B/api/health")
if [ -n "$HEALTH" ] && echo "$HEALTH" | grep -q '"problems"'; then
  echo "$HEALTH" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
except Exception:
    sys.exit(0)
problems = d.get('problems') or []
if problems:
    print('Server files are out of step with each other:')
    for p in problems:
        print('   ', p)
else:
    print('Server modules are consistent.')
print()
"
else
  echo "This server predates the self-check. Update the server files."
  echo
fi

node=$(curl -s --max-time 5 "$B/api/state" | python3 -c "
import json,sys
try: n = json.load(sys.stdin).get('nodes') or []
except Exception: n = []
print(n[0]['id'] if n else '')" 2>/dev/null)

STALE=0
check() {
  code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -X "$1" "$B$2" \
    -H 'Content-Type: application/json' --data "${3:-\{\}}")
  case "$code" in
    404) echo "  MISSING  $2   <- endpoint not in this server"; STALE=1 ;;
    5*)  echo "  BROKEN   $2   <- HTTP $code, a module is out of date"; STALE=1 ;;
    *)   echo "  ok       $2" ;;
  esac
}

echo "Endpoints the interface uses:"
check GET  /api/catalog
check POST /api/naming/preview '{"names":["x.mkv"]}'
check POST /api/lookup/test    '{"key":""}'
if [ -n "$node" ]; then
  check POST "/api/nodes/$node/slots" '{"slots":1}'
else
  echo "  (no node registered, skipping the slots check)"
fi

echo
if [ "$STALE" = "1" ]; then
  echo "Something is out of date. Unzip the release over this folder and"
  echo "restart Forge — the browser has newer code than the server."
else
  echo "Everything checks out."
fi
