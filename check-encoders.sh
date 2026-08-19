#!/usr/bin/env bash
# Shows exactly what your FFmpeg can and can't do, with the real errors.
#
#   chmod +x check-encoders.sh && ./check-encoders.sh

cd "$(dirname "$0")"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

echo "FFmpeg: $(ffmpeg -version 2>/dev/null | head -1)"
echo

echo "=== Encoders your FFmpeg was built with ==="
ffmpeg -hide_banner -encoders 2>/dev/null \
  | grep -Ei 'videotoolbox|nvenc|qsv|amf|vaapi|libx26[45]|libsvtav1' \
  | sed 's/^/  /'
echo

# Some encoders behave differently writing to a real file than to null,
# and several reject small frames, so test both ways at a realistic size.
try() {
  local enc="$1" size="$2" out="$3" label="$4"
  local target
  [ "$out" = "null" ] && target=(-f null -) || target=("$TMP/out_${enc}.mp4")
  err=$(ffmpeg -hide_banner -loglevel error -y \
          -f lavfi -i "testsrc=size=${size}:rate=10:duration=1" \
          -pix_fmt yuv420p -c:v "$enc" -frames:v 10 "${target[@]}" 2>&1)
  if [ $? -eq 0 ]; then
    echo "  PASS  $enc  ($label)"
    return 0
  fi
  echo "  FAIL  $enc  ($label)"
  echo "$err" | tail -3 | sed 's/^/          /'
  return 1
}

echo "=== Real encode tests ==="
for enc in hevc_videotoolbox h264_videotoolbox; do
  ffmpeg -hide_banner -encoders 2>/dev/null | grep -q " $enc " || continue
  try "$enc" 1280x720  file "720p to a file"
  try "$enc" 1280x720  null "720p to null"
  try "$enc" 1920x1080 file "1080p to a file"
  try "$enc" 320x240   file "320x240 to a file"
  echo
done

echo "=== Speed check: HEVC on this machine ==="
for enc in hevc_videotoolbox libx265; do
  ffmpeg -hide_banner -encoders 2>/dev/null | grep -q " $enc " || continue
  start=$(date +%s.%N)
  if ffmpeg -hide_banner -loglevel error -y \
       -f lavfi -i "testsrc=size=1920x1080:rate=30:duration=10" \
       -pix_fmt yuv420p -c:v "$enc" "$TMP/speed_$enc.mp4" 2>/dev/null; then
    end=$(date +%s.%N)
    printf "  %-20s 300 frames of 1080p in %.1fs\n" "$enc" "$(echo "$end - $start" | bc)"
  else
    echo "  $enc  could not run"
  fi
done
echo
echo "If hevc_videotoolbox passes here but Forge didn't list it, tell me which"
echo "of the four tests above succeeded — that identifies the bad assumption."
