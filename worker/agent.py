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
import sysinfo
import verify

SERVER = os.environ.get("SERVER", "http://localhost:8420").rstrip("/")
NAME = os.environ.get("NODE_NAME", socket.gethostname())
MOUNTS = json.loads(os.environ.get("MOUNTS", "[]"))
# Set once a login is configured on the server. Copy it from the node
# card in Forge, which shows the whole command ready to paste.
TOKEN = os.environ.get("FORGE_TOKEN", "").strip()
AUTH_HEADERS = {"X-Forge-Token": TOKEN} if TOKEN else {}
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


# Every FFmpeg this worker has running. Popen does not tie a child's
# life to its parent's on any platform, so without this a Ctrl+C or a
# crash leaves the encode running — still holding gigabytes, still
# writing to a scratch file nobody will claim, and invisible except as
# a machine whose memory never comes back. sweep_work_files on the
# server exists to clear up after exactly that.
LIVE_ENCODES = set()
LIVE_LOCK = threading.Lock()


def _watch(proc):
    with LIVE_LOCK:
        # Drop anything that has already exited. A job that raises
        # between starting FFmpeg and waiting on it never reaches
        # _unwatch, and on a worker left running for weeks those add up.
        for done in [p for p in LIVE_ENCODES if p.poll() is not None]:
            LIVE_ENCODES.discard(done)
        LIVE_ENCODES.add(proc)
    return proc


def _unwatch(proc):
    with LIVE_LOCK:
        LIVE_ENCODES.discard(proc)


def stop_all_encodes(grace=5):
    """Ask every running FFmpeg to stop, then insist.

    Called on the way out. terminate() first so FFmpeg closes its output
    file properly rather than leaving a half-written one behind; kill()
    only for anything that ignores it.
    """
    with LIVE_LOCK:
        procs = list(LIVE_ENCODES)
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except OSError:
            pass
    deadline = time.time() + grace
    for proc in procs:
        try:
            proc.wait(timeout=max(0, deadline - time.time()))
        except Exception:
            try:
                proc.kill()
            except OSError:
                pass
    return len(procs)


class Phase:
    """Says what this job is doing while nothing measurable is happening.

    Fetching a file and decoding every track to check it plays both
    take minutes on a large file and produce no percentage at all. The
    server saw silence, so the lease expired and the job bounced — and
    a file whose check takes longer than the lease could never get past
    it. This keeps saying the same short sentence until the work ends,
    which both renews the lease and gives the queue something to show.

    Used as a context manager so the thread can't outlive the step it
    describes, including when that step raises.
    """

    EVERY = 30          # comfortably inside the server's 120s lease

    def __init__(self, job_id, text):
        self.job_id = job_id
        self.text = text
        self._stop = threading.Event()
        self._thread = None

    def say(self, text):
        """Change the message mid-phase, e.g. moving to the next track."""
        self.text = text
        self._beat()

    def _beat(self):
        try:
            requests.post(f"{SERVER}/api/jobs/{self.job_id}/progress",
                          json={"heartbeat": True, "phase": self.text},
                          headers=AUTH_HEADERS, timeout=10)
        except requests.RequestException:
            pass        # a missed heartbeat is not worth failing the job over

    def _loop(self):
        while not self._stop.wait(self.EVERY):
            self._beat()

    def __enter__(self):
        self._beat()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        return False


def explain_401():
    """Said plainly, because the cause is never obvious from a 401."""
    if TOKEN:
        return ("the server rejected this node's token. Get the current one "
                "from the node card in Forge and restart with it.")
    return ("this Forge has a login set up, so the node needs its token. "
            "Open Forge, look at the node card for the run command, and "
            "start this worker with FORGE_TOKEN set to the value it shows.")


FEATURES = ["video_level"]


def level_expectation(spec, encoder):
    """Which level check this job's output needs, if any.

    Mirrors the server's own reading of the spec: a relabel when the
    video is copied, an encode level when it's re-encoded to HEVC or
    H.264. The two sides have to agree, since the server refuses to
    place a relabel that arrives without the check.
    """
    if spec.get("codec") == "copy" or encoder is None:
        return "relabel" if spec.get("relabel_level") else None
    if (spec.get("encode_level") or {}).get("level") \
            and spec.get("codec") in ("hevc", "h264"):
        return "encode"
    return None


def register(nid, caps):
    resp = requests.post(f"{SERVER}/api/nodes/register", timeout=15,
                         headers=AUTH_HEADERS, json={
        "id": nid, "name": NAME, "encoders": caps,
        "mounts": MOUNTS, "max_jobs": MAX_JOBS, "cpus": os.cpu_count(),
        "recipes": {e: n for e, (n, _b) in encoders.WORKING_RECIPE.items()},
        "benchmarks": encoders.BENCHMARKS,
        "benchmarks_10bit": encoders.BENCHMARKS_10BIT,
        # A snapshot of what this machine is doing, read fresh on every
        # heartbeat. Empty on a worker whose requirements predate it,
        # which the card copes with by showing nothing extra.
        "stats": sysinfo.collect(),
        # What this version can do that older ones can't. The server
        # only hands relabel work to a worker that says video_level: one
        # without it would copy the stream and leave the label as it was.
        "features": FEATURES,
    })
    if resp.status_code == 401:
        raise PermissionError(explain_401())
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
    # Identifies this attempt, not this job. A lease that expires is
    # handed out again while this worker is still encoding, so the job id
    # on its own names a file two attempts would both write to — the
    # first to finish would then have its output deleted or moved out
    # from under the other. A server too old to issue one gets a locally
    # made substitute, which is just as unique and only lacks the
    # server's half of the check.
    token = job.get("lease_token") or uuid.uuid4().hex[:8]

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
            report_fail(job_id, "No matching encoder on this node", token)
            return

    WORK_DIR.mkdir(parents=True, exist_ok=True)
    fetched = None
    local_out = None

    try:
        if job["transport"] == "stream":
            fetched = WORK_DIR / f"src-{job_id}-{token}{Path(job['source_path']).suffix}"
            with Phase(job_id, "copying the file to this machine"), \
                    requests.get(f"{SERVER}/api/jobs/{job_id}/source",
                                 headers=AUTH_HEADERS,
                                 stream=True, timeout=(15, 900)) as resp:
                resp.raise_for_status()
                with fetched.open("wb") as fh:
                    shutil.copyfileobj(resp.raw, fh)
            src = str(fetched)
            scratch = WORK_DIR / f"job-{job_id}-{token}.{container}"
        else:
            src = job["path"]
            if not Path(src).is_file():
                report_fail(job_id, f"Mounted path not found: {src}", token)
                return
            # Write beside the source so the move is on the same filesystem.
            # The token in the name is what keeps two overlapping attempts
            # at this job from writing to, and tidying up, one file.
            scratch = Path(src).parent / f".forge-{job_id}-{token}.{container}"
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
        check_mode = spec.get("health_check") or "full"
        if info and check_mode != "off" and not spec.get("health_checked"):
            with Phase(job_id, "checking the file plays") as phase:
                health = streams.health_check(
                    src, info, mode=check_mode,
                    on_stream=lambda done, total, kind: phase.say(
                        f"reading all {total} tracks through to check the "
                        f"file plays" if kind == "all" else
                        f"checking the start and end of all {total} tracks"
                        if kind == "sample" else
                        f"finding the bad track \u2014 reading the {kind} "
                        f"track on its own, {done} of {total}"))
            video_ok, video_msg = health["video"] or (True, None)
            if not video_ok:
                report_fail(
                    job_id,
                    "Health check failed — the video stream itself won't "
                    f"decode: {video_msg}",
                    token, unhealthy_video=True)
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

        post(f"/api/jobs/{job_id}/progress",
             {"heartbeat": True, "phase": "starting the encoder"})
        proc = _watch(subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, text=True,
                                       encoding="utf-8", errors="replace",
                                       bufsize=1))

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
        _unwatch(proc)
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
            report_fail(job_id, explanation, token,
                        audio_related="Leave audio alone" in explanation)
            return

        # FFmpeg said it was happy, so anything missing here is the file
        # going astray rather than the encode failing. Said plainly:
        # reaching for it anyway raised a bare pathlib FileNotFoundError,
        # which reported a stat() call in a traceback and told nobody
        # looking at the queue anything about the file they queued.
        if not scratch.is_file():
            report_fail(job_id,
                        "The encode finished, but its work file "
                        f"({scratch.name}) was gone before it could be "
                        "measured. The source file is untouched — queue it "
                        "again.", token)
            return

        size_after = scratch.stat().st_size
        params = {"size_after": size_after, "encoder": encoder or "remux",
                  "lease_token": token}

        # A job that changes a level label is only reported done once the
        # change is proved, against the original that's still sitting
        # untouched. A failure here can only repeat if tried again, so
        # the server is told not to.
        expected = level_expectation(spec, encoder)
        if expected:
            with Phase(job_id, "checking the result against the original"):
                out_info = streams.analyze(str(scratch))
                if expected == "relabel":
                    report = verify.verify_relabel(
                        src, str(scratch), spec,
                        info or streams.analyze(src), out_info)
                else:
                    _a, _b, enforced = encoders.level_args(
                        encoder, spec.get("codec"), spec.get("encode_level"))
                    report = verify.verify_encode(str(scratch), spec, out_info,
                                                  enforced, encoder)
            if not report["ok"]:
                scratch.unlink(missing_ok=True)
                report_fail(job_id, f"Level check failed: {report['error']}. "
                                    "The original file is untouched.",
                            token, permanent=True)
                return
            print(f"[job {job_id}] level check passed: "
                  f"{', '.join(report['checks'])}")
            params["verified"] = json.dumps(report)

        if job["transport"] == "stream":
            with scratch.open("rb") as fh:
                requests.post(f"{SERVER}/api/jobs/{job_id}/complete",
                              headers=AUTH_HEADERS,
                              files={"result": (scratch.name, fh)},
                              params=params, timeout=(15, 1800)).raise_for_status()
            scratch.unlink(missing_ok=True)
        else:
            # Leave it in place; the server moves it and handles the original.
            params["output_local"] = local_out
            reply = post(f"/api/jobs/{job_id}/complete", None, params=params)
            if reply and reply.get("superseded"):
                # This attempt lost the job while it was encoding, so the
                # server kept whatever replaced it. Nothing is left for
                # anyone to collect here, and a discarded encode is often
                # gigabytes — take it away rather than wait for the sweep.
                print(f"[job {job_id}] another attempt finished this one "
                      f"first; discarding what this node made")
                scratch.unlink(missing_ok=True)
                return

        print(f"[job {job_id}] done - {size_after / 1e6:.0f} MB")

    except Exception as exc:
        if local_out:
            try:
                Path(local_out).unlink(missing_ok=True)
            except OSError as tidy:
                # Tidying up must never replace the error that caused it.
                # On Windows, removing a file another process still holds
                # raises instead of succeeding, and that escaped this
                # handler — so the job was never reported failed, and the
                # exception went on to kill the slot that was running it.
                print(f"[job {job_id}] could not remove the work file "
                      f"({tidy}); leaving it for the sweep")
        # Where it happened matters as much as what happened: a bare message
        # like "must be str, not NoneType" says nothing about the cause.
        where = traceback.extract_tb(exc.__traceback__)[-1]
        report_fail(job_id, f"{type(exc).__name__}: {exc} "
                            f"(at {Path(where.filename).name} line "
                            f"{where.lineno}, in {where.name})"[:400], token)
    finally:
        if fetched:
            Path(fetched).unlink(missing_ok=True)


def post(path, payload, params=None):
    """POST and return the decoded reply, or None if it didn't get through."""
    try:
        resp = requests.post(f"{SERVER}{path}", json=payload, params=params,
                             headers=AUTH_HEADERS, timeout=30)
        if resp.status_code == 401:
            print(f"post {path} refused: {explain_401()}")
            return None
        return resp.json() if resp.content else {}
    except (requests.RequestException, ValueError) as exc:
        print(f"post {path} failed: {exc}")
        return None


def report_fail(job_id, message, token=None, **extra):
    """Tell the server this job didn't work.

    The lease token goes with it so the server can tell a live attempt's
    failure from one reported by an attempt that has already lost the
    job — the latter is not this job's news to report.
    """
    print(f"[job {job_id}] failed: {message}")
    post(f"/api/jobs/{job_id}/fail",
         {"error": message, "lease_token": token, **extra})


def report_measurement(job):
    """A loudness-only pass: read the file, report a number, no output.

    Only meaningful for a locally mounted path — measuring loudness
    means decoding the whole audio track, and streaming an entire file
    over HTTP first just to throw the decoded result away afterward
    isn't worth the bandwidth on a remote node.
    """
    job_id, src = job["id"], job["path"]
    token = job.get("lease_token")
    if job["transport"] != "local":
        report_fail(job_id,
                    "Loudness measurement needs a locally mounted path.", token)
        return
    if not Path(src).is_file():
        report_fail(job_id, f"Mounted path not found: {src}", token)
        return
    print(f"[job {job_id}] measuring loudness: {Path(src).name}")
    # This is one blocking FFmpeg call with no progress heartbeat during
    # the run itself — without this, the job sits at "leased" for its
    # entire runtime, since nothing ever tells the server it started.
    # That's invisible to a person: the node card only counts a job as
    # busy once it's "running", so a real measurement in progress would
    # otherwise still show the node as idle.
    post(f"/api/jobs/{job_id}/progress", {"progress": 1})
    with Phase(job_id, "measuring how loud it is"):
        values, error = streams.measure_loudness(src)
    if not values:
        report_fail(job_id, f"Could not measure loudness: {error}", token)
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
            resp = requests.post(f"{SERVER}/api/nodes/{nid}/lease", timeout=20,
                                 headers=AUTH_HEADERS)
            if resp.status_code == 401:
                print(f"[slot {index}] {explain_401()}")
                time.sleep(30)      # no point asking quickly; nothing changes
                continue
            job = resp.json()
            if job:
                run_job(job, caps)
                continue
        except requests.RequestException as exc:
            print(f"[slot {index}] server unreachable ({exc})")
        except Exception as exc:
            # A slot has to outlive anything one job can do to it. This
            # was uncaught, so a single unexpected error ended the thread
            # and that slot stopped taking work for the rest of the run —
            # silently, because the node kept pinging and still looked
            # healthy. Overnight every slot could go this way and the
            # whole machine would sit there doing nothing.
            print(f"[slot {index}] unexpected error, carrying on: "
                  f"{type(exc).__name__}: {exc}")
            traceback.print_exc()
        time.sleep(8)


def pid_exists(pid):
    """Is this still a live process — without relying on POSIX kill(pid, 0)
    semantics, which os.kill() doesn't actually have on Windows.

    On Linux/macOS, os.kill(pid, 0) sends nothing and just reports whether
    the process exists. On Windows, signal 0 is CTRL_C_EVENT, and os.kill
    there goes through GenerateConsoleCtrlEvent — a mechanism for sending
    Ctrl+C to a console process group, not for checking existence, and it
    can raise all sorts of unrelated errors (like WinError 11) depending
    on what that PID happens to be doing, rather than a clean answer.
    OpenProcess with a minimal, read-only access right is the actual
    correct way to ask this question on Windows.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True   # exists, just not ours to signal
    return True


def _mounts_json():
    """MOUNTS the way the launchers write it themselves.

    Compact, with no space after the colons: run-node.sh reads the local
    path back out with a sed matching "local":"..." exactly, so a space
    there leaves it empty and quietly skips the warning that the share
    isn't mounted — the most useful thing that script says.
    """
    return json.dumps(MOUNTS, separators=(",", ":"))


def restart_command():
    """The line that starts this worker again on this machine.

    Forge only offers this from its "no machines are connected" panel,
    which is gone for good the moment a node registers — so at the one
    moment it's wanted, when a worker has stopped and needs starting
    again, it isn't anywhere near the node that stopped. Printing it at
    startup puts it in the console of the machine it belongs to.

    Built from what this process is actually running with, so it carries
    this machine's own mounts, name and slots rather than an example.
    """
    system = platform.system()                  # Windows | Darwin | Linux
    # The token is what makes the line usable as it stands, which is the
    # point of printing one. Held back when stdout isn't a terminal:
    # that's run-node-forever.ps1 capturing to worker.log, and a secret
    # belongs in a console someone is reading, not in a file on disk.
    on_screen = sys.stdout.isatty()
    token = TOKEN if (TOKEN and on_screen) else None

    # One mount reads as the pair of paths a person would type; anything
    # more unusual than that is passed back as the JSON it came in as.
    single = MOUNTS[0] if len(MOUNTS) == 1 else None
    work_dir = os.environ.get("WORK_DIR")
    if work_dir and work_dir == str(Path(tempfile.gettempdir()) / "forge"):
        work_dir = None                         # the default, not worth saying

    if system == "Windows":
        parts = [f".\\run-node.ps1 -Server {SERVER}"]
        if single:
            local = single.get("local", "").replace("/", "\\")
            parts.append(f'-Mounts "{local}" -ServerPath {single.get("server", "")}')
        elif MOUNTS:
            parts.append(f"-Mounts '{_mounts_json()}'")
        if NAME != socket.gethostname():
            parts.append(f'-NodeName "{NAME}"')
        if MAX_JOBS != 1:
            parts.append(f"-MaxJobs {MAX_JOBS}")
        if work_dir:
            parts.append(f'-WorkDir "{work_dir}"')
        line = " ".join(parts)
        # PowerShell has no "VAR=value command" form, and the two
        # statements need the semicolon or it's a parse error rather
        # than two commands.
        if token:
            line = f'$env:FORGE_TOKEN="{token}"; {line}'
        elif TOKEN:
            line = f'$env:FORGE_TOKEN="<token from Settings -> Access>"; {line}'
        return "Windows (PowerShell)", line

    env = []
    if token:
        env.append(f"FORGE_TOKEN={token}")
    elif TOKEN:
        env.append("FORGE_TOKEN=<token from Settings -> Access>")
    if MOUNTS:
        env.append(f"MOUNTS='{_mounts_json()}'")
    if NAME != socket.gethostname():
        env.append(f"NODE_NAME='{NAME}'")
    if MAX_JOBS != 1:
        env.append(f"MAX_JOBS={MAX_JOBS}")
    if work_dir:
        env.append(f"WORK_DIR='{work_dir}'")
    prefix = " ".join(env) + " " if env else ""
    label = "Mac" if system == "Darwin" else "Linux"
    return label, f"{prefix}./run-node.sh {SERVER}"


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
        except ValueError:
            other = None                # stale/corrupt lock, safe to take over
        if other and pid_exists(other):
            print(f"Another worker is already running (pid {other}).")
            print("Stop it first, or delete", lock)
            sys.exit(1)
    lock.write_text(str(os.getpid()))
    return lock


def main():
    lock = claim_single_instance()
    # Past the lock this is the only worker on the machine, which is what
    # makes this safe: any FFmpeg still writing one of our scratch files
    # belongs to a run that is no longer around to finish it. A clean
    # exit stops its own encodes and a reboot takes everything with it —
    # this is for the process killed outright, or the console window
    # closed before Python could run its shutdown, where FFmpeg carries
    # on encoding for a job nobody will collect.
    orphans = sysinfo.kill_orphaned_encodes(WORK_DIR)
    if orphans:
        print(f"Stopped {orphans} encode(s) left over from a previous run.")
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
    # Read from each encoder's own help at startup, so this is what this
    # machine's FFmpeg actually accepts rather than what it's assumed to.
    level_notes = encoders.level_note()
    if level_notes:
        print("How each encoder is told the video level:")
        for enc, how in level_notes.items():
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

    where, line = restart_command()
    print()
    print(f"To start this worker again \u2014 {where}:")
    print(f"  {line}")
    if TOKEN and not sys.stdout.isatty():
        print("  (the token is left out because this is being written to a "
              "log; Forge shows it under Settings \u2192 Access)")

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
        # Before anything else: a child left running here is the one
        # thing that outlives this process and keeps costing memory.
        stopped = stop_all_encodes()
        if stopped:
            print(f"Stopped {stopped} encode(s) still running.")
        lockfile = WORK_DIR / "worker.pid"
        try:
            if lockfile.read_text().strip() == str(os.getpid()):
                lockfile.unlink()
        except OSError:
            pass
