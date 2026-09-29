#!/usr/bin/env python3
"""Look over the files behind failed jobs and say what actually happened to them.

    python check-failed.py --server http://192.168.1.50:58420

Run it on the worker — the machine that can see the media — not the NAS.
It reads the failed queue from the server, then goes and looks at each
file on disk. Nothing is changed: this only reports.

The failure it was written for reads like this:

    FileNotFoundError: [WinError 2] The system cannot find the file
    specified: '...\\.forge-95851.mp4' (at pathlib.py line 840, in stat)

That is the worker measuring its own work file after FFmpeg exited
cleanly, and finding it gone. It says nothing whatsoever about the source
file — so the only way to know whether those files need anything is to go
and look at them, which is what this does.
"""
import argparse
import ast
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path, PureWindowsPath

# Containers worth reporting on when listing what's in a folder. Anything
# else sitting beside a movie (artwork, subtitles, .nfo) is noise here.
MEDIA_SUFFIXES = {".mkv", ".mp4", ".m4v", ".avi", ".ts", ".m2ts",
                  ".mov", ".wmv", ".mpg", ".mpeg", ".webm"}

# The work file vanished between FFmpeg finishing and the worker measuring
# it. Matched on the scratch name rather than the error type: the same
# disappearance surfaces as a stat, an open or a move depending on which
# line got there first.
VANISHED = re.compile(r"\.forge-(\d+)\.[A-Za-z0-9]+")


def get(server, path, token, timeout=30):
    req = urllib.request.Request(server.rstrip("/") + path)
    if token:
        req.add_header("X-Forge-Token", token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            sys.exit("The server wants a token. Pass --token, or set "
                     "FORGE_TOKEN — it's on the node card in Forge.")
        sys.exit(f"{server} answered {exc.code} for {path}")
    except urllib.error.URLError as exc:
        sys.exit(f"Can't reach {server}: {exc.reason}")


def scratch_path(error):
    """Pull the work file's path back out of the recorded error message.

    It's the one part of a failed job that is already written in *this*
    machine's path space, mounts and all, so using it saves having to
    reverse the server's mount mapping to find the folder again.
    """
    quoted = re.search(r": ('.*?'|\".*?\") \(at ", error or "")
    if quoted:
        try:
            return ast.literal_eval(quoted.group(1))
        except (ValueError, SyntaxError):
            pass
    return None


def human(size):
    """Sizes worth reading. A 900 MB file shouldn't print as 0.90 GB."""
    return f"{size / 1e9:.2f} GB" if size >= 1e9 else f"{size / 1e6:.0f} MB"


def probe(path, timeout=120):
    """What FFmpeg makes of this file, or None if it can't read it at all."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_streams", "-show_format", str(path)],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        sys.exit("FFmpeg isn't installed, or isn't on the PATH. This has to "
                 "run on the worker, where it is.")
    except subprocess.TimeoutExpired:
        return None
    if out.returncode != 0:
        return None
    try:
        return json.loads(out.stdout)
    except json.JSONDecodeError:
        return None


def decode_check(path, seconds, timeout=1800):
    """Actually decode the file, rather than just reading its header.

    A truncated or damaged file often probes perfectly well and only falls
    over when something reads it through, so this is the check that can
    clear a file rather than merely fail to condemn it.
    """
    cmd = ["ffmpeg", "-v", "error", "-xerror"]
    if seconds:
        cmd += ["-t", str(seconds)]
    cmd += ["-i", str(path), "-f", "null", "-"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timed out while decoding"
    if out.returncode != 0:
        first = (out.stderr or "").strip().splitlines()
        return False, first[0] if first else f"exit {out.returncode}"
    return True, None


def video_of(info):
    for stream in (info or {}).get("streams", []):
        if stream.get("codec_type") == "video" and not stream.get("disposition", {}).get("attached_pic"):
            return stream
    return None


def describe(path, info):
    """One line of shape for a file that's there: codec, size, resolution."""
    size = path.stat().st_size
    video = video_of(info)
    shape = ""
    if video:
        shape = f"{video.get('codec_name', '?')}"
        if video.get("height"):
            shape += f" {video['width']}x{video['height']}"
    return human(size) + (f", {shape}" if shape else "")


def main():
    ap = argparse.ArgumentParser(
        description="Check the files behind failed Forge jobs.")
    ap.add_argument("--server", default=os.environ.get("SERVER", ""),
                    help="Forge's address, e.g. http://192.168.1.50:58420")
    ap.add_argument("--token", default=os.environ.get("FORGE_TOKEN", ""),
                    help="node token, if the server has a login")
    ap.add_argument("--all", action="store_true",
                    help="look at every failed job, not just the ones whose "
                         "work file vanished")
    ap.add_argument("--deep", action="store_true",
                    help="decode each source all the way through as well. "
                         "Slow — minutes per file — but it's the check that "
                         "can actually clear a file.")
    ap.add_argument("--sample", type=int, default=0, metavar="SECONDS",
                    help="with --deep, decode only this many seconds")
    ap.add_argument("--limit", type=int, default=0,
                    help="stop after this many files")
    args = ap.parse_args()

    if not args.server:
        sys.exit("Say where Forge is: --server http://your-nas:58420")
    server = args.server if args.server.startswith("http") else "http://" + args.server

    data = get(server, "/api/jobs/all?view=failed&sort=newest", args.token)
    jobs = data.get("jobs") or []
    if not jobs:
        print("Nothing in the failed queue.")
        return 0

    picked, other = [], 0
    for job in jobs:
        if VANISHED.search(job.get("error") or ""):
            picked.append(job)
        elif args.all:
            picked.append(job)
        else:
            other += 1

    print(f"{len(jobs)} failed job(s); {len(picked)} to look at"
          + (f", {other} failed for other reasons (--all to include them)"
             if other else "") + ".")
    print()

    if args.limit:
        picked = picked[:args.limit]

    tally = {}

    def verdict(key, text):
        tally[key] = tally.get(key, 0) + 1
        print(f"  verdict: {text}")
        print()

    for job in picked:
        error = job.get("error") or ""
        spec = job.get("spec") or {}
        if isinstance(spec, str):
            try:
                spec = json.loads(spec)
            except json.JSONDecodeError:
                spec = {}
        container = spec.get("container", "mkv")
        wanted = (spec.get("codec") or "").lower()

        # The source's name comes from the job (server path space); the
        # folder comes from the work file in the error (this machine's).
        # Together they point at the file without needing the mounts.
        name = PureWindowsPath(job.get("path", "").replace("/", "\\")).name
        work = scratch_path(error)
        folder = Path(PureWindowsPath(work).parent) if work else None

        print(f"{name}")
        vanished = VANISHED.search(error)
        if vanished:
            print(f"  job {job.get('id')}: the work file .forge-"
                  f"{vanished.group(1)} was gone when the worker went to "
                  f"measure it")
        else:
            print(f"  job {job.get('id')}: {' '.join(error.split())[:200]}")

        if folder is None:
            verdict("unknown", "couldn't tell which folder this was in from "
                               "the error — check it by hand")
            continue
        if not folder.is_dir():
            verdict("unreachable", f"can't see {folder} from this machine. "
                                   "Is the share mapped?")
            continue

        source = folder / name
        siblings = sorted(p for p in folder.iterdir()
                          if p.is_file() and p.suffix.lower() in MEDIA_SUFFIXES
                          and not p.name.startswith(".forge-"))
        leftovers = sorted(folder.glob(".forge-*"))

        source_info = probe(source) if source.is_file() else None
        if source.is_file():
            if source_info is None:
                print(f"  source:  {source.name} — FFmpeg can't read it")
            else:
                print(f"  source:  {source.name} — {describe(source, source_info)}")
        else:
            print(f"  source:  {source.name} — not there any more")

        for other_file in siblings:
            if other_file == source:
                continue
            info = probe(other_file)
            shape = describe(other_file, info) if info else "FFmpeg can't read it"
            print(f"  also in folder: {other_file.name} — {shape}")

        if leftovers:
            total = sum(p.stat().st_size for p in leftovers)
            print(f"  leftover work file(s): "
                  f"{', '.join(p.name for p in leftovers)} "
                  f"({human(total)})")

        # ------------------------------------------------------ the call

        converted = [p for p in siblings
                     if p.suffix.lstrip(".").lower() == container.lower()
                     and p.stem == source.stem]
        source_codec = (video_of(source_info) or {}).get("codec_name", "").lower()
        # hevc is spelt h265 in some places and hevc in others.
        matches_wanted = bool(wanted) and source_codec in (
            wanted, wanted.replace("h265", "hevc"), wanted.replace("hevc", "h265"))

        if not source.is_file() and converted:
            verdict("done", "already converted — the new file is here and the "
                            "original is gone. The job row is stale; clear it.")
            continue
        if not source.is_file():
            verdict("missing", "the source isn't here and no converted copy "
                               "is either. Look in Originals before requeueing.")
            continue
        if source_info is None:
            verdict("damaged", "FFmpeg can't read this file at all. This one "
                               "is a real problem with the file.")
            continue
        if matches_wanted and source.suffix.lstrip(".").lower() == container.lower():
            verdict("done", f"already {source_codec} in .{container} — it was "
                            "converted in place and the failure was recorded "
                            "after the work landed. Clear the job.")
            continue

        if args.deep:
            ok, why = decode_check(source, args.sample)
            if not ok:
                print(f"  decode:  failed — {why}")
                verdict("damaged", "the file itself won't decode. This one is "
                                   "a real problem with the file.")
                continue
            print("  decode:  clean" + (f" (first {args.sample}s)"
                                        if args.sample else ""))

        verdict("requeue",
                "still the original, and it reads fine. Nothing wrong with "
                "this file — the work file went missing under Forge. "
                "Safe to requeue.")

    print("-" * 60)
    labels = {
        "requeue": "fine, just never finished — requeue",
        "done": "already converted — clear the job",
        "damaged": "genuinely broken files",
        "missing": "source gone, no converted copy",
        "unreachable": "folder not visible from here",
        "unknown": "couldn't work out where the file was",
    }
    for key, label in labels.items():
        if tally.get(key):
            print(f"{tally[key]:5}  {label}")
    if tally.get("requeue") and not args.deep:
        print()
        print("Those were cleared on their headers alone. Run again with "
              "--deep (or --deep --sample 60) to decode them as well.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
