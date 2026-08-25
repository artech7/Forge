"""Forge worker. Run one per encoding machine.

    NODE_NAME=basement-4090 SERVER=http://nas:8420 \
    MOUNTS='[{"server":"/media","local":"/mnt/nas/media"}]' python agent.py
"""
import collections
import json
import os
import shutil
import platform
import re
import socket
import subprocess
import threading
import sys
import tempfile
import time
import traceback
import uuid
from pathlib import Path

import requests

import encoders
import streams

SERVER = os.environ.get("SERVER", "http://localhost:8420").rstrip("/")
NAME = os.environ.get("NODE_NAME", socket.gethostname())
MOUNTS = json.loads(os.environ.get("MOUNTS", "[]"))
MAX_JOBS = int(os.environ.get("MAX_JOBS", "1"))
WORK_DIR = Path(os.environ.get("WORK_DIR", tempfile.gettempdir())) / "forge"

# Stable across restarts so the server doesn't accumulate ghost nodes.
ID_FILE = WORK_DIR / "node-id"


def node_id():
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    if ID_FILE.exists():
        return ID_FILE.read_text().strip()
    new_id = str(uuid.uuid4())
    ID_FILE.write_text(new_id)
    return new_id


# How many jobs to run at once. The server is authoritative — this is
# updated on every check-in so the number can be changed from the UI without
# restarting anything.
DESIRED = {"slots": MAX_JOBS}
SLOT_LOCK = threading.Lock()


def register(nid, caps):
    resp = requests.post(f"{SERVER}/api/nodes/register", timeout=15, json={
        "id": nid, "name": NAME, "encoders": caps,
        "mounts": MOUNTS, "max_jobs": MAX_JOBS, "cpus": os.cpu_count(),
        "recipes": {e: n for e, (n, _b) in encoders.WORKING_RECIPE.items()},
        "benchmarks": encoders.BENCHMARKS,
        "benchmarks_10bit": encoders.BENCHMARKS_10BIT,
    })
    resp.raise_for_status()
    try:
        slots = int((resp.json() or {}).get("slots", MAX_JOBS))
        with SLOT_LOCK:
            DESIRED["slots"] = max(0, slots)
    except (ValueError, TypeError):
        pass


# FFmpeg's stderr is mostly stream descriptions; the real failure is a
# handful of lines among hundreds. Taking the tail catches metadata dumps
# instead of the cause.
ERROR_MARKERS = ("error", "invalid", "unable to", "no such file",
                 "permission denied", "not supported", "failed",
                 "cannot", "unrecognized", "no space left")
# Lines that restate a cause already captured elsewhere in different words —
# skipped once at least one real cause line has been found, so the message
# doesn't say the same thing twice.
NOISE_MARKERS = ("error opening output files:",
                 "task finished with error code")

# FFmpeg tags each log line with the internal component that produced it —
# "[af#0:1 @ 000002c3d107a840]" — which is meaningful to FFmpeg's own
# developers and noise to everyone else. This turns the ones that identify
# an actual stream (af/vf/sf = audio/video/subtitle filtergraph) into a
# plain "audio track 2:", and just drops the memory address for everything
# else, rather than showing a hex pointer no one can act on.
_TAG_RE = re.compile(r"^\[([a-zA-Z_]+)(?:#(\d+):(\d+))?\s*@\s*(?:0x)?[0-9a-fA-F]+\]\s*")
_TAG_KIND = {"af": "audio", "vf": "video", "sf": "subtitle"}


def _clean_line(line):
    m = _TAG_RE.match(line)
    if not m:
        return line
    kind, _file_idx, stream_idx = m.groups()
    rest = line[m.end():].strip()
    name = _TAG_KIND.get(kind)
    if name and stream_idx is not None:
        return f"{name} track {int(stream_idx) + 1}: {rest}"
    return rest


# FFmpeg returns its own error codes rather than small exit statuses, and
# Windows reports them unsigned, so they arrive as huge meaningless numbers.
FFMPEG_CODES = {
    -1094995529: "the data in the file wasn't valid",
    -541478725: "the file ended sooner than expected",
    -1179861752: "no decoder for one of the streams",
    -1128613112: "no encoder for one of the streams",
    -1330794744: "the file format wasn't recognised",
    -2: "a file was missing",
    -13: "permission denied",
    -22: "an argument was rejected",
    -28: "the disk is full",
    -32: "a pipe closed early",
}


def describe_exit(returncode):
    """A code a person can act on, rather than a 10-digit number."""
    signed = returncode - 2 ** 32 if returncode > 2 ** 31 else returncode
    meaning = FFMPEG_CODES.get(signed)
    if meaning:
        return f"FFmpeg gave up \u2014 {meaning}"
    return f"FFmpeg exited {signed}"


def explain_failure(stderr, returncode):
    """Pull the lines that actually say what went wrong."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    hits = []
    audio_related = False
    for line in lines:
        low = line.lower()
        if not any(marker in low for marker in ERROR_MARKERS):
            continue
        if any(noise in low for noise in NOISE_MARKERS) and hits:
            continue          # generic summary, we already have the cause
        if "af#" in low or "audio" in low:
            audio_related = True
        cleaned = _clean_line(line)
        if cleaned not in hits:
            hits.append(cleaned)
    prefix = describe_exit(returncode)
    if not hits:
        return f"{prefix}: " + (_clean_line(lines[-1]) if lines else "no output")

    # A muxer complaining it can't write a header is a consequence; the
    # stream that failed to decode is the cause, and belongs first.
    hits.sort(key=lambda line: 0 if ("audio track" in line or "video track" in line
                                     or "Error while decoding" in line)
              else 1)
    message = f"{prefix}: " + " \u2014 ".join(hits[:2])

    if audio_related:
        message += (". This usually means one audio track is damaged or "
                    "uses something FFmpeg can't decode. Try setting this "
                    "library's audio to \"Leave audio alone\" to see whether "
                    "the rest of the file is fine.")
    return message


def parse_progress(line, duration):
    """FFmpeg -progress emits key=value lines. Return a partial update."""
    key, _, value = line.strip().partition("=")
    if key == "out_time_ms" and duration:
        try:
            return {"progress": min(99.9, (int(value) / 1e6) / duration * 100)}
        except ValueError:
            return {}
    if key == "total_size":
        # FFmpeg reports bytes written so far, which gives a live ratio
        # against the source size long before the job finishes.
        try:
            return {"size_now": int(value)}
        except ValueError:
            return {}
    if key == "fps":
        try:
            return {"fps": float(value)}
        except ValueError:
            return {}
    if key == "speed":
        try:
            return {"speed": float(value.rstrip("x"))}
        except ValueError:
            return {}
    return {}


def source_duration(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60)
        return float(out.stdout.strip())
    except (ValueError, OSError, subprocess.TimeoutExpired):
        return 0


def run_job(job, caps):
    """Encode one job. The server decides where the result finally lands."""
    job_id = job["id"]
    spec = job["spec"]

    if spec.get("measure") == "loudness":
        report_measurement(job)
        return

    container = spec.get("container", "mkv")

    # The source's bit depth decides both which encoder is fastest here and
    # what depth to produce, so it has to be read before choosing.
    source_info = None
    source_depth = 8
    if spec.get("codec") != "copy" and job["transport"] == "local":
        source_info = streams.analyze(job["path"])
        if source_info:
            video = next((st for st in source_info.get("streams", [])
                          if st.get("codec_type") == "video"
                          and not streams.is_image(st)), None)
            source_depth = streams.source_bit_depth(video)

    if spec.get("codec") == "copy":
        encoder = None                      # remux: no video encoder needed
    else:
        encoder = encoders.pick(job["encoders"], caps, source_depth, spec)
        if not encoder:
            report_fail(job_id, "No matching encoder on this node")
            return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    fetched = None
    local_out = None

    try:
        if job["transport"] == "stream":
            fetched = WORK_DIR / f"src-{job_id}{Path(job['source_path']).suffix}"
            with requests.get(f"{SERVER}/api/jobs/{job_id}/source",
                              stream=True, timeout=(15, 900)) as resp:
                resp.raise_for_status()
                with fetched.open("wb") as fh:
                    shutil.copyfileobj(resp.raw, fh)
            src = str(fetched)
            scratch = WORK_DIR / f"job-{job_id}.{container}"
        else:
            src = job["path"]
            if not Path(src).is_file():
                report_fail(job_id, f"Mounted path not found: {src}")
                return
            # Write beside the source so the move is on the same filesystem.
            scratch = Path(src).parent / f".forge-{job_id}.{container}"
            local_out = str(scratch)

        duration = source_duration(src)
        # Inspect the real streams so tracks can be reordered, cover art
        # dropped, and forced subtitles spotted.
        info = source_info if (source_info and src == job["path"]) \
            else streams.analyze(src)

        # Decode every stream once, cheaply, before committing real encode
        # time to this file. Catches a damaged track in seconds instead of
        # discovering it after a long encode fails outright — and for video
        # damage specifically, no retry would ever have fixed it anyway.
        # Skipped on a retry that's already been checked once.
        if info and not spec.get("health_checked"):
            health = streams.health_check(src, info)
            video_ok, video_msg = health["video"] or (True, None)
            if not video_ok:
                report_fail(
                    job_id,
                    "Health check failed — the video stream itself won't "
                    f"decode: {video_msg}",
                    unhealthy_video=True)
                return
            bad_audio = [idx for idx, (ok, msg) in health["audio"].items()
                        if not ok]
            if bad_audio:
                print(f"[job {job_id}] health check: audio track(s) "
                     f"{bad_audio} won't decode, excluding before encoding")
            spec = {**spec, "health_checked": True,
                   "exclude_stream_indexes":
                       sorted(set(spec.get("exclude_stream_indexes") or [])
                              | set(bad_audio))}

        if info:
            _m, _d, notes = streams.plan_streams(info, spec)
            for note in notes:
                print(f"[job {job_id}] {note}")
        encoders.LAST_DEPTH_NOTE.clear()
        cmd = encoders.build_command(src, str(scratch), encoder, spec, info)
        for note in encoders.LAST_DEPTH_NOTE:
            print(f"[job {job_id}] {note}")
        depth_note = encoders.LAST_DEPTH_NOTE[0] if encoders.LAST_DEPTH_NOTE else None
        print(f"[job {job_id}] {encoder or 'remux'}: {Path(job['source_path']).name}")

        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True,
                                encoding="utf-8", errors="replace", bufsize=1)

        # FFmpeg writes warnings to stderr continuously. Nothing was reading
        # that pipe until the process finished, so once the operating
        # system's buffer filled — a few dozen kilobytes of "non-monotonous
        # DTS" is plenty — FFmpeg blocked writing to it and waited forever
        # while this loop waited on stdout. Neither side could move.
        # Draining it in the background keeps both flowing, and the last few
        # hundred lines are kept for reporting a failure.
        stderr_tail = collections.deque(maxlen=300)

        def drain_stderr():
            for line in proc.stderr:
                stderr_tail.append(line)

        stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
        stderr_thread.start()

        update = {"encoder": encoder or "remux"}
        if depth_note:
            update["note"] = depth_note
        last_sent, stopped = 0, False
        for line in proc.stdout:
            update.update(parse_progress(line, duration))
            if time.time() - last_sent > 2:
                reply = post(f"/api/jobs/{job_id}/progress", update)
                last_sent = time.time()
                if reply and reply.get("stop"):
                    print(f"[job {job_id}] {reply.get('reason','stopped')} "
                          f"by the server — abandoning")
                    proc.terminate()
                    stopped = True
                    break
        proc.wait()
        stderr_thread.join(timeout=5)

        if stopped:
            scratch.unlink(missing_ok=True)
            return

        if proc.returncode != 0:
            scratch.unlink(missing_ok=True)
            explanation = explain_failure("".join(stderr_tail), proc.returncode)
            # The phrase is only ever appended when explain_failure decided
            # the cause was audio-related — cheaper than re-deriving the
            # same judgement a second time from the raw stderr.
            report_fail(job_id, explanation,
                        audio_related="Leave audio alone" in explanation)
            return

        size_after = scratch.stat().st_size
        params = {"size_after": size_after, "encoder": encoder or "remux"}

        if job["transport"] == "stream":
            with scratch.open("rb") as fh:
                requests.post(f"{SERVER}/api/jobs/{job_id}/complete",
                              files={"result": (scratch.name, fh)},
                              params=params, timeout=(15, 1800)).raise_for_status()
            scratch.unlink(missing_ok=True)
        else:
            # Leave it in place; the server moves it and handles the original.
            params["output_local"] = local_out
            post(f"/api/jobs/{job_id}/complete", None, params=params)

        print(f"[job {job_id}] done - {size_after / 1e6:.0f} MB")

    except Exception as exc:
        if local_out:
            Path(local_out).unlink(missing_ok=True)
        # Where it happened matters as much as what happened: a bare message
        # like "must be str, not NoneType" says nothing about the cause.
        where = traceback.extract_tb(exc.__traceback__)[-1]
        report_fail(job_id, f"{type(exc).__name__}: {exc} "
                            f"(at {Path(where.filename).name} line "
                            f"{where.lineno}, in {where.name})"[:400])
    finally:
        if fetched:
            Path(fetched).unlink(missing_ok=True)


def post(path, payload, params=None):
    """POST and return the decoded reply, or None if it didn't get through."""
    try:
        resp = requests.post(f"{SERVER}{path}", json=payload,
                             params=params, timeout=30)
        return resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"post {path} failed: {exc}")
        return None


def report_fail(job_id, message, **extra):
    print(f"[job {job_id}] failed: {message}")
    post(f"/api/jobs/{job_id}/fail", {"error": message, **extra})


def report_measurement(job):
    """A loudness-only pass: read the file, report a number, no output.

    Only meaningful for a locally mounted path — measuring loudness
    means decoding the whole audio track, and streaming an entire file
    over HTTP first just to throw the decoded result away afterward
    isn't worth the bandwidth on a remote node.
    """
    job_id, src = job["id"], job["path"]
    if job["transport"] != "local":
        report_fail(job_id, "Loudness measurement needs a locally mounted path.")
        return
    if not Path(src).is_file():
        report_fail(job_id, f"Mounted path not found: {src}")
        return
    print(f"[job {job_id}] measuring loudness: {Path(src).name}")
    values, error = streams.measure_loudness(src)
    if not values:
        report_fail(job_id, f"Could not measure loudness: {error}")
        return
    post(f"/api/jobs/{job_id}/measured", {"loudness": values})


def heartbeat(nid, caps):
    """Keep checking in while encoding, and pick up slot changes.

    A long job means no lease requests for hours, and without this the
    server would mark a perfectly busy node as offline.
    """
    while True:
        time.sleep(20)
        try:
            register(nid, caps)
        except requests.RequestException:
            pass


def runner(index, nid, caps):
    """One concurrent encoding slot.

    Threads above the desired count finish what they're doing and retire,
    so lowering the number in the UI never kills a job mid-encode.
    """
    while True:
        with SLOT_LOCK:
            if index >= DESIRED["slots"]:
                return
        try:
            resp = requests.post(f"{SERVER}/api/nodes/{nid}/lease", timeout=20)
            job = resp.json()
            if job:
                run_job(job, caps)
                continue
        except requests.RequestException as exc:
            print(f"[slot {index}] server unreachable ({exc})")
        time.sleep(8)


def claim_single_instance():
    """Refuse to start if another worker is already using this node id.

    Two workers sharing an id both register as the same node, so the UI shows
    one node while two encodes run — which looks like the app ignoring its own
    settings rather than a leftover process.
    """
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    lock = WORK_DIR / "worker.pid"
    if lock.exists():
        try:
            other = int(lock.read_text().strip())
            os.kill(other, 0)          # signal 0 just tests existence
        except (ValueError, ProcessLookupError, PermissionError):
            pass                        # stale lock, safe to take over
        else:
            print(f"Another worker is already running (pid {other}).")
            print("Stop it first, or delete", lock)
            sys.exit(1)
    lock.write_text(str(os.getpid()))
    return lock


def main():
    lock = claim_single_instance()
    nid = node_id()
    print(f"Detecting encoders on {NAME}…")
    caps, rejected = encoders.detect(explain=True)
    if not caps:
        print("No usable encoders found. Is FFmpeg installed?")
        for enc, why in rejected.items():
            print(f"  {enc}: {why}")
        sys.exit(1)
    print("Verified:", ", ".join(caps))
    notes = encoders.recipe_note()
    if notes:
        print("Settings chosen:")
        for enc, how in notes.items():
            print(f"  {enc:22} {how}")
    ranked = encoders.ranking()
    if ranked:
        print("Measured at 1080p (jobs go to whichever is fastest):")
        for enc, fps in ranked:
            ten = encoders.BENCHMARKS_10BIT.get(enc)
            extra = f"   10-bit: {ten} fps" if ten else ""
            print(f"  {enc:22} {fps} fps{extra}")

    slow10 = encoders.ten_bit_warnings()
    if slow10:
        print()
        print("These are much slower producing 10-bit video:")
        for enc, m in slow10.items():
            print(f"  {enc:22} {m['eight_bit']} fps at 8-bit, "
                  f"{m['ten_bit']} fps at 10-bit")
        print("The hardware encoder is likely giving up and using software.")
        print("Setting a library's colour depth to 8-bit avoids this.")

    slow = [e for e, (n, _b) in encoders.WORKING_RECIPE.items()
            if encoders.is_software_recipe(n)]
    if slow:
        print()
        print("NOTE: these fell back to SOFTWARE encoding:")
        for enc in slow:
            print(f"  {enc}")
        print("The hardware encoder rejected every parameter set tried.")
        if platform.machine() == "x86_64" and platform.system() == "Darwin":
            print()
            print("This FFmpeg is x86_64. On an Apple Silicon Mac that means")
            print("it's running under Rosetta, which does not expose the")
            print("hardware HEVC encoder. Install a native arm64 build:")
            print("  /bin/bash -c \"$(curl -fsSL "
                  "https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"")
            print("  /opt/homebrew/bin/brew install ffmpeg")
    if rejected:
        print("Not usable on this machine:")
        for enc, why in rejected.items():
            print(f"  {enc:22} {why}")

    try:
        register(nid, caps)
    except requests.RequestException as exc:
        print(f"Could not reach the server ({exc}); will keep trying")

    threading.Thread(target=heartbeat, args=(nid, caps), daemon=True).start()

    # Keep exactly as many runner threads alive as the server asks for.
    running = {}
    while True:
        with SLOT_LOCK:
            want = DESIRED["slots"]
        for index in range(want):
            thread = running.get(index)
            if thread is None or not thread.is_alive():
                thread = threading.Thread(target=runner,
                                          args=(index, nid, caps), daemon=True)
                thread.start()
                running[index] = thread
        time.sleep(5)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        lockfile = WORK_DIR / "worker.pid"
        try:
            if lockfile.read_text().strip() == str(os.getpid()):
                lockfile.unlink()
        except OSError:
            pass
