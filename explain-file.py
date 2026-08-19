#!/usr/bin/env python3
"""Explain why Forge did or didn't pick up a particular file.

    python3 explain-file.py "/path/to/the/file.mp4"

Walks the same decisions the scanner makes, in order, and prints what each
one concluded. Reads the live database, so it can be run while Forge is up.
"""
import os
import pathlib
import sys
import time

HERE = pathlib.Path(__file__).parent
sys.path.insert(0, str(HERE / "server"))

import db          # noqa: E402

# Lets the checks point at a scratch database instead of the live one.
if os.environ.get("FORGE_DB"):
    db.DB_PATH = pathlib.Path(os.environ["FORGE_DB"])
import naming      # noqa: E402
import profiles    # noqa: E402
import watcher     # noqa: E402


def human(n):
    if not n:
        return "unknown"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 1
    target = pathlib.Path(sys.argv[1]).expanduser().resolve()
    print(f"File: {target.name}")
    print(f"In:   {target.parent}")
    print()

    if not target.exists():
        print("This file does not exist at that path.")
        return 1

    # ---- which library, if any, covers it ------------------------------
    libraries = db.list_libraries()
    if not libraries:
        print("No libraries are configured, so nothing is being watched.")
        print(f"(reading {db.DB_PATH})")
        return 1

    owner = None
    for lib in libraries:
        root = pathlib.Path(lib["watch_path"]).expanduser().resolve()
        if root == target.parent or root in target.parents:
            owner = lib
            break

    if not owner:
        print("No library watches this folder. Watch folders configured:")
        for lib in libraries:
            print(f"   {lib['name']}: {lib['watch_path']}")
        return 1

    print(f"Library: {owner['name']}  ({'watching' if owner['enabled'] else 'PAUSED'})")
    if not owner["enabled"]:
        print("   -> This library is paused, so nothing in it is scanned.")
        return 0

    spec = profiles.resolve(owner["profile"])
    print(f"   wants: {spec['codec']} video, {spec['audio']} audio, "
          f".{spec['container']}, quality {spec['quality']}")
    print()

    # ---- is it even considered a video --------------------------------
    print("1. Recognised as video")
    if target.suffix.lower() not in watcher.VIDEO_EXT:
        print(f"   NO - '{target.suffix}' isn't in the list of video types.")
        return 0
    if target.name.startswith("."):
        print("   NO - names starting with a dot are ignored.")
        return 0
    skipped_dir = [p for p in target.parts if p.lower() in watcher.SKIP_DIRS]
    if skipped_dir:
        print(f"   NO - it sits inside '{skipped_dir[0]}', which is skipped.")
        return 0
    print("   yes")

    stat = target.stat()
    age = time.time() - stat.st_mtime
    print()
    print("2. Finished copying")
    print(f"   size {human(stat.st_size)}, last changed "
          f"{age / 60:.1f} minutes ago")
    if age <= watcher.SETTLED_AGE:
        print(f"   NOT YET - files are held until unchanged for "
              f"{watcher.SETTLED_AGE}s, in case they're still being copied.")
        print("   This resolves itself; try again shortly.")
        return 0
    print("   yes")

    print()
    print("3. Already handled before")
    if db.was_processed(str(target), stat.st_mtime):
        print("   YES - this exact file was already dealt with, so it is")
        print("   skipped. That happens after it was converted, filtered out,")
        print("   or found to need nothing. Touching the file (or editing the")
        print("   library, which rescans) will make Forge look again.")
        return 0
    print("   no, it's new to Forge")

    # ---- skip rules ---------------------------------------------------
    filters = owner.get("filters") or {}
    print()
    print("4. Skip rules")
    reason = watcher.filter_verdict(target, stat.st_size, None, filters)
    if reason:
        print(f"   SKIPPED - {reason}")
        return 0
    info = None
    try:
        import app
        info = app.probe(str(target))
    except Exception as exc:
        print(f"   (could not inspect the file: {exc})")
    reason = watcher.filter_verdict(target, stat.st_size, info, filters)
    if reason:
        print(f"   SKIPPED - {reason}")
        return 0
    print("   passes every skip rule")

    if not info:
        print()
        print("Could not read the file's streams. FFmpeg may not recognise it.")
        return 0

    print()
    print("5. What's in the file")
    print(f"   video {info.get('video_codec')} "
          f"{info.get('width')}x{info.get('height')} at "
          f"{(info.get('video_bitrate') or 0) / 1000:.0f} kbps")
    print(f"   audio {', '.join(info.get('audio_codecs') or []) or 'none'}")
    print(f"   container .{target.suffix.lstrip('.')}")

    print()
    print("6. Speed")
    depth = int(info.get("bit_depth") or 8)
    hi10p = info.get("video_codec") == "h264" and depth > 8
    slow_here = []
    for node in db.list_nodes():
        eight = node.get("benchmarks") or {}
        ten = node.get("benchmarks_10bit") or {}
        for enc, fast in eight.items():
            slow = ten.get(enc)
            if slow and fast and slow < fast * 0.4:
                slow_here.append((node["name"], enc, fast, slow))

    if depth > 8:
        print(f"   This file is {depth}-bit ({info.get('pix_fmt')}).")
        if hi10p:
            print("   10-bit H.264 can't be decoded by any consumer graphics")
            print("   hardware, so decoding runs on the processor.")
        if slow_here:
            for name, enc, fast, slow in slow_here:
                print(f"   {name}: {enc} does {fast}fps at 8-bit but only "
                      f"{slow}fps at 10-bit.")
            print("   That encoder gives up on 10-bit and uses software.")
            print("   Setting this library's colour depth to 8-bit would keep")
            print("   it on the hardware encoder and be far faster.")
        elif not hi10p:
            print("   Nothing obviously slow about it.")
    else:
        print(f"   {depth}-bit source, nothing unusual expected.")

    print()
    print("7. What needs doing")
    action, adjusted, why = watcher.plan_conversion(target, info, spec, filters)
    print(f"   {action.upper()}: {why}")

    if action == "skip":
        print()
        if owner["skip_matching"]:
            print("   Nothing needs converting - the file already matches")
            print("   everything this library asks for. Converting it would")
            print("   only cost quality, so Forge won't queue it.")
            if owner.get("output_path"):
                dest = watcher.destination_for(
                    owner, str(target), target.suffix.lstrip("."))
                print()
                print("   It will still be renamed and moved to:")
                print(f"   {dest}")
                print()
                print("   If it hasn't moved yet, press 'Check now' on the")
                print("   library. Files needing no conversion are moved by the")
                print("   scanner rather than going through the queue.")
            else:
                print()
                print("   This library has no destination folder, so the file")
                print("   stays where it is. That's expected.")
        else:
            print("   'Skip files already in the right format' is off for this")
            print("   library, so it would be converted anyway.")
        return 0

    print()
    print("8. Where it would go")
    if owner.get("output_path"):
        print("   " + str(watcher.destination_for(owner, str(target), spec["container"])))
    else:
        print("   converted in place (no destination set)")
    print()
    print("Nothing is stopping this file. If it still isn't queued, the")
    print("scanner may not have run yet - press 'Check now' on the library.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
