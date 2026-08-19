#!/usr/bin/env bash
# Narrow down exactly which argument hevc_videotoolbox is rejecting.
# Each line is one hypothesis. Whichever passes tells us the answer.

TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
IN=(-f lavfi -i "testsrc=size=1280x720:rate=10:duration=1")

t() {
  local label="$1"; shift
  if err=$(ffmpeg -hide_banner -loglevel error -y "${IN[@]}" "$@" \
             -frames:v 10 "$TMP/o.mp4" 2>&1); then
    echo "  PASS  $label"
  else
    echo "  FAIL  $label"
    echo "$err" | grep -Ei 'error|invalid|unsupported' | head -1 | sed 's/^/          /'
  fi
}

echo "=== isolating hevc_videotoolbox ==="
t "yuv420p, no rate control"      -pix_fmt yuv420p -c:v hevc_videotoolbox
t "no pix_fmt at all"                              -c:v hevc_videotoolbox
t "nv12"                          -pix_fmt nv12    -c:v hevc_videotoolbox
t "p010le (10-bit)"               -pix_fmt p010le  -c:v hevc_videotoolbox
t "explicit bitrate"              -pix_fmt yuv420p -c:v hevc_videotoolbox -b:v 6M
t "constant quality -q:v 60"      -pix_fmt yuv420p -c:v hevc_videotoolbox -q:v 60
t "allow_sw 1"                    -pix_fmt yuv420p -c:v hevc_videotoolbox -allow_sw 1
t "allow_sw 1 + bitrate"          -pix_fmt yuv420p -c:v hevc_videotoolbox -allow_sw 1 -b:v 6M
t "profile main + bitrate"        -pix_fmt yuv420p -c:v hevc_videotoolbox -profile:v main -b:v 6M
t "realtime 0 + bitrate"          -pix_fmt yuv420p -c:v hevc_videotoolbox -realtime 0 -b:v 6M
t "tag hvc1 + bitrate"            -pix_fmt yuv420p -c:v hevc_videotoolbox -tag:v hvc1 -b:v 6M
t "even framerate 30"             -pix_fmt yuv420p -c:v hevc_videotoolbox -r 30 -b:v 6M

echo
echo "=== control: same tests on h264_videotoolbox ==="
t "h264 baseline"                 -pix_fmt yuv420p -c:v h264_videotoolbox
