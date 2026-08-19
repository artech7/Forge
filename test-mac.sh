#!/usr/bin/env bash
# Local test run on macOS. Generates throwaway sample videos, then starts
# the server and one worker natively so VideoToolbox is reachable.
#
#   chmod +x test-mac.sh && ./test-mac.sh
#
# Ctrl-C stops both. Nothing outside ./testmedia is touched.

set -euo pipefail
cd "$(dirname "$0")"

command -v ffmpeg >/dev/null || { echo "Install FFmpeg first: brew install ffmpeg"; exit 1; }

# Downloads land flat; fix the layout before anything else tries to use it.
NEED="server/app.py server/db.py server/scheduler.py server/watcher.py
      server/profiles.py server/schedule.py server/naming.py server/lookup.py server/static/index.html
      worker/agent.py worker/encoders.py worker/streams.py"
for f in $NEED; do
  if [ ! -f "$f" ]; then
    echo "Layout incomplete — running setup.sh first."
    ./setup.sh || exit 1
    echo
    break
  fi
done

for f in $NEED; do
  if [ ! -f "$f" ]; then
    echo "Still missing: $f"
    echo "Download that file into this folder and run setup.sh again."
    exit 1
  fi
done

MEDIA="$PWD/testmedia"
mkdir -p "$MEDIA"

# Three H.264 clips of different sizes, so the >512MB remote rule is visible.
if [ ! -f "$MEDIA/clip-a.mkv" ]; then
  echo "Generating sample clips (about a minute)…"
  for spec in "clip-a 30 1280x720" "clip-b 60 1920x1080" "clip-c 20 640x480"; do
    set -- $spec
    ffmpeg -hide_banner -loglevel error -y \
      -f lavfi -i "testsrc2=size=$3:rate=30:duration=$2" \
      -f lavfi -i "sine=frequency=440:duration=$2" \
      -c:v libx264 -preset ultrafast -b:v 8M \
      -c:a aac -shortest "$MEDIA/$1.mkv"
    echo "  $1.mkv"
  done
fi

python3 -m venv .venv 2>/dev/null || true
source .venv/bin/activate
pip install -q -r server/requirements.txt -r worker/requirements.txt

# A previous run left running will hold port 8420, and worse, its worker will
# keep encoding with whatever it detected back then — which looks like the new
# worker misbehaving. Clear both out first.
STALE_SRV=$(lsof -ti :8420 2>/dev/null || true)
STALE_WRK=$(pgrep -f 'worker/agent.py' 2>/dev/null || true)
if [ -n "$STALE_SRV$STALE_WRK" ]; then
  echo "Stopping a previous Forge that's still running…"
  [ -n "$STALE_SRV" ] && kill $STALE_SRV 2>/dev/null || true
  [ -n "$STALE_WRK" ] && kill $STALE_WRK 2>/dev/null || true
  sleep 2
  # Anything still holding the port gets a firmer nudge.
  STILL=$(lsof -ti :8420 2>/dev/null || true)
  [ -n "$STILL" ] && kill -9 $STILL 2>/dev/null || true
  sleep 1
fi

if lsof -ti :8420 >/dev/null 2>&1; then
  echo "Port 8420 is still in use by something else. Find it with:"
  echo "  lsof -i :8420"
  exit 1
fi

cleanup() { echo; echo "Stopping…"; kill 0 2>/dev/null; }
trap cleanup EXIT INT TERM

# Catch a partial update before starting anything, rather than letting it
# surface later as a stream of tracebacks.
if ! (cd server && python -c "
import sys
import app
problems = app.check_modules()
sys.exit(1 if problems else 0)" >/dev/null 2>&1); then
  echo
  echo "Server files are out of step with each other:"
  (cd server && python -c "import app" 2>&1 | grep -E "missing|older" || true)
  echo
  echo "Unzip the release over this folder, replacing existing files:"
  echo "  cd ~/Downloads && unzip -o forge.zip"
  echo
  read -r -p "Start anyway? [y/N] " answer
  case "$answer" in
    [yY]*) echo "Continuing." ;;
    *) exit 1 ;;
  esac
fi

MEDIA_ROOTS="$MEDIA" python -m uvicorn app:app \
  --app-dir server --host 127.0.0.1 --port 8420 --proxy-headers &

sleep 3

# Same machine, so the worker sees the files directly — identity mapping.
SERVER="http://127.0.0.1:8420" \
NODE_NAME="mac" \
MAX_JOBS=1 \
MOUNTS="[{\"server\":\"$MEDIA\",\"local\":\"$MEDIA\"}]" \
python worker/agent.py &

sleep 2
echo
echo "  UI:  http://127.0.0.1:8420"
echo "  Media: $MEDIA"
echo
echo "Click 'Scan library', then queue everything with:"
echo "  ./queue-all.sh"
echo

wait
