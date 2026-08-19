#!/usr/bin/env bash
# Turn this machine into a Forge worker.
#
#   ./run-node.sh http://nas.lan:8420
#
# Runs natively rather than in Docker, which matters on a Mac: Docker can't
# reach VideoToolbox, so a containerised Mac worker falls back to slow
# software encoding. On Linux with an NVIDIA or Intel GPU either works.
#
# Optional settings:
#   NODE_NAME   what to call this machine in the interface
#   MOUNTS      how this machine's paths line up with the server's
#   WORK_DIR    where to put temporary files

set -euo pipefail
cd "$(dirname "$0")"

SERVER="${1:-${SERVER:-}}"
if [ -z "$SERVER" ]; then
  echo "Usage: ./run-node.sh http://your-nas:8420"
  echo
  echo "That address is where the Forge interface is. Use the NAS's address"
  echo "or name, not localhost, unless the server is on this same machine."
  exit 1
fi

if ! command -v ffmpeg >/dev/null; then
  echo "FFmpeg isn't installed."
  echo "  macOS:  brew install ffmpeg"
  echo "  Ubuntu: sudo apt install ffmpeg"
  exit 1
fi

if ! curl -s -o /dev/null --max-time 5 "$SERVER/api/state"; then
  echo "Can't reach Forge at $SERVER"
  echo
  echo "Check that the server is running and that this machine can see it."
  echo "From here, try:  curl $SERVER/api/state"
  exit 1
fi

python3 -m venv .venv-node 2>/dev/null || true
source .venv-node/bin/activate
pip install -q -r worker/requirements.txt

# One worker per machine. A second would register as the same node and both
# would encode while the interface showed one.
LOCK="${WORK_DIR:-${TMPDIR:-/tmp}}/forge/worker.pid"
if [ -f "$LOCK" ] && kill -0 "$(cat "$LOCK" 2>/dev/null)" 2>/dev/null; then
  echo "A worker is already running on this machine (pid $(cat "$LOCK"))."
  echo "Stop it first, or delete $LOCK if it's stale."
  exit 1
fi

echo "Connecting to $SERVER as \"${NODE_NAME:-$(hostname -s)}\"…"
echo "It will measure this machine's encoders once, which takes a minute."
echo "Press Ctrl-C to stop."
echo

SERVER="$SERVER" \
NODE_NAME="${NODE_NAME:-$(hostname -s)}" \
MOUNTS="${MOUNTS:-[]}" \
MAX_JOBS="${MAX_JOBS:-1}" \
exec python worker/agent.py
