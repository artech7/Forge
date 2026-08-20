#!/usr/bin/env bash
# Build a realistic test library from one real video file.
#
#   ./make-test-files.sh ~/Downloads/bbb_sunflower_1080p_30fps.mkv
#
# Produces one file per decision Forge can make, so you can watch it
# classify each correctly instead of guessing. Uses 30-second clips and
# fast presets — the whole thing takes a couple of minutes.

set -euo pipefail
cd "$(dirname "$0")"

SRC="${1:-}"
if [ -z "$SRC" ] || [ ! -f "$SRC" ]; then
  echo "Usage: ./make-test-files.sh /path/to/a/video.mkv"
  echo
  echo "Get one from https://test-videos.co.uk/bigbuckbunny/mkv"
  exit 1
fi

OUT="$PWD/testlib"
rm -rf "$OUT"
mkdir -p "$OUT/Action" "$OUT/Comedy"

ff() { ffmpeg -hide_banner -loglevel error -y -i "$SRC" -t 30 "$@"; }

echo "Building test library from $(basename "$SRC")…"

# 1. Already H.265 but with AC3 audio — the case you download constantly.
#    Expect: AUDIO ONLY. Video copied untouched, finishes in seconds.
echo "  [audio only]  Needs Audio Fix (2008) x265.mkv"
ff -c:v libx265 -preset ultrafast -x265-params log-level=none -crf 28 \
   -c:a ac3 -b:a 448k "$OUT/Action/Needs Audio Fix (2008) x265.mkv"

# 2. Already H.265 with AAC at a sane bitrate.
#    Expect: SKIPPED. Nothing to do.
echo "  [skip]        Already Perfect (2010) x265.mkv"
ff -c:v libx265 -preset ultrafast -x265-params log-level=none -crf 28 \
   -c:a aac -b:a 160k "$OUT/Action/Already Perfect (2010) x265.mkv"

# 3. Right codecs, wrong container.
#    Expect: REPACKAGE. No re-encoding at all.
echo "  [repackage]   Wrong Box (2012).mp4"
ff -c:v libx265 -preset ultrafast -x265-params log-level=none -crf 28 \
   -tag:v hvc1 -c:a aac -b:a 160k "$OUT/Comedy/Wrong Box (2012).mp4"

# 4. H.265 but at a wasteful bitrate.
#    Expect: VIDEO ONLY. Proves the bitrate ceiling catches bloat that a
#    plain codec filter would wave through.
echo "  [video only]  Bloated Remux (2015) x265.mkv"
ff -c:v libx265 -preset ultrafast -x265-params log-level=none -b:v 25M \
   -c:a aac -b:a 160k "$OUT/Action/Bloated Remux (2015) x265.mkv"

# 5. Old H.264 encode with MP3 audio.
#    Expect: FULL CONVERT.
echo "  [full]        Old Encode (2005).mkv"
ff -c:v libx264 -preset ultrafast -crf 23 \
   -c:a libmp3lame -b:a 192k "$OUT/Comedy/Old Encode (2005).mkv"

# 6. Something with 'sample' in the name.
#    Expect: FILTERED OUT, if you set that skip rule in the wizard.
echo "  [filtered]    Old Encode (2005)-sample.mkv"
ffmpeg -hide_banner -loglevel error -y -i "$SRC" -t 4 \
   -c:v libx264 -preset ultrafast -crf 30 -c:a aac -b:a 96k \
   "$OUT/Comedy/Old Encode (2005)-sample.mkv"

echo
echo "Done. Point a library's watch folder at:"
echo "  $OUT"
echo
echo "Suggested settings to see everything work:"
echo "  Video    H.265, Balanced      Audio  AAC at 160k"
echo "  Skip     names containing 'sample'"
echo "  Leave 'ignore files already in these formats' BLANK"
echo "  Keep the bitrate ceiling ON"
echo
ffprobe -v error -show_entries stream=codec_name,codec_type \
  -of csv=p=0 "$OUT/Action/Needs Audio Fix (2008) x265.mkv" \
  | paste -sd' ' - | sed 's/^/  sanity check: /'
