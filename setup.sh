#!/usr/bin/env bash
# Downloads arrive flat. This puts everything back where it belongs.
# Safe to run more than once.

set -euo pipefail
cd "$(dirname "$0")"

mkdir -p server/static worker

move() {  # move $1 to $2 if it's still sitting in the current directory
  local src="$1" dst="$2"
  if [ -f "$src" ]; then
    mv "$src" "$dst"
    echo "  $src -> $dst"
  elif [ -f "$dst" ]; then
    :  # already in place
  else
    echo "  MISSING: $src"
    MISSING=1
  fi
}

MISSING=0
echo "Arranging files…"
move app.py           server/app.py
move db.py            server/db.py
move scheduler.py     server/scheduler.py
move watcher.py       server/watcher.py
move schedule.py      server/schedule.py
move profiles.py      server/profiles.py
move naming.py        server/naming.py
move lookup.py        server/lookup.py
move agent.py         worker/agent.py
move encoders.py      worker/encoders.py
move streams.py       worker/streams.py

# The UI may have downloaded as index or index.html.
if [ -f index.html ]; then
  mv index.html server/static/index.html
  echo "  index.html -> server/static/index.html"
elif [ -f index ]; then
  mv index server/static/index.html
  echo "  index -> server/static/index.html"
elif [ ! -f server/static/index.html ]; then
  echo "  MISSING: index.html"
  MISSING=1
fi

# Rewrite these rather than guess which duplicate landed where.
cat > server/requirements.txt <<'EOF'
fastapi==0.115.6
uvicorn[standard]==0.34.0
python-multipart==0.0.20
EOF

cat > worker/requirements.txt <<'EOF'
requests==2.32.3
EOF

# Any stray copies from the flat download.
rm -f requirements.txt "requirements 2.txt" "requirements(1).txt" 2>/dev/null || true

echo
# Anything left at the root is a file this script doesn't know about —
# most likely a newer module added after this copy of setup.sh.
STRAY=$(find . -maxdepth 1 -name '*.py' -not -name 'setup.py' 2>/dev/null)
if [ -n "$STRAY" ]; then
  echo "These files weren't recognised and are still in the root:"
  for f in $STRAY; do echo "  ${f#./}"; done
  echo "If they're server modules, move them yourself:"
  echo "  mv *.py server/"
  echo
fi

if [ "$MISSING" = "1" ]; then
  echo "Some files are missing — download them and run this again."
  exit 1
fi

echo "Layout is correct."
echo "  Server + a local worker:  ./test-mac.sh"
echo "  This machine as a node:   ./run-node.sh http://your-nas:8420"
