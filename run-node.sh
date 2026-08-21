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

# The script sits in the Forge folder and works from there. Run from
# somewhere else and nothing it needs is alongside it.
if [ ! -f worker/agent.py ]; then
  echo "This needs to run from inside the Forge folder."
  echo
  echo "It's looking in:  $(pwd)"
  echo "and can't find:   worker/agent.py"
  echo
  if [ -d "$HOME/Downloads/forge/worker" ]; then
    echo "Found Forge at $HOME/Downloads/forge — try:"
    echo
    echo "  cd ~/Downloads/forge"
    echo "  ./run-node.sh $*"
  else
    echo "Unzip the release somewhere, then run it from that folder:"
    echo
    echo "  cd /path/to/forge"
    echo "  ./run-node.sh $*"
  fi
  exit 1
fi

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

# A bare address is meant as http, and a Forge server on the network speaks
# plain http — only the reverse proxy adds TLS.
case "$SERVER" in
  http://*|https://*) ;;
  *) SERVER="http://$SERVER" ;;
esac
SERVER="${SERVER%/}"

# TLS on a private address is nearly always a slip: the certificate lives on
# the public name, not on 192.168.x.x.
case "$SERVER" in
  https://10.*|https://192.168.*|https://172.1[6-9].*|https://172.2[0-9].*|\
  https://172.3[01].*|https://localhost*|https://127.*)
    PLAIN="http://${SERVER#https://}"
    echo "That address is on your own network but written as https."
    echo "Forge itself speaks plain http; only the proxy adds TLS."
    echo "Trying $PLAIN instead."
    echo
    SERVER="$PLAIN"
    ;;
esac

# Going through a reverse proxy works for the interface but not well for a
# worker: without a share mapping it uploads whole video files, and proxies
# routinely cap request bodies and time out long uploads.
case "$SERVER" in
  https://*|*.synology.me*|*.duckdns.org*|*.ddns.net*)
    echo "Note: that looks like a public or proxied address."
    echo
    echo "Workers are better pointed straight at the NAS on your network,"
    echo "for example:  ./run-node.sh http://192.168.1.50:58420"
    echo
    echo "Through a proxy the interface is fine, but file transfers can be"
    echo "cut off by request size limits and timeouts."
    if [ -z "${MOUNTS:-}" ] || [ "${MOUNTS:-[]}" = "[]" ]; then
      echo
      echo "You also haven't set MOUNTS, so every file would be copied"
      echo "across the network rather than read from the share directly."
    fi
    echo
    printf "Carry on anyway? [y/N] "
    read -r answer
    case "$answer" in
      [yY]*) echo ;;
      *) exit 1 ;;
    esac
    ;;
esac

if ! curl -s -o /dev/null --max-time 5 "$SERVER/api/state"; then
  # Usually the wrong scheme, so try the other one before giving up.
  case "$SERVER" in
    https://*) OTHER="http://${SERVER#https://}" ;;
    *)         OTHER="https://${SERVER#http://}" ;;
  esac
  if curl -s -o /dev/null --max-time 5 "$OTHER/api/state"; then
    echo "$SERVER didn't answer, but $OTHER does. Using that."
    echo
    SERVER="$OTHER"
  else
    echo "Can't reach Forge at $SERVER"
    echo
    echo "Neither http nor https answered on that address."
    echo
    echo "Things to check:"
    echo "  - Is the Forge container running on the NAS?"
    echo "  - Is the port published? The stack maps 58420 to the"
    echo "    container's 8420."
    echo "  - Ping working but the port not usually means a firewall on the"
    echo "    NAS, or the container bound to a different address."
    echo
    echo "From here, try:  curl $SERVER/api/state"
    exit 1
  fi
fi

# MOUNTS may be given as a plain path, which is what people actually type.
# Full JSON still works for unusual setups.
SERVER_PATH="${SERVER_PATH:-/media}"
case "${MOUNTS:-}" in
  ""|"[]") MOUNTS="[]" ;;
  \[*) : ;;                                   # already an array
  \{*) MOUNTS="[$MOUNTS]" ;;                   # a single object
  *=*) MOUNTS="[{\"server\":\"${MOUNTS%%=*}\",\"local\":\"${MOUNTS#*=}\"}]" ;;
  *)   MOUNTS="[{\"server\":\"$SERVER_PATH\",\"local\":\"${MOUNTS%/}\"}]" ;;
esac

if [ "$MOUNTS" != "[]" ]; then
  LOCAL_PATH=$(printf '%s' "$MOUNTS" | sed -n 's/.*"local":"\([^"]*\)".*/\1/p')
  if [ -n "$LOCAL_PATH" ] && [ ! -d "$LOCAL_PATH" ]; then
    echo "Warning: $LOCAL_PATH doesn't exist on this machine."
    echo "Check the share is mounted, or Forge won't be able to open the"
    echo "files it is sent."
    echo
  fi
fi

# Checked rather than assumed: a failed venv used to sail past and then
# error on a missing activate script, which says nothing useful.
if [ ! -x .venv-node/bin/python ]; then
  echo "Setting up (once)…"
  if ! python3 -m venv .venv-node 2>/tmp/forge-venv.log; then
    echo
    echo "Couldn't create the Python environment."
    sed 's/^/  /' /tmp/forge-venv.log
    echo
    echo "On Debian or Ubuntu this is usually a missing package:"
    echo "  sudo apt install python3-venv"
    exit 1
  fi
fi

if ! .venv-node/bin/pip install -q -r worker/requirements.txt; then
  echo "Couldn't install what the worker needs. See the messages above."
  exit 1
fi

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
exec .venv-node/bin/python worker/agent.py
