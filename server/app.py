"""Forge — distributed transcode server."""
import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile, File, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

import db
import scheduler
import lookup
import naming
import profiles
import watcher
import schedule
import arr

STATIC = Path(__file__).parent / "static"
MEDIA_ROOTS = [r.strip() for r in os.environ.get("MEDIA_ROOTS", "/media").split(",") if r.strip()]
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts"}

app = FastAPI(title="Forge")
listeners: set[WebSocket] = set()


# ---------------------------------------------------------------- lifecycle

# Modules get updated independently when files are copied by hand, so the
# server checks at boot that its pieces actually fit together. A missing
# function surfaces here rather than as a 500 halfway through a session.
REQUIRED = {
    "db": ["init", "migrate", "set_slots", "node_slots", "create_library",
           "get_settings", "save_settings", "record_original", "mark_processed",
           "count_jobs", "job_counts", "delete_job", "delete_jobs",
           "requeue_jobs", "record_completion"],
    "scheduler": ["lease_job", "reverse_path", "requeue_expired"],
    "watcher": ["scan_library", "scan_all", "destination_for", "sweep_originals",
                "filter_verdict", "plan_conversion"],
    "profiles": ["catalog", "resolve", "warnings_for"],
    "naming": ["parse", "format_path", "preview", "resolve"],
    "schedule": ["is_open", "describe", "cleanup_due", "describe_cleanup",
                 "overrun_reason", "describe_auto_fail", "limit_seconds"],
    "lookup": ["TMDB", "enrich"],
}


def check_modules():
    modules = {"db": db, "scheduler": scheduler, "watcher": watcher,
               "profiles": profiles, "naming": naming, "schedule": schedule,
               "lookup": lookup}
    problems = []
    for name, needed in REQUIRED.items():
        module = modules.get(name)
        for attribute in needed:
            if not hasattr(module, attribute):
                problems.append(f"{name}.py is missing {attribute}()")
    if problems:
        print("=" * 62)
        print("Some server files are older than the rest:")
        for problem in problems:
            print(f"  {problem}")
        print()
        print("Copy the whole server folder across, or unzip the release again,")
        print("then restart. Parts of the interface will fail until you do.")
        print("=" * 62)
    return problems


@app.on_event("startup")
async def startup():
    app.state.module_problems = check_modules()
    db.init()
    db.migrate()
    repaired = db.repair_profiles()
    if repaired:
        print(f"Corrected {repaired} library profile(s) with missing settings.")
    asyncio.create_task(reaper())
    asyncio.create_task(watch_loop())
    asyncio.create_task(originals_loop())


async def reaper():
    """Requeue work from nodes that went away, and give up on stuck jobs."""
    while True:
        await asyncio.sleep(20)
        changed = False
        try:
            changed = bool(scheduler.requeue_expired())
        except Exception as exc:  # a reaper crash must not take the server down
            print(f"reaper: {exc}")
        try:
            changed = fail_overrunning() or changed
        except Exception as exc:
            print(f"auto-fail: {exc}")
        if changed:
            try:
                await broadcast()
            except Exception:
                pass


def fail_overrunning():
    """Fail jobs that have run too long or stopped making progress.

    The worker finds out on its next progress report: the endpoint tells it
    to stop, and it terminates FFmpeg and cleans up. Nothing here touches
    files or waiting jobs.
    """
    conf = db.get_settings().get("auto_fail") or {}
    if not conf.get("enabled"):
        return False
    failed = 0
    for job in db.list_jobs(states=list(db.ACTIVE_STATES), limit=200):
        reason = schedule.overrun_reason(job, conf)
        if not reason:
            continue
        db.update_job(job["id"], state="failed", error=reason,
                      finished_at=time.time())
        print(f"[job {job['id']}] {reason}")
        failed += 1
    return failed > 0


async def watch_loop():
    """Poll every library's watch folder for new arrivals.

    Runs a first pass straight away so a newly added library doesn't sit
    idle for a cycle, then settles into the configured interval.
    """
    await asyncio.sleep(2)
    while True:
        try:
            report = await asyncio.to_thread(watcher.scan_all, probe)
            for library in db.list_libraries():
                if library["name"] in report:
                    log_filed(library, report[library["name"]])
            if any(r.get("queued") or r.get("filed") for r in report.values()):
                await broadcast()
        except Exception as exc:
            print(f"watcher: {exc}")
        interval = db.get_settings().get("scan_seconds") or watcher.SCAN_SECONDS
        await asyncio.sleep(max(10, int(interval)))


async def originals_loop():
    """Check every ten minutes whether the Originals sweep is due.

    The last run is stored rather than held in memory, so a restart doesn't
    cause a second sweep on the same day or reset an interval.
    """
    while True:
        await asyncio.sleep(600)
        try:
            settings = db.get_settings()
            conf = settings.get("originals") or {}
            stamp = (settings.get("originals_state") or {}).get("last_run")
            last_run = datetime.fromisoformat(stamp) if stamp else None
            due, _why = schedule.cleanup_due(conf, last_run)
            if not due:
                continue
            result = await asyncio.to_thread(watcher.sweep_originals, settings)
            db.save_settings({"originals_state":
                              {"last_run": datetime.now().isoformat()}})
            print(f"originals sweep: {result}")
            await broadcast()
        except Exception as exc:
            print(f"originals sweep: {exc}")


async def broadcast():
    """Push state to any open browsers.

    Never allowed to raise: this is called from the middle of worker
    endpoints, so a rendering problem here would fail the worker's request
    and stall encoding. A broken dashboard is a nuisance; a stalled queue
    is a real outage.
    """
    try:
        payload = json.dumps(await build_state())
    except Exception as exc:
        print(f"broadcast: could not build state ({exc})")
        return
    dead = set()
    for ws in listeners:
        try:
            await ws.send_text(payload)
        except Exception:
            dead.add(ws)
    listeners.difference_update(dead)


def _text(module, name, *args, fallback=""):
    """Call an optional helper, tolerating an out-of-date module.

    A missing function should cost one line of description, not every
    websocket update. The startup check and the banner report the real
    problem; this just keeps the interface alive meanwhile.
    """
    fn = getattr(module, name, None)
    if not fn:
        return fallback
    try:
        return fn(*args)
    except Exception:
        return fallback


async def build_state():
    now = time.time()
    nodes = db.list_nodes()
    for node in nodes:
        node["online"] = (now - node["last_seen"]) < 45
        node["active"] = scheduler.active_job_count(node["id"])
    return {
        "nodes": nodes,
        # Only live work goes over the websocket. History is fetched on
        # demand, so a library with thousands of finished jobs doesn't make
        # every update enormous.
        "jobs": db.list_jobs(states=list(db.ACTIVE_STATES), limit=50),
        "counts": db.job_counts(),
        # Per-library breakdown of the same counts, so the interface can
        # show accurate tab badges the instant a library is selected,
        # without a round trip. Cheap: one GROUP BY, not 4 queries per lib.
        "counts_by_library": db.counts_by_library(),
        "stats": db.stats(),
        "deep_scan": DEEP_SCAN_STATE,
        "libraries": db.list_libraries(),
        "settings": db.get_settings(),
        "schedule_open": _text(schedule, "is_open", db.get_settings(),
                               fallback=True),
        "schedule_text": _text(schedule, "describe", db.get_settings(),
                               fallback="Running any time"),
        "originals": db.originals_summary(),
        # Same text under both keys: the interface reads cleanup_text, the
        # settings panel reads originals_text.
        "auto_fail_text": _text(schedule, "describe_auto_fail",
                                db.get_settings().get("auto_fail") or {}),
        "module_problems": getattr(app.state, "module_problems", []),
        "cleanup_text": _text(schedule, "describe_cleanup",
                              db.get_settings().get("originals") or {}),
        "originals_text": schedule.describe_cleanup(
            db.get_settings().get("originals") or {}),
    }


# ------------------------------------------------------------------- probing

def _bit_depth(video):
    """How many bits per colour sample. 10-bit sources behave very
    differently on hardware encoders, so it's worth knowing up front."""
    if not video:
        return 8
    raw = video.get("bits_per_raw_sample")
    if raw:
        try:
            return int(raw)
        except (TypeError, ValueError):
            pass
    fmt = video.get("pix_fmt") or ""
    if "12" in fmt:
        return 12
    return 10 if "10" in fmt else 8


def probe(path):
    """ffprobe a file into the shape the rules engine will want.

    The rules engine only ever needs the summary fields returned below —
    but a deep scan (see /api/scan/deep) wants everything ffprobe already
    handed over and this used to just throw away: every audio and
    subtitle track with its own language and channel layout, HDR/color
    info, frame rate, codec profile. Kept under "detail" rather than as
    more top-level columns, the same way spec/profile/filters are JSON
    blobs elsewhere in this schema.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", path],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=60,
        )
        if out.returncode != 0:
            return None
        data = json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError,
            TypeError, UnicodeDecodeError):
        return None

    streams = data.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
    sub_streams = [s for s in streams if s.get("codec_type") == "subtitle"]
    audio = [s.get("codec_name") for s in audio_streams]
    fmt = data.get("format", {})
    total = int(fmt.get("bit_rate", 0) or 0)
    vbits = int((video or {}).get("bit_rate", 0) or 0)
    if not vbits and total:
        # Many MKVs don't store per-stream rates. Audio is a small slice, so
        # the container total minus a rough allowance is close enough to
        # judge whether a file is bloated.
        vbits = max(0, total - 192000 * max(1, len(audio)))

    def frame_rate():
        raw = (video or {}).get("r_frame_rate") or "0/1"
        try:
            n, d = raw.split("/")
            d = int(d)
            return round(int(n) / d, 3) if d else 0
        except (ValueError, ZeroDivisionError):
            return 0

    transfer = (video or {}).get("color_transfer") or ""
    hdr = transfer in {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}

    return {
        "size": int(fmt.get("size", 0) or 0),
        "duration": float(fmt.get("duration", 0) or 0),
        "bitrate": total,
        "video_bitrate": vbits,
        "video_codec": video.get("codec_name") if video else None,
        "pix_fmt": video.get("pix_fmt") if video else None,
        "bit_depth": _bit_depth(video),
        "width": video.get("width") if video else None,
        "height": video.get("height") if video else None,
        "audio_codecs": audio,
        "detail": {
            "container": fmt.get("format_name"),
            "frame_rate": frame_rate(),
            "profile": (video or {}).get("profile"),
            "level": (video or {}).get("level"),
            "hdr": hdr,
            "color_transfer": transfer or None,
            "color_primaries": (video or {}).get("color_primaries"),
            "audio_tracks": [
                {"codec": s.get("codec_name"),
                 "language": (s.get("tags") or {}).get("language"),
                 "channels": s.get("channels"),
                 "channel_layout": s.get("channel_layout")}
                for s in audio_streams
            ],
            "subtitle_tracks": [
                {"codec": s.get("codec_name"),
                 "language": (s.get("tags") or {}).get("language")}
                for s in sub_streams
            ],
        },
    }


# ---------------------------------------------------------------- node API

@app.post("/api/nodes/register")
async def register_node(req: Request):
    body = await req.json()
    required = ("id", "name")
    if not all(k in body for k in required):
        raise HTTPException(400, "id and name are required")
    db.upsert_node(
        body["id"], body["name"],
        body.get("encoders", []), body.get("mounts", []),
        int(body.get("max_jobs", 1)),
        body.get("recipes"), body.get("benchmarks"), body.get("cpus"),
        body.get("benchmarks_10bit"),
    )
    await broadcast()
    # The server owns concurrency, so the worker is told how many to run.
    return {"ok": True, "slots": db.node_slots(body["id"])}


@app.post("/api/nodes/{node_id}/lease")
async def lease(node_id: str):
    db.touch_node(node_id)
    # Queueing never stops; only handing out work respects the schedule.
    if not schedule.is_open(db.get_settings()):
        return {}
    job = scheduler.lease_job(node_id)
    if job:
        await broadcast()
    return job or {}


@app.post("/api/nodes/{node_id}/slots")
async def set_slots(node_id: str, req: Request):
    """How many files this node converts at once."""
    body = await req.json()
    value = db.set_slots(node_id, body.get("slots", 1))
    await broadcast()
    return {"slots": value}


@app.post("/api/jobs/{job_id}/progress")
async def progress(job_id: int, req: Request):
    body = await req.json()
    # A node mid-encode isn't asking for work, so progress is its proof of life.
    job = db.get_job(job_id)
    if job and job.get("node_id"):
        db.touch_node(job["node_id"])
    # If the job was cancelled or re-queued while this node was working on
    # it, tell the node to stop. Otherwise it would finish an encode nobody
    # wants and overwrite the file on the way out.
    if job and job["state"] not in ("leased", "running"):
        return {"stop": True, "reason": job["state"]}

    # Recorded only when the figure actually advances, so a worker that keeps
    # reporting the same percentage still counts as stalled.
    reported = float(body.get("progress", 0))
    moved = reported > float(job.get("progress") or 0) if job else True
    db.update_job(
        job_id, state="running",
        progress=reported,
        **({"progress_at": time.time()} if moved else {}),
        fps=float(body.get("fps", 0)),
        speed=float(body.get("speed", 0)),
        size_now=int(body.get("size_now") or 0) or None,
        encoder_used=body.get("encoder"),
        **({"outcome": body["note"]} if body.get("note") else {}),
    )
    scheduler.renew_lease(job_id)
    await broadcast()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/source")
async def source(job_id: int):
    """Stream the original to a node that can't see the file itself."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if not Path(job["path"]).is_file():
        raise HTTPException(410, "Source file is gone")
    return FileResponse(job["path"], filename=Path(job["path"]).name)


@app.post("/api/jobs/{job_id}/complete")
async def complete(job_id: int, result: UploadFile = File(None),
                   size_after: int = 0, encoder: str = "",
                   output_local: str = ""):
    """Finish a job: place the new file, then deal with the original.

    Remote nodes upload the result. Nodes with the share mounted wrote it
    themselves and tell us where, in their path space — we map that back to
    ours so all file placement happens in one place.
    """
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    if job["state"] in ("cancelled", "queued"):
        # Abandoned mid-flight; don't place a file for a job that was called off.
        return {"ok": False, "error": f"job was {job['state']}"}

    source = Path(job["path"])
    library = db.get_library(job["library_id"]) if job.get("library_id") else None
    container = job["spec"].get("container", "mkv")

    if library:
        final = watcher.destination_for(library, job["path"], container)
    else:
        final = source.with_suffix("." + container)

    try:
        final.parent.mkdir(parents=True, exist_ok=True)
        staged = final.with_name(final.name + ".forge-part")

        if result is not None:
            with staged.open("wb") as fh:
                shutil.copyfileobj(result.file, fh)
        else:
            node = db.get_node(job["node_id"]) or {}
            written = Path(scheduler.reverse_path(node, output_local))
            if not written.is_file():
                raise FileNotFoundError(f"Worker output not visible: {written}")
            shutil.move(str(written), staged)

        size_after = staged.stat().st_size
        if size_after == 0:
            staged.unlink(missing_ok=True)
            raise ValueError("Result file was empty")

        os.replace(staged, final)          # atomic within a filesystem
        handle_original(job, library, final)

    except Exception as exc:
        db.update_job(job_id, state="failed", error=f"Placing file: {exc}"[:400],
                      finished_at=time.time())
        await broadcast()
        return {"ok": False, "error": str(exc)}

    db.update_job(job_id, size_after=size_after or None, final_path=str(final),
                  encoder_used=encoder or job.get("encoder_used"))

    profile = (library or {}).get("profile") or {}
    spec = job.get("spec") or {}

    # A salvage pass deliberately keeps the original picture, so there is no
    # size saving to judge — the point was the audio, tracks and filing.
    if spec.get("salvage"):
        db.record_completion(job.get("size_before"), size_after)
        db.update_job(job_id, state="done", progress=100,
                      outcome="picture kept as it was — it was already "
                              "efficient — everything else applied",
                      finished_at=time.time())
        if library:
            try:
                st = final.stat()
                db.mark_processed(str(final), st.st_mtime, st.st_size, library["id"])
            except OSError:
                pass
        await broadcast()
        return {"ok": True, "path": str(final), "salvaged": True}

    # Was it actually worth doing? A conversion that grew the file is worse
    # than no conversion at all: bigger *and* re-encoded.
    threshold = float(profile.get("min_saving_percent") or 0)
    worth_it, percent, reason = profiles.savings_verdict(
        job.get("size_before"), size_after, threshold)

    if not worth_it:
        return await handle_bloated(db.get_job(job_id), library, percent, reason)

    db.record_completion(job.get("size_before"), size_after)
    db.update_job(job_id, state="done", progress=100, outcome=None,
                  finished_at=time.time())
    if library:
        try:
            st = final.stat()
            db.mark_processed(str(final), st.st_mtime, st.st_size, library["id"])
        except OSError:
            pass
    await broadcast()
    return {"ok": True, "path": str(final)}


async def handle_bloated(job, library, percent, reason):
    """A conversion that isn't worth keeping.

    Puts the original back where possible, then either steps down the
    quality automatically or parks the job for review.
    """
    note = reason or "not worth keeping"
    restored, message = watcher.restore_original(job, library) if library \
        else (False, "this job has no library")
    if restored:
        note += " — original restored"
    else:
        note += f" — kept the new file ({message})"

    db.update_job(job["id"], state="bloated", progress=100,
                  outcome=note, finished_at=time.time())

    # Automatic ladder: only meaningful if the original came back, since
    # otherwise there's nothing left to convert from.
    profile = (library or {}).get("profile") or {}
    ladder = profiles.retry_ladder(profile)
    attempt = job.get("attempt", 1)
    if restored and ladder:
        current = int((job.get("spec") or {}).get("quality") or 22)
        nxt = next((q for q in ladder if q > current), None)
        if nxt:
            spec = {**job["spec"], "quality": nxt}
            new_id = db.enqueue(job["path"], spec, job.get("size_before"),
                                library["id"], attempt=attempt + 1)
            if new_id:
                # The superseded attempt would otherwise leave one row per
                # rung of the ladder for a single file.
                db.delete_job(job["id"])
                await broadcast()
                return {"ok": True, "retrying": nxt, "job": new_id}

    # Nothing is going to make this picture smaller without hurting it. Keep
    # the original video stream, but still do the audio, tracks, renaming and
    # filing — the file shouldn't be stranded in the inbox over this.
    if (restored and profile.get("salvage_when_stuck", True)
            and not (job.get("spec") or {}).get("salvage")):
        spec = {**job["spec"], "codec": "copy", "salvage": True}
        new_id = db.enqueue(job["path"], spec, job.get("size_before"),
                            library["id"], attempt=attempt + 1)
        if new_id:
            db.delete_job(job["id"])
            await broadcast()
            return {"ok": True, "salvaging": True, "job": new_id,
                    "after_attempts": attempt}

    if attempt > 1:
        note += f" (after {attempt} attempts)"

    # The original is back in the watch folder, so without this the scanner
    # would find it again on the next pass and convert it forever.
    source = Path(job["path"])
    if library and source.is_file():
        try:
            st = source.stat()
            db.mark_processed(str(source), st.st_mtime, st.st_size, library["id"])
        except OSError:
            pass

    db.update_job(job["id"], outcome=note)
    await broadcast()
    return {"ok": True, "bloated": True, "note": note}


def handle_original(job, library, final):
    """Archive, delete, or keep the source file once the new one is safe."""
    source = Path(job["path"])
    if not source.exists() or source.resolve() == final.resolve():
        return
    action = (library or {}).get("original_action", "archive")

    if action == "delete":
        source.unlink(missing_ok=True)
    elif action == "archive" and library:
        dest_dir = watcher.originals_dir(library)
        try:
            relative = source.relative_to(Path(library["watch_path"])).parent
        except ValueError:
            relative = Path(".")
        target_dir = dest_dir / relative
        target_dir.mkdir(parents=True, exist_ok=True)
        archived = target_dir / source.name
        size = source.stat().st_size
        shutil.move(str(source), str(archived))
        db.record_original(str(archived), job["id"], library["id"],
                           str(final), size)
    elif action == "archive":
        source.unlink(missing_ok=True)


@app.post("/api/jobs/{job_id}/fail")
async def fail(job_id: int, req: Request):
    body = await req.json()
    error = body.get("error", "Unknown")
    job = db.get_job(job_id)
    spec = (job or {}).get("spec") or {}

    if job and body.get("unhealthy_video"):
        return await handle_unhealthy_video(job, error)

    # Only worth retrying when audio was actually being re-encoded — if it
    # was already a straight copy (the library's own setting, or Forge's
    # own remux shortcut when the source already matched), retrying with
    # "leave audio alone" would send FFmpeg the exact same audio handling
    # that just failed.
    if job and body.get("audio_related") and spec.get("audio") != "copy":
        retried = await handle_audio_fail(job, error)
        if retried:
            return retried

    # Either this wasn't an audio problem, or it was and the audio was
    # already untouched — by the library's setting, by a remux shortcut,
    # or by an earlier salvage retry — and it still failed. That last case
    # is exactly what "ignored" is for: this already *is* the "leave the
    # audio alone" attempt, and it didn't work, so there's nothing left to
    # try automatically.
    already_left_alone = bool(body.get("audio_related")) and spec.get("audio") == "copy"
    state = "ignored" if already_left_alone else "failed"
    db.update_job(job_id, state=state, error=error, finished_at=time.time())
    await broadcast()
    return {"ok": True}


async def handle_unhealthy_video(job, error):
    """A file whose video stream can't be decoded at all.

    Unlike a damaged audio track, there's no fallback encode setting that
    fixes this — the picture data itself is gone. No retry, ladder, or
    audio-copy substitution helps. The only two honest outcomes are
    "someone should look at this" or, if Radarr/Sonarr are configured for
    this library, "ask the *arr to fetch a working copy instead."
    """
    library = db.get_library(job["library_id"]) if job.get("library_id") else None
    profile = (library or {}).get("profile") or {}
    conf = profile.get("arr") or {}
    note = ("The video stream itself won't decode — this file is corrupt, "
           "not just this attempt.")

    if conf.get("on_unhealthy_video") == "delete_and_research" and conf.get("url"):
        ok, message = await asyncio.to_thread(
            arr.find_and_research, conf.get("kind"), conf.get("url"),
            conf.get("api_key"), job["path"],
            conf.get("path_from", ""), conf.get("path_to", ""))
        if ok:
            db.update_job(job["id"], state="removed", progress=100,
                          finished_at=time.time(), outcome=f"{note} {message}")
            await broadcast()
            return {"ok": True, "removed_and_researched": True}
        note += f" Tried to remove it via {conf.get('kind')}, but: {message}"

    db.update_job(job["id"], state="ignored", error=note,
                  finished_at=time.time())
    await broadcast()
    return {"ok": True}


async def handle_audio_fail(job, error):
    """One automatic retry with this job's audio left untouched.

    Only called when the failed attempt was genuinely re-encoding audio
    (see the "audio" != "copy" check in fail() above) — so this is always
    a real, different configuration from what just failed, never a repeat
    of it. If this retry also fails, fail() will see spec["audio"] =="copy"
    next time and route straight to "ignored" instead of looping.
    """
    spec = job.get("spec") or {}
    new_spec = {**spec, "audio": "copy", "audio_salvage": True}
    # This job's row is still 'running' at this point - nothing has updated
    # it yet - and the database won't allow a second active row for the
    # same path, which is exactly what the retry would be. Marking this
    # one out of the way first is the same ordering handle_bloated() uses
    # for the same reason; without it, enqueue() below always silently
    # fails and this retry can never actually happen.
    db.update_job(job["id"], state="cancelled")
    new_id = db.enqueue(job["path"], new_spec, job.get("size_before"),
                        job.get("library_id"), attempt=job.get("attempt", 1) + 1)
    if not new_id:
        # A genuine race, not the expected case above - put this job back
        # exactly as it was so fail()'s normal Failed/Ignored logic still
        # applies to it, rather than it vanishing with no result at all.
        db.update_job(job["id"], state="running")
        return None
    db.delete_job(job["id"])
    await broadcast()
    return {"ok": True, "retrying_with_audio_copied": True, "job": new_id}


# ----------------------------------------------------------------- client API

@app.get("/api/jobs")
async def list_jobs(view: str = "active", page: int = 1, per_page: int = 20,
                     library_id: int = None):
    """One page of jobs from a view, with enough detail to render a pager.

    library_id narrows both the page of jobs and the counts to one library,
    so each library's queue tab can be paged independently of the others.
    """
    states = db.VIEWS.get(view)
    if not states:
        raise HTTPException(400, f"Unknown view: {view}")
    per_page = max(1, min(100, int(per_page)))
    total = db.count_jobs(list(states), library_id)
    pages = max(1, -(-total // per_page))
    page = max(1, min(page, pages))
    return {
        "view": view, "page": page, "pages": pages, "total": total,
        "per_page": per_page,
        "jobs": db.list_jobs(list(states), per_page, (page - 1) * per_page,
                             library_id),
        "counts": db.job_counts(library_id),
    }


@app.delete("/api/jobs/{job_id}")
async def remove_job(job_id: int):
    """Forget a job. The media file itself is never touched."""
    db.delete_job(job_id)
    await broadcast()
    return {"ok": True}


@app.get("/api/jobs/{job_id}/retry-options")
async def retry_options(job_id: int):
    """Quality steps available for a file that came out too big.

    The reason a retry isn't possible matters: blaming the current library
    setting is wrong when the file was converted under a different one.
    """
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    library = db.get_library(job["library_id"]) if job.get("library_id") else None
    profile = (library or {}).get("profile") or {}
    spec = job.get("spec") or {}

    record = db.original_for_job(job_id)
    source_back = Path(job["path"]).is_file()
    archived_exists = bool(record and Path(record["archived_path"]).is_file())
    can_retry = bool(source_back or archived_exists)

    now_keeps = (library or {}).get("original_action") == "archive"
    then_kept = spec.get("original_action", "archive") == "archive"

    if can_retry:
        reason = None
    elif not library:
        reason = ("This job isn't attached to a library any more, so Forge "
                  "doesn't know where to put the result.")
    elif not now_keeps:
        reason = (f"This library is set to "
                  f"“{_action_name(library['original_action'])}”, so "
                  f"the source file is gone. Change it to “Move it to an "
                  f"Originals folder” and future files can be retried.")
    elif not then_kept:
        reason = ("This file was converted while the library was set to "
                  f"“{_action_name(spec.get('original_action'))}”, so "
                  "its source wasn't kept. The library keeps originals now, so "
                  "files converted from here on can be retried.")
    elif record:
        reason = (f"The archived original is missing from "
                  f"{Path(record['archived_path']).parent}. It may have been "
                  f"removed by an Originals cleanup or by hand.")
    else:
        reason = ("No archived original was recorded for this file, so there "
                  "is nothing to convert from. This can happen to files "
                  "processed by an older version of Forge.")

    return {
        "current_quality": int(spec.get("quality") or 22),
        "steps": profiles.manual_steps(profile),
        "can_retry": can_retry,
        "reason": reason,
        "keeps_originals": now_keeps,
        "source_present": source_back,
    }


def _action_name(action):
    names = {"archive": "Move it to an Originals folder",
             "delete": "Delete it", "keep": "Leave it where it is"}
    return names.get(action, action or "unknown")


@app.post("/api/jobs/{job_id}/retranscode")
async def retranscode(job_id: int, req: Request):
    """Convert this file again at a lower quality."""
    body = await req.json()
    quality = int(body.get("quality") or 0)
    if not 1 <= quality <= 51:
        raise HTTPException(400, "Pick a quality between 1 and 51.")
    ok, message = await _retranscode_one(job_id, quality)
    if not ok:
        raise HTTPException(400, message)
    await broadcast()
    return {"ok": True, "quality": quality}


async def _retranscode_one(job_id, quality):
    """Shared by the single-job and batch retranscode endpoints.

    Doesn't broadcast itself — batch callers do one broadcast after the
    whole selection is processed rather than one per job.
    """
    job = db.get_job(job_id)
    if not job:
        return False, "No such job."
    library = db.get_library(job["library_id"]) if job.get("library_id") else None

    source = Path(job["path"])
    if not source.is_file():
        restored, message = watcher.restore_original(job, library)
        if not restored:
            return False, f"Can't convert it again: {message}."

    # Cleared so the scanner stops treating this file as settled.
    db.forget_processed(str(source))
    spec = {**(job.get("spec") or {}), "quality": quality}
    new_id = db.enqueue(str(source), spec, job.get("size_before"),
                        job.get("library_id"),
                        attempt=job.get("attempt", 1) + 1)
    if not new_id:
        return False, "That file is already queued."
    db.delete_job(job_id)
    return True, None


@app.post("/api/jobs/bloated/bulk-retranscode")
async def bulk_retranscode(req: Request):
    """Apply one quality setting to a whole batch of bloated jobs at once.

    Selecting through them one at a time to pick the same quality on each
    is the exact tedium this exists to remove — same underlying retry as
    the single-job button, just looped, with one broadcast at the end
    instead of one per job.
    """
    body = await req.json()
    job_ids = body.get("job_ids") or []
    quality = int(body.get("quality") or 0)
    if not 1 <= quality <= 51:
        raise HTTPException(400, "Pick a quality between 1 and 51.")
    if not job_ids:
        raise HTTPException(400, "No jobs selected.")

    succeeded, failed = [], []
    for job_id in job_ids:
        ok, message = await _retranscode_one(job_id, quality)
        (succeeded if ok else failed).append(
            job_id if ok else {"id": job_id, "reason": message})
    await broadcast()
    return {"queued": len(succeeded), "failed": failed}


@app.post("/api/jobs/bloated/bulk-accept")
async def bulk_accept(req: Request):
    """Keep a whole batch of bloated files as they are, in one action."""
    body = await req.json()
    job_ids = body.get("job_ids") or []
    if not job_ids:
        raise HTTPException(400, "No jobs selected.")
    n = 0
    for job_id in job_ids:
        job = db.get_job(job_id)
        if not job or job.get("state") != "bloated":
            continue
        db.record_completion(job.get("size_before"), job.get("size_after"))
        db.update_job(job_id, state="done",
                      outcome=(job.get("outcome") or "") + " — kept anyway")
        n += 1
    await broadcast()
    return {"kept": n}


@app.post("/api/jobs/{job_id}/accept")
async def accept_job(job_id: int):
    """Keep the converted file as it is, despite the size."""
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")
    db.record_completion(job.get("size_before"), job.get("size_after"))
    db.update_job(job_id, state="done",
                  outcome=(job.get("outcome") or "") + " — kept anyway")
    await broadcast()
    return {"ok": True}


@app.post("/api/jobs/bulk")
async def bulk_jobs(req: Request):
    """Retry, clear, or cancel a whole view — or a whole library — at once."""
    body = await req.json()
    action = body.get("action")
    library_id = body.get("library_id")

    if action == "cancel":
        # Cancel targets whatever is active, not a state list from VIEWS,
        # since "in progress" isn't a single fixed set of rows the way
        # failed/done/ignored are — it's whichever jobs happen to be
        # queued/leased/running right now.
        cancelled = db.cancel_active_jobs(library_id)
        await broadcast()
        return {"cancelled": cancelled}

    states = db.VIEWS.get(body.get("view", "failed"))
    if not states:
        raise HTTPException(400, "Unknown view")

    if action == "retry":
        if set(states) & set(db.ACTIVE_STATES):
            raise HTTPException(400, "Those jobs are already queued")
        moved, skipped = db.requeue_jobs(list(states), library_id)
        await broadcast()
        return {"requeued": moved, "skipped": skipped}

    if action == "clear":
        if set(states) & set(db.ACTIVE_STATES):
            raise HTTPException(400, "Cancel active jobs instead of clearing them")
        removed = db.delete_jobs(list(states), library_id)
        await broadcast()
        return {"removed": removed}

    raise HTTPException(400, "Unknown action")


@app.get("/api/state")
async def state():
    return await build_state()


@app.get("/api/health")
async def health():
    """What this server can actually do, for the interface to check against."""
    return {
        "ok": not getattr(app.state, "module_problems", []),
        "problems": getattr(app.state, "module_problems", []),
        "routes": sorted({r.path for r in app.routes if hasattr(r, "path")}),
    }


# Deep scan: walks every file in every library regardless of whether it's
# already been probed, for the Stats tab's "as much detail as possible"
# ask. Genuinely slow on a large library by design — this pushed progress
# over the same websocket everything else uses, rather than adding a
# second channel, so the frontend needs no new plumbing to watch it run.
DEEP_SCAN_STATE = {"active": False, "library": None, "current": None,
                   "done": 0, "total": 0}


async def run_deep_scan(library_ids=None):
    if DEEP_SCAN_STATE["active"]:
        return
    libraries = [l for l in db.list_libraries()
                if library_ids is None or l["id"] in library_ids]
    targets = []
    for lib in libraries:
        for path in Path(lib["watch_path"]).rglob("*"):
            if path.is_file() and path.suffix.lower() in VIDEO_EXT:
                targets.append((lib, path))

    DEEP_SCAN_STATE.update(active=True, done=0, total=len(targets),
                           library=None, current=None)
    await broadcast()
    try:
        for i, (lib, path) in enumerate(targets):
            DEEP_SCAN_STATE["library"] = lib["name"]
            DEEP_SCAN_STATE["current"] = path.name
            try:
                info = await asyncio.to_thread(probe, str(path))
                if info:
                    db.cache_probe(str(path), info)
            except Exception as exc:
                print(f"deep scan: {path} ({exc})")
            DEEP_SCAN_STATE["done"] = i + 1
            # Every file, not just every few — a full library scan is
            # exactly the case where "is this actually still moving"
            # matters most, and a probe is cheap enough that broadcasting
            # this often isn't a real cost.
            await broadcast()
    finally:
        DEEP_SCAN_STATE.update(active=False, current=None)
        await broadcast()


@app.post("/api/scan/deep")
async def scan_deep(req: Request):
    """Start a full, forced reprobe of every file — see run_deep_scan."""
    if DEEP_SCAN_STATE["active"]:
        raise HTTPException(409, "A deep scan is already running.")
    try:
        body = await req.json()
    except Exception:
        body = {}
    library_ids = (body or {}).get("library_ids")
    asyncio.create_task(run_deep_scan(library_ids))
    return {"started": True}


@app.get("/api/stats/files")
async def stats_files(attribute: str, value: str, library_id: int = None):
    """The files behind one chart segment, for clicking into a chart."""
    allowed = {"video_codec", "audio_codec", "container", "resolution", "bit_depth"}
    if attribute not in allowed:
        raise HTTPException(400, f"Can't drill into '{attribute}'.")
    return {"files": db.files_matching(attribute, value, library_id)}


@app.get("/api/stats/jobs")
async def stats_jobs(attribute: str, value: str, library_id: int = None):
    """The completed jobs behind one performance-chart segment."""
    if attribute != "encoder_used":
        raise HTTPException(400, f"Can't drill into '{attribute}'.")
    return {"jobs": db.jobs_matching(value, library_id)}


@app.post("/api/stats/queue")
async def stats_queue(req: Request):
    """Queue specific files picked from a stats drill-down, with ad-hoc
    audio/video overrides — for standardizing a subset of a library
    (every EAC3 file, say) without changing what the rest of that
    library does on its own.

    Each file keeps its own library's other settings — naming, subtitle
    handling, HDR, everything not explicitly overridden here — by
    starting from that library's normally-resolved spec and only
    replacing the fields the person actually chose to change.
    """
    body = await req.json()
    paths = body.get("paths") or []
    if not paths:
        raise HTTPException(400, "No files selected.")

    overrides = {}
    if body.get("convert_audio"):
        if not body.get("audio_codec"):
            raise HTTPException(400, "Choose an audio codec.")
        overrides["audio"] = body["audio_codec"]
        if body.get("audio_bitrate"):
            overrides["audio_bitrate"] = body["audio_bitrate"]
    if body.get("convert_video"):
        if not body.get("video_codec"):
            raise HTTPException(400, "Choose a video codec.")
        overrides["codec"] = body["video_codec"]
        if body.get("quality"):
            overrides["quality"] = int(body["quality"])
    if body.get("container"):
        overrides["container"] = body["container"]
    if not overrides:
        raise HTTPException(400, "Choose at least one thing to convert.")

    libraries = db.list_libraries()
    library_for = db.library_matcher(libraries)

    queued, skipped = 0, []
    for path in paths:
        lib = library_for(path)
        if not lib:
            skipped.append({"path": path, "reason": "not part of any configured library"})
            continue
        if not Path(path).is_file():
            skipped.append({"path": path, "reason": "file no longer exists"})
            continue
        spec = {**profiles.resolve(lib.get("profile") or {}), **overrides}
        cached = db.get_cached_file(path)
        size_before = (cached or {}).get("size")
        new_id = db.enqueue(path, spec, size_before, lib["id"])
        if new_id:
            queued += 1
        else:
            skipped.append({"path": path, "reason": "already queued or in progress"})

    if queued:
        await broadcast()
    return {"queued": queued, "skipped": skipped}


@app.post("/api/scan")
async def scan():
    """Walk the media roots and cache a probe for anything new."""
    found = 0
    for root in MEDIA_ROOTS:
        for path in Path(root).rglob("*"):
            if path.suffix.lower() not in VIDEO_EXT or not path.is_file():
                continue
            info = probe(str(path))
            if info:
                db.cache_probe(str(path), info)
                found += 1
    return {"scanned": found}


@app.get("/api/library")
async def library(limit: int = 500):
    with db.connect() as conn:
        rows = conn.execute(
            "SELECT * FROM files ORDER BY size DESC LIMIT ?", (limit,)
        ).fetchall()
    return [db.row_to_dict(r) for r in rows]


@app.post("/api/queue")
async def queue(req: Request):
    body = await req.json()
    spec = body.get("spec") or {"codec": "hevc", "quality": 22,
                                "audio": "copy", "container": "mkv"}
    added, skipped = [], []
    for path in body.get("paths", []):
        info = probe(path)
        job_id = db.enqueue(path, spec, info["size"] if info else None)
        (added if job_id else skipped).append(path)
    await broadcast()
    return {"added": len(added), "skipped": len(skipped)}


@app.post("/api/jobs/{job_id}/cancel")
async def cancel(job_id: int):
    db.update_job(job_id, state="cancelled", finished_at=time.time())
    await broadcast()
    return {"ok": True}


@app.post("/api/naming/preview")
async def naming_preview(req: Request):
    """Show what a filename would become, before committing to it."""
    body = await req.json()
    scheme = body.get("scheme", "jellyfin")
    container = body.get("container", "mkv")
    folders = body.get("folders", True)
    settings = db.get_settings()
    tmdb = settings.get("tmdb") or {}
    credential = tmdb.get("key") if (tmdb.get("enabled")
                                     and body.get("use_lookup", True)) else None
    results = await asyncio.to_thread(
        lambda: [naming.preview(n, scheme, container, folders, credential)
                 for n in (body.get("names") or [])[:12]])
    return {"results": results, "used_lookup": bool(credential)}


@app.post("/api/lookup/test")
async def lookup_test(req: Request):
    body = await req.json()
    key = body.get("key") or (db.get_settings().get("tmdb") or {}).get("key")
    ok, message = await asyncio.to_thread(lookup.TMDB(key).test)
    return {"ok": ok, "message": message, "attribution": lookup.ATTRIBUTION}


@app.post("/api/arr/test")
async def arr_test(req: Request):
    """Used by the wizard's "Test connection" button before saving a library."""
    body = await req.json()
    kind = body.get("kind")
    if kind not in ("radarr", "sonarr"):
        raise HTTPException(400, "kind must be radarr or sonarr")
    url, key = body.get("url", ""), body.get("api_key", "")
    if not url or not key:
        return {"ok": False, "message": "Enter an address and API key first."}
    ok, message = await asyncio.to_thread(arr.test_connection, kind, url, key)
    return {"ok": ok, "message": message}


@app.get("/api/naming/samples")
async def naming_samples(library_id: int = None, limit: int = 6):
    """Real filenames from a watch folder, for a preview that means something."""
    names = []
    libs = [db.get_library(library_id)] if library_id else db.list_libraries()
    for lib in [l for l in libs if l]:
        for path in watcher.walk_library(lib["watch_path"]):
            names.append(path.name)
            if len(names) >= limit:
                return {"names": names}
    return {"names": names}


@app.get("/api/catalog")
async def catalog():
    """Everything the setup wizard needs to describe the options."""
    return profiles.catalog()


@app.post("/api/profile/check")
async def profile_check(req: Request):
    return {"warnings": profiles.warnings_for(await req.json())}


@app.get("/api/browse")
async def browse(path: str = ""):
    """List folders so the wizard can offer a picker instead of a text box."""
    target = Path(path) if path else Path(MEDIA_ROOTS[0] if MEDIA_ROOTS else "/")
    if not target.is_dir():
        raise HTTPException(400, f"Not a folder: {target}")
    try:
        folders = sorted(
            [str(c) for c in target.iterdir()
             if c.is_dir() and not c.name.startswith(".")]
        )
    except PermissionError:
        raise HTTPException(403, f"No permission to read {target}")
    return {"path": str(target), "parent": str(target.parent),
            "folders": folders}


@app.get("/api/stats/detailed")
async def stats_detailed():
    """Library composition and transcode performance, for the Stats tab.

    Heavier than the summary figures pushed on every websocket broadcast —
    this scans the whole probe cache and job history — so it's fetched on
    demand when that tab is actually opened, not pushed continuously.
    """
    return {
        "composition": await asyncio.to_thread(db.library_composition),
        "performance": await asyncio.to_thread(db.transcode_performance),
    }


@app.get("/api/libraries")
async def get_libraries():
    return db.list_libraries()


def check_originals_folder(body, watch):
    """Validate a chosen originals folder, if there is one.

    Runs for every library, including ones converting in place — that's the
    case where it matters most, since the default would otherwise put
    originals inside the folder your media server scans.
    """
    chosen = (body.get("originals_path") or "").strip()
    if not chosen or body.get("original_action", "archive") != "archive":
        return
    originals = Path(chosen).expanduser()
    try:
        originals.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Can't use {originals} for originals: {exc}")
    if not os.access(originals, os.W_OK):
        raise HTTPException(400, f"Forge can't write to {originals}.")
    if originals == watch or watch in originals.parents:
        raise HTTPException(
            400, "The originals folder is inside the folder being watched, so "
                 "those files would be found and converted over and over. "
                 "Put it somewhere outside it.")


def validate_library(body, existing_id=None):
    """Reject setups that would fail confusingly later."""
    for field in ("name", "watch_path", "profile"):
        if not body.get(field):
            raise HTTPException(400, f"Please fill in the {field.replace('_', ' ')}.")

    name = body["name"].strip()
    for library in db.list_libraries():
        if library["id"] != existing_id and library["name"].lower() == name.lower():
            raise HTTPException(
                400, f'A library called "{library["name"]}" already exists. '
                     "Pick a different name.")

    watch = Path(body["watch_path"]).expanduser()
    if not watch.is_dir():
        raise HTTPException(400, f"That watch folder doesn't exist: {watch}")

    check_originals_folder(body, watch)

    out = body.get("output_path")
    if not out:
        return watch, None

    output = Path(out).expanduser()
    if output == watch:
        raise HTTPException(
            400, "The watch folder and destination are the same. Leave the "
                 "destination empty if you want files converted where they are.")
    # An output folder inside the watch folder means finished files land back
    # in the folder being watched, and get picked up again.
    if watch in output.parents:
        raise HTTPException(
            400, f"The destination sits inside the watch folder, so converted "
                 f"files would be found and converted again. Move it outside "
                 f"{watch}.")
    if output in watch.parents:
        raise HTTPException(
            400, "The watch folder is inside the destination. Pick folders "
                 "that don't contain each other.")

    for library in db.list_libraries():
        if library["id"] == existing_id:
            continue
        other = Path(library["watch_path"])
        if other == watch:
            raise HTTPException(
                400, f'"{library["name"]}" already watches that folder.')

    # Archived originals go beside the destination, so that folder's parent
    # has to be writable. Picking the top of a mounted share puts them
    # outside it, which in a container means the read-only root filesystem —
    # and the failure would otherwise appear only after a file was encoded.
    if body.get("original_action", "archive") == "archive":
        parent = output.parent
        if str(parent) in ("/", ""):
            raise HTTPException(
                400, "Pick a folder inside your media share rather than the "
                     "top of it — Forge keeps originals alongside the "
                     "destination, and there's nowhere above this to put "
                     "them. For example /media/Movies rather than /media.")
        if not os.access(parent, os.W_OK):
            raise HTTPException(
                400, f"Forge can't write to {parent}, where it would keep "
                     f"originals. Check the folder is mounted and writable, "
                     f"or choose a destination further inside your share.")
    return watch, output


@app.post("/api/libraries")
async def add_library(req: Request):
    body = await req.json()
    watch, output = validate_library(body)
    body["watch_path"] = str(watch)
    out = str(output) if output else None
    if output:
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Couldn't create the destination: {exc}")
    try:
        lib_id = db.create_library(
            body["name"].strip(), body["watch_path"], out, body["profile"],
            body.get("original_action", "archive"),
            body.get("mirror_folders", True),
            body.get("skip_matching", True),
            body.get("filters") or {},
            body.get("naming") or {},
            body.get("originals_path") or None,
        )
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400, f"Could not create that library: {exc}")

    # Don't make the user wait for the next tick to see something happen —
    # but this is a convenience, not part of "did the save work." The
    # library is already committed above; a scan hiccup here (a bad file,
    # a flaky mount) must never turn a successful save into a false
    # "couldn't save" error. Worst case, the next scheduled scan picks up
    # the folder instead of this one happening immediately.
    report = {}
    try:
        report = await asyncio.to_thread(watcher.scan_library,
                                         db.get_library(lib_id), probe)
    except Exception as exc:
        print(f"add_library: initial scan of library {lib_id} failed ({exc})")
    await broadcast()
    return {"id": lib_id, "first_scan": report}


@app.patch("/api/libraries/{lib_id}")
async def edit_library(lib_id: int, req: Request):
    body = await req.json()
    current = db.get_library(lib_id)
    if not current:
        raise HTTPException(404, "No such library")
    # Only validate when the fields that matter are actually changing.
    if {"name", "watch_path", "output_path"} & set(body):
        merged = {**current, **body}
        validate_library(merged, existing_id=lib_id)
    allowed = {"name", "watch_path", "output_path", "profile", "filters", "naming",
               "original_action", "mirror_folders", "skip_matching", "enabled",
               "originals_path"}
    fields = {k: v for k, v in body.items() if k in allowed}
    if "name" in fields:
        fields["name"] = str(fields["name"]).strip()
    if fields.get("output_path"):
        try:
            Path(fields["output_path"]).expanduser().mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(400, f"Couldn't create the destination: {exc}")
    try:
        db.update_library(lib_id, **fields)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400, f"Could not save that: {exc}")
    await broadcast()
    return {"ok": True}


@app.delete("/api/libraries/{lib_id}")
async def remove_library(lib_id: int):
    db.delete_library(lib_id)
    await broadcast()
    return {"ok": True}


def log_filed(library, report):
    """Give moved-untouched files a row so they don't move in silence."""
    for source, message in report.get("conflicts") or []:
        # One row per stuck file, not one per scan.
        if db.has_job_for(source):
            continue
        job_id = db.enqueue(source, {"codec": "copy", "action": "filed",
                                     "quality": 0}, None, library["id"])
        if job_id:
            db.update_job(
                job_id, state="failed", finished_at=time.time(),
                error=f"Needs no conversion, but couldn't be filed: {message}. "
                      f"Two releases of the same title end up with the same "
                      f"name. Remove or rename one, then remove this entry and "
                      f"Forge will try again.")

    for source, destination, size in report.get("filed") or []:
        spec = {"codec": "copy", "audio": "copy", "action": "filed",
                "quality": 0, "container": Path(destination).suffix.lstrip(".")}
        job_id = db.enqueue(source, spec, size, library["id"])
        if job_id:
            db.update_job(job_id, state="done", progress=100,
                          size_after=size, final_path=destination,
                          encoder_used="none",
                          outcome="already in the right format — renamed and "
                                  "filed without converting",
                          finished_at=time.time())


@app.post("/api/libraries/{lib_id}/scan")
async def scan_library_now(lib_id: int):
    library = db.get_library(lib_id)
    if not library:
        raise HTTPException(404, "No such library")
    report = await asyncio.to_thread(watcher.scan_library, library, probe)
    log_filed(library, report)
    await broadcast()
    return report


def _settings_payload(settings):
    """Settings plus the plain-language summaries the panel shows.

    Built in one place because two endpoints return this and they drifted
    apart last time they each built it themselves.
    """
    conf = settings.get("originals") or {}
    stamp = (settings.get("originals_state") or {}).get("last_run")
    last_run = datetime.fromisoformat(stamp) if stamp else None
    _due, why = schedule.cleanup_due(conf, last_run)
    return {**settings,
            "schedule_text": schedule.describe(settings),
            "open_now": schedule.is_open(settings),
            "originals_text": schedule.describe_cleanup(conf),
            "cleanup_text": schedule.describe_cleanup(conf),
            "originals_next": why,
            "originals_last_run": stamp,
            "auto_fail_text": schedule.describe_auto_fail(
                settings.get("auto_fail") or {})}


@app.get("/api/settings")
async def read_settings():
    settings = db.get_settings()
    return _settings_payload(settings)


@app.put("/api/settings")
async def write_settings(req: Request):
    db.save_settings(await req.json())
    await broadcast()
    settings = db.get_settings()
    return _settings_payload(settings)


@app.post("/api/originals/sweep")
async def sweep_now(force: bool = False):
    """Manual Originals cleanup. force ignores the age requirement."""
    settings = db.get_settings()
    if force:
        settings = {**settings,
                    "originals": {**settings.get("originals", {}),
                                  "enabled": True, "after_days": 0}}
    result = await asyncio.to_thread(watcher.sweep_originals, settings)
    await broadcast()
    return result


@app.get("/api/originals")
async def read_originals():
    rows = db.list_originals()
    for row in rows:
        row["replacement_ok"] = bool(
            row["final_path"] and Path(row["final_path"]).is_file())
    return rows


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: int):
    job = db.get_job(job_id)
    if not job:
        raise HTTPException(404, "No such job")

    # Only one live job per file. If this path is already waiting or running,
    # putting this one back would break that rule — and the file is going to
    # be converted anyway, so the stale entry is simply cleared.
    if db.has_job_for(job["path"], list(db.ACTIVE_STATES)):
        db.delete_job(job_id)
        await broadcast()
        return {"ok": True, "removed": True,
                "message": "That file is already in the queue, so this old "
                           "entry has been cleared."}

    try:
        db.update_job(job_id, state="queued", node_id=None, lease_expires=None,
                      progress=0, fps=0, speed=0, error=None, outcome=None)
    except sqlite3.IntegrityError as exc:
        raise HTTPException(400, f"Couldn't queue that again: {exc}")
    await broadcast()
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    listeners.add(ws)
    try:
        try:
            await ws.send_text(json.dumps(await build_state()))
        except Exception as exc:
            # Never let a bad state payload close the socket on connect:
            # that turns one broken field into a dead interface.
            print(f"websocket: could not send the first update ({exc})")
            await ws.send_text(json.dumps(
                {"error": str(exc),
                 "module_problems": getattr(app.state, "module_problems", [])}))
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        listeners.discard(ws)


@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    """Browsers request this from the root whatever the page links to."""
    return FileResponse(STATIC / "favicon.ico")


@app.get("/", response_class=HTMLResponse)
async def index():
    return (STATIC / "index.html").read_text()


app.mount("/static", StaticFiles(directory=STATIC), name="static")
