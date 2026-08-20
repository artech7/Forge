#!/usr/bin/env python3
"""Work out why one file won't convert.

    python3 check-media.py "/path/to/the/file.mkv"

Reads the file's streams, then actually tries to decode each one. A track
that fails here is the reason the conversion failed, and the answer is
usually to leave that track alone or drop it.

Run it on the machine that has the file — the worker, not the server, if
they're different.
"""
import json
import subprocess
import sys
from pathlib import Path


def run(args, timeout=300):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout)
    except FileNotFoundError:
        print("FFmpeg isn't installed, or isn't on the PATH.")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        return None


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json",
               "-show_streams", "-show_format", str(path)])
    if not out or out.returncode != 0:
        print("ffprobe couldn't read this file at all.")
        if out and out.stderr.strip():
            for line in out.stderr.strip().splitlines()[:5]:
                print("  " + line)
        print()
        print("That normally means the file is damaged or truncated.")
        return None
    return json.loads(out.stdout)


def decode_test(path, specifier, label):
    """Decode one stream to nothing and see whether it survives."""
    out = run(["ffmpeg", "-hide_banner", "-v", "error", "-nostdin",
               "-i", str(path), "-map", specifier, "-f", "null", "-"])
    if out is None:
        print(f"  TIMED OUT  {label}")
        return False
    if out.returncode == 0 and not out.stderr.strip():
        print(f"  ok         {label}")
        return True
    print(f"  FAILED     {label}")
    for line in (out.stderr or "").strip().splitlines()[:3]:
        print(f"               {line}")
    return False


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    path = Path(sys.argv[1]).expanduser()
    if not path.is_file():
        print(f"No such file: {path}")
        return 1

    print(f"File: {path.name}")
    size = path.stat().st_size
    print(f"Size: {size / 1e9:.2f} GB" if size >= 1e9 else
          f"Size: {size / 1e6:.0f} MB")
    print()

    info = probe(path)
    if not info:
        return 1

    streams = info.get("streams", [])
    print("Streams:")
    for stream in streams:
        kind = stream.get("codec_type", "?")
        tags = stream.get("tags") or {}
        bits = [stream.get("codec_name", "?")]
        if kind == "video":
            bits.append(f"{stream.get('width')}x{stream.get('height')}")
            bits.append(stream.get("pix_fmt", ""))
        if kind == "audio":
            bits.append(f"{stream.get('channels', '?')}ch")
            bits.append(stream.get("channel_layout", ""))
        if tags.get("language"):
            bits.append(tags["language"])
        if tags.get("title"):
            bits.append(f'"{tags["title"]}"')
        print(f"  {stream.get('index'):>2}  {kind:9} "
              + " ".join(b for b in bits if b))
    print()

    # Decoding is where a damaged track shows itself. ffprobe reads headers
    # and is happy; the conversion reads every frame and is not.
    print("Decoding each stream (this takes as long as reading the file):")
    bad = []
    for stream in streams:
        index = stream.get("index")
        kind = stream.get("codec_type", "?")
        if kind not in ("video", "audio", "subtitle"):
            continue
        if not decode_test(path, f"0:{index}",
                           f"stream {index} ({kind}, {stream.get('codec_name')})"):
            bad.append((index, kind, stream.get("codec_name")))

    print()
    if not bad:
        print("Every stream decodes cleanly.")
        print()
        print("So the file itself is fine, and the failure is in what Forge")
        print("asked FFmpeg to do with it. Worth checking the library's audio")
        print("codec and container against what this file contains.")
        return 0

    print(f"{len(bad)} stream(s) failed to decode:")
    for index, kind, codec in bad:
        print(f"  stream {index} — {kind}, {codec}")
    print()
    print("That's the cause. Options, roughly in order of preference:")
    print()
    if any(kind == "audio" for _i, kind, _c in bad):
        print("  - Set this library's audio to \"Leave audio alone\". The bad")
        print("    track is copied rather than decoded, which usually works.")
        print("  - Or restrict audio to the languages you want, if the damaged")
        print("    track is one you don't need.")
    if any(kind == "subtitle" for _i, kind, _c in bad):
        print("  - Set subtitles to \"Remove all\" for this file, or keep only")
        print("    the languages you want.")
    if any(kind == "video" for _i, kind, _c in bad):
        print("  - The picture itself is damaged. Nothing Forge does will fix")
        print("    that; the file needs replacing.")
    print("  - Or add a skip rule so Forge leaves this file alone.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
