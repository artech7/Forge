"""Walks the same decisions the scanner makes for one file, in order.

Shared by explain-file.py (the CLI) and the in-app "why isn't this
queued?" tool, so the two can never drift out of sync with each other
the way explain-file.py's own was_processed() call once did.
"""
import time
from pathlib import Path

import db
import profiles
import watcher


def human(n):
    if not n:
        return "unknown"
    return f"{n / 1e9:.2f} GB" if n >= 1e9 else f"{n / 1e6:.0f} MB"


def walk(target: Path, probe_fn):
    """Return the explanation as a list of plain-text lines."""
    out = []

    def p(line=""):
        out.append(line)

    p(f"File: {target.name}")
    p(f"In:   {target.parent}")
    p()

    if not target.exists():
        p("This file does not exist at that path.")
        return out

    # ---- which library, if any, covers it ------------------------------
    libraries = db.list_libraries()
    if not libraries:
        p("No libraries are configured, so nothing is being watched.")
        return out

    owner = None
    for lib in libraries:
        root = Path(lib["watch_path"]).expanduser().resolve()
        if root == target.parent or root in target.parents:
            owner = lib
            break

    if not owner:
        p("No library watches this folder. Watch folders configured:")
        for lib in libraries:
            p(f"   {lib['name']}: {lib['watch_path']}")
        return out

    p(f"Library: {owner['name']}  ({'watching' if owner['enabled'] else 'PAUSED'})")
    if not owner["enabled"]:
        p("   -> This library is paused, so nothing in it is scanned.")
        return out

    spec = profiles.resolve(owner["profile"])
    p(f"   wants: {spec['codec']} video, {spec['audio']} audio, "
      f".{spec['container']}, quality {spec['quality']}")
    p()

    # ---- is it even considered a video --------------------------------
    p("1. Recognised as video")
    if target.suffix.lower() not in watcher.VIDEO_EXT:
        p(f"   NO - '{target.suffix}' isn't in the list of video types.")
        return out
    if target.name.startswith("."):
        p("   NO - names starting with a dot are ignored.")
        return out
    skipped_dir = [part for part in target.parts if part.lower() in watcher.SKIP_DIRS]
    if skipped_dir:
        p(f"   NO - it sits inside '{skipped_dir[0]}', which is skipped.")
        return out
    p("   yes")

    stat = target.stat()
    age = time.time() - stat.st_mtime
    p()
    p("2. Finished copying")
    p(f"   size {human(stat.st_size)}, last changed "
      f"{age / 60:.1f} minutes ago")
    if age <= watcher.SETTLED_AGE:
        p(f"   NOT YET - files are held until unchanged for "
          f"{watcher.SETTLED_AGE}s, in case they're still being copied.")
        p("   This resolves itself; try again shortly.")
        return out
    p("   yes")

    p()
    p("3. Already handled before")
    if db.was_processed(str(target), stat.st_mtime, stat.st_size):
        p("   YES - this exact file was already dealt with, so it is")
        p("   skipped. That happens after it was converted, filtered out,")
        p("   or found to need nothing. Touching the file (or editing the")
        p("   library, which rescans) will make Forge look again.")
        return out
    p("   no, it's new to Forge")

    p()
    p("4. A past attempt still sitting unresolved")
    stuck = db.unresolved_job_for(str(target))
    if stuck and stuck.get("size_before") == stat.st_size:
        p(f"   YES - job #{stuck['id']} against this exact file (same size) is")
        p(f"   sitting in {stuck['state'].capitalize()}, waiting for a person to")
        p("   look at it. The scanner leaves it alone on purpose rather than")
        p("   re-trying the same failure every cycle. Retry or remove it from")
        p(f"   the {stuck['state'].capitalize()} list to make Forge look again.")
        return out
    p("   no")

    # ---- skip rules ---------------------------------------------------
    filters = owner.get("filters") or {}
    p()
    p("5. Skip rules")
    reason = watcher.filter_verdict(target, stat.st_size, None, filters)
    if reason:
        p(f"   SKIPPED - {reason}")
        return out
    info = None
    try:
        info = probe_fn(str(target))
    except Exception as exc:
        p(f"   (could not inspect the file: {exc})")
    reason = watcher.filter_verdict(target, stat.st_size, info, filters)
    if reason:
        p(f"   SKIPPED - {reason}")
        return out
    p("   passes every skip rule")

    if not info:
        p()
        p("Could not read the file's streams. FFmpeg may not recognise it.")
        return out

    p()
    p("6. What's in the file")
    p(f"   video {info.get('video_codec')} "
      f"{info.get('width')}x{info.get('height')} at "
      f"{(info.get('video_bitrate') or 0) / 1000:.0f} kbps")
    p(f"   audio {', '.join(info.get('audio_codecs') or []) or 'none'}")
    p(f"   container .{target.suffix.lstrip('.')}")

    p()
    p("7. Speed")
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
        p(f"   This file is {depth}-bit ({info.get('pix_fmt')}).")
        if hi10p:
            p("   10-bit H.264 can't be decoded by any consumer graphics")
            p("   hardware, so decoding runs on the processor.")
        if slow_here:
            for name, enc, fast, slow in slow_here:
                p(f"   {name}: {enc} does {fast}fps at 8-bit but only "
                  f"{slow}fps at 10-bit.")
            p("   That encoder gives up on 10-bit and uses software.")
            p("   Setting this library's colour depth to 8-bit would keep")
            p("   it on the hardware encoder and be far faster.")
        elif not hi10p:
            p("   Nothing obviously slow about it.")
    else:
        p(f"   {depth}-bit source, nothing unusual expected.")

    p()
    p("8. What needs doing")
    action, adjusted, why = watcher.plan_conversion(target, info, spec, filters)
    p(f"   {action.upper()}: {why}")

    if action == "skip":
        p()
        if owner["skip_matching"]:
            p("   Nothing needs converting - the file already matches")
            p("   everything this library asks for. Converting it would")
            p("   only cost quality, so Forge won't queue it.")
            if owner.get("output_path"):
                dest = watcher.destination_for(
                    owner, str(target), target.suffix.lstrip("."))
                p()
                p("   It will still be renamed and moved to:")
                p(f"   {dest}")
                p()
                p("   If it hasn't moved yet, press 'Check now' on the")
                p("   library. Files needing no conversion are moved by the")
                p("   scanner rather than going through the queue.")
            else:
                p()
                p("   This library has no destination folder, so the file")
                p("   stays where it is. That's expected.")
        else:
            p("   'Skip files already in the right format' is off for this")
            p("   library, so it would be converted anyway.")
        return out

    p()
    p("9. Where it would go")
    if owner.get("output_path"):
        p("   " + str(watcher.destination_for(owner, str(target), spec["container"])))
    else:
        p("   converted in place (no destination set)")
    p()
    p("Nothing is stopping this file. If it still isn't queued, the")
    p("scanner may not have run yet - press 'Check now' on the library.")
    return out


def find(name: str, libraries, limit=25):
    """Files under any of these libraries' watch folders whose name
    contains this text, case-insensitively.

    Lets someone explain a file without knowing its exact in-container
    path — just the name they already recognise from Jellyfin or the
    *arrs.
    """
    needle = name.strip().lower()
    if not needle:
        return []
    matches = []
    for lib in libraries:
        root = Path(lib["watch_path"])
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if path.is_file() and needle in path.name.lower():
                matches.append(path)
                if len(matches) >= limit:
                    return matches
    return matches
