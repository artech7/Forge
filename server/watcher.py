"""Watch folders.

Polling, not inotify — inotify doesn't work over SMB or NFS, which is
where most of this media actually lives.

The important subtlety is that a file appearing is not a file that has
finished arriving. A 40 GB remux copied over the network shows up
instantly at zero bytes and grows for ten minutes. So a file must hold
the same size across two consecutive scans before it is queued.
"""
import shutil
import time
from pathlib import Path

import db
import naming
import profiles

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".wmv", ".ts", ".m2ts",
             ".mpg", ".mpeg", ".flv", ".webm"}
SCAN_SECONDS = 30
# A file untouched for this long is finished arriving — no need to hold it
# for a second scan. Without this, a folder of existing files sits idle for
# a full cycle before anything happens.
SETTLED_AGE = 90
SKIP_DIRS = {"originals", "@eadir", ".@__thumb", "#recycle", ".git"}


def is_video(path: Path):
    return path.suffix.lower() in VIDEO_EXT and not path.name.startswith(".")


def walk_library(watch_path: str):
    root = Path(watch_path)
    if not root.is_dir():
        return
    for path in root.rglob("*"):
        if any(part.lower() in SKIP_DIRS for part in path.parts):
            continue
        if path.is_file() and is_video(path):
            yield path


FILTER_FIELDS = {
    "skip_extensions": [],      # ["mp4", "avi"] — don't touch these
    "only_extensions": [],      # if set, ignore everything else
    "min_size_mb": None,
    "max_size_mb": None,
    "min_minutes": None,
    "max_minutes": None,
    "skip_name_contains": [],   # case-insensitive substrings
    "only_name_contains": [],
    "skip_video_codecs": [],    # ["hevc", "av1"] — ignore these entirely
    "bitrate_ceiling": {},      # {enabled, sd, hd, fullhd, uhd} in kbps
    "min_bitrate_kbps": None,   # ignore files already below this
    "max_bitrate_kbps": None,
}


def filter_verdict(path, size, info, filters):
    """Return None to process, or a plain-language reason to skip.

    Any single matching skip rule wins. The 'only' rules work the other
    way: if set, a file must match at least one to be considered at all.
    """
    if not filters:
        return None
    name = path.name.lower()
    ext = path.suffix.lower().lstrip(".")

    only_ext = [e.lower().lstrip(".") for e in filters.get("only_extensions") or []]
    if only_ext and ext not in only_ext:
        return f"not one of {', '.join(only_ext)}"

    skip_ext = [e.lower().lstrip(".") for e in filters.get("skip_extensions") or []]
    if ext in skip_ext:
        return f"{ext} files are set to be skipped"

    only_name = [t.lower() for t in filters.get("only_name_contains") or []]
    if only_name and not any(t in name for t in only_name):
        return "name doesn't match the required text"

    for term in filters.get("skip_name_contains") or []:
        if term.lower() in name:
            return f"name contains \"{term}\""

    mb = (size or 0) / (1024 * 1024)
    if filters.get("min_size_mb") and mb < float(filters["min_size_mb"]):
        return f"smaller than {filters['min_size_mb']} MB"
    if filters.get("max_size_mb") and mb > float(filters["max_size_mb"]):
        return f"larger than {filters['max_size_mb']} MB"

    minutes = ((info or {}).get("duration") or 0) / 60
    if minutes:
        if filters.get("min_minutes") and minutes < float(filters["min_minutes"]):
            return f"shorter than {filters['min_minutes']} minutes"
        if filters.get("max_minutes") and minutes > float(filters["max_minutes"]):
            return f"longer than {filters['max_minutes']} minutes"

    kbps = ((info or {}).get("video_bitrate") or 0) / 1000
    if kbps:
        if filters.get("min_bitrate_kbps") and kbps < float(filters["min_bitrate_kbps"]):
            return f"already efficient ({kbps:.0f} kbps)"
        if filters.get("max_bitrate_kbps") and kbps > float(filters["max_bitrate_kbps"]):
            return f"bitrate above {filters['max_bitrate_kbps']} kbps"

    codec = (info or {}).get("video_codec")
    skip_codecs = [c.lower() for c in filters.get("skip_video_codecs") or []]
    if codec and codec.lower() in skip_codecs:
        return f"already {codec} (skipped entirely — audio not checked)"

    return None


# A bitrate that's fine for 720p is wasteful at the same number for 4K, so
# thresholds are per resolution tier rather than one global number.
DEFAULT_BITRATE_CEILING = {"sd": 1500, "hd": 3000, "fullhd": 6000, "uhd": 18000}


def tier_for(info):
    height = (info or {}).get("height") or 0
    if height >= 1600:
        return "uhd"
    if height >= 1000:
        return "fullhd"
    if height >= 700:
        return "hd"
    return "sd"


def video_is_efficient(info, filters):
    """True if the picture is already small enough to leave alone.

    Only consulted when the codec already matches the target — a bloated
    H.265 file still gets re-encoded, which is the case a plain codec
    filter misses entirely.
    """
    conf = (filters or {}).get("bitrate_ceiling") or {}
    if not conf.get("enabled"):
        return True                     # not checking bitrate at all
    kbps = ((info or {}).get("video_bitrate") or 0) / 1000
    if not kbps:
        return True                     # unknown; don't re-encode on a guess
    ceiling = float(conf.get(tier_for(info)) or DEFAULT_BITRATE_CEILING[tier_for(info)])
    return kbps <= ceiling


def plan_conversion(path, info, spec, filters):
    """Decide what actually needs doing to this file.

    Returns (action, adjusted_spec, description). The key case: a file that
    is already the right video codec but has the wrong audio gets its video
    stream copied and only the audio re-encoded. Seconds instead of hours.
    """
    adjusted = dict(spec)

    want_video = spec.get("codec", "hevc")
    have_video = (info or {}).get("video_codec")
    video_ok = (want_video == "copy"
                or (have_video == want_video and video_is_efficient(info, filters)))

    want_audio = spec.get("audio", "aac")
    have_audio = (info or {}).get("audio_codecs") or []
    audio_ok = (want_audio == "copy"
                or (bool(have_audio) and all(c == want_audio for c in have_audio)))

    want_container = spec.get("container", "mkv")
    container_ok = path.suffix.lower().lstrip(".") == want_container.lower()

    if video_ok:
        adjusted["codec"] = "copy"
    if audio_ok:
        adjusted["audio"] = "copy"

    if video_ok and audio_ok and container_ok:
        return "skip", adjusted, "already exactly right"

    if video_ok and audio_ok:
        return "remux", adjusted, f"repackaging into {want_container.upper()}"

    if video_ok:
        return "audio_only", adjusted, (
            f"video is already {have_video}, converting audio "
            f"{'/'.join(sorted(set(have_audio))) or '?'} to {want_audio.upper()}")

    if audio_ok:
        return "video_only", adjusted, (
            f"audio is already {want_audio.upper()}, converting video to "
            f"{want_video.upper()}")

    return "full", adjusted, f"converting to {want_video.upper()} / {want_audio.upper()}"


def scan_library(library, probe_fn):
    """One pass over a library's watch folder. Returns a short report."""
    spec = profiles.resolve(library["profile"])
    filters = library.get("filters") or {}
    now = time.time()
    queued, waiting, skipped, filtered = 0, 0, 0, 0
    reasons = {}
    filed = []          # (source, destination, size) for files moved untouched
    conflicts = []      # (source, destination) where something is already there

    for path in walk_library(library["watch_path"]):
        try:
            stat = path.stat()
        except OSError:
            continue

        if db.was_processed(str(path), stat.st_mtime):
            continue

        # A file that hasn't changed in a while is finished arriving.
        # Anything newer has to prove it by holding its size across scans.
        settled = (now - stat.st_mtime) > SETTLED_AGE
        if not settled and not db.note_pending(str(path), stat.st_size):
            waiting += 1
            continue

        # Cheap filters first, so we don't ffprobe files we'll never touch.
        reason = filter_verdict(path, stat.st_size, None, filters)
        info = None
        if reason is None:
            info = probe_fn(str(path))
            if info:
                db.cache_probe(str(path), info)
            reason = filter_verdict(path, stat.st_size, info, filters)

        if reason is not None:
            db.mark_processed(str(path), stat.st_mtime, stat.st_size, library["id"])
            db.clear_pending(str(path))
            filtered += 1
            reasons[path.name] = reason
            continue

        action, job_spec, why = plan_conversion(path, info, spec, filters)
        if action == "skip" and library["skip_matching"]:
            db.clear_pending(str(path))
            skipped += 1
            # Nothing to convert, but it still belongs in the library.
            moved, result = file_as_is(library, path, spec)
            if moved:
                filed.append((str(path), str(result), stat.st_size))
                try:
                    st = Path(result).stat()
                    db.mark_processed(str(result), st.st_mtime, st.st_size,
                                      library["id"])
                except OSError:
                    pass
            elif isinstance(result, str) and result.startswith("something is"):
                # Two releases of the same film resolve to the same name.
                # Deliberately not marked as handled: once the collision is
                # resolved, the next scan should pick this up. One row is
                # logged so it isn't stranded silently.
                conflicts.append((str(path), result))
            else:
                db.mark_processed(str(path), stat.st_mtime, stat.st_size,
                                  library["id"])
            continue
        if action == "skip":
            job_spec = dict(spec)           # user asked for no skipping at all

        job_spec["action"] = action
        job_spec["why"] = why
        # Recorded now because the library setting can change afterwards, and
        # a message that blames the current setting would be wrong.
        job_spec["original_action"] = library.get("original_action", "archive")
        if db.enqueue(str(path), job_spec, stat.st_size, library["id"]):
            db.clear_pending(str(path))
            queued += 1
            reasons[path.name] = why

    return {"queued": queued, "waiting": waiting, "already_ok": skipped,
            "filtered_out": filtered, "reasons": reasons, "filed": filed,
            "conflicts": conflicts}


def scan_all(probe_fn):
    report = {}
    for library in db.list_libraries():
        if not library["enabled"]:
            continue
        try:
            report[library["name"]] = scan_library(library, probe_fn)
        except Exception as exc:
            report[library["name"]] = {"error": str(exc)[:200]}
    return report


def destination_for(library, source_path: str, container: str):
    """Where the finished file belongs, in server path space."""
    source = Path(source_path)
    if not library.get("output_path"):
        return source.with_suffix("." + container)   # in-place

    out_root = Path(library["output_path"])

    # Renaming replaces the folder structure rather than mirroring it: the
    # media server wants its own layout, not the release's.
    rules = library.get("naming") or {}
    if rules.get("enabled"):
        settings = db.get_settings()
        tmdb = settings.get("tmdb") or {}
        credential = tmdb.get("key") if tmdb.get("enabled") else None
        parsed = naming.resolve(source.name, credential)
        if parsed["confident"] or not rules.get("skip_uncertain", True):
            return out_root / naming.format_path(
                parsed, rules.get("scheme", "jellyfin"), container,
                rules.get("folders", True))

    if library.get("mirror_folders"):
        try:
            relative = source.relative_to(Path(library["watch_path"]))
        except ValueError:
            relative = Path(source.name)
        target = out_root / relative
    else:
        target = out_root / source.name
    return target.with_suffix("." + container)


def originals_dir(library):
    """Where this library's archived sources go.

    A chosen folder wins. Otherwise they sit beside the destination, or —
    when converting in place — inside the watched folder, which is fine for
    a staging area but wrong for a media library your server also scans.
    """
    chosen = (library.get("originals_path") or "").strip()
    if chosen:
        return Path(chosen) / library["name"]
    if library.get("output_path"):
        return Path(library["output_path"]).parent / "Originals" / library["name"]
    return Path(library["watch_path"]) / "Originals"


def sweep_originals(settings):
    """Delete archived originals, but only where the replacement is verified.

    Never trusts the ledger alone: the transcoded file must exist on disk
    and be non-empty before the original is removed. A failed move or a
    deleted output means the original stays put.
    """
    conf = settings.get("originals") or {}
    if not conf.get("enabled"):
        return {"deleted": 0, "kept": 0, "reason": "disabled"}

    age = float(conf.get("after_days", 14)) * 86400
    deleted, freed, kept = 0, 0, 0

    for row in db.list_originals(older_than_seconds=age):
        archived = Path(row["archived_path"])
        final = Path(row["final_path"]) if row["final_path"] else None

        if not archived.exists():
            db.forget_original(row["archived_path"])   # already gone
            continue

        if not final or not final.is_file() or final.stat().st_size == 0:
            kept += 1                                   # replacement missing
            continue

        try:
            freed += archived.stat().st_size
            archived.unlink()
            db.forget_original(row["archived_path"])
            deleted += 1
        except OSError:
            kept += 1

    prune_empty_dirs()
    return {"deleted": deleted, "kept": kept, "freed": freed}


def prune_empty_dirs():
    """Tidy leftover folders in Originals after files are removed."""
    for library in db.list_libraries():
        root = originals_dir(library)
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*"), key=lambda p: -len(p.parts)):
            if path.is_dir():
                try:
                    next(path.iterdir())
                except StopIteration:
                    path.rmdir()
                except OSError:
                    pass


def file_as_is(library, path, spec):
    """Move and rename a file that needs no conversion.

    A file already matching everything the library asks for shouldn't be
    converted — that would only cost quality. But it still needs renaming and
    filing, otherwise it sits in the watch folder indefinitely looking like
    Forge ignored it.

    Returns (ok, destination_or_message).
    """
    if not library.get("output_path"):
        return False, "library converts in place, nothing to move"

    destination = destination_for(library, str(path), path.suffix.lstrip("."))
    if destination == path:
        return False, "already in the right place"

    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            return False, f"something is already at {destination}"
        # Same filesystem is a rename; across filesystems this copies then
        # removes, which is what shutil.move does anyway.
        shutil.move(str(path), str(destination))
    except OSError as exc:
        return False, f"could not move it: {exc}"
    return True, destination


def find_archived(job, library):
    """Locate the archived source for a job.

    The ledger is the fast path, but a file archived more than once, or by an
    older version, may not be indexed against this job. Falling back to the
    expected location on disk means a perfectly restorable file is never
    reported as missing.
    """
    record = db.original_for_job(job["id"])
    if record:
        candidate = Path(record["archived_path"])
        if candidate.is_file():
            return candidate

    if not library or library.get("original_action") != "archive":
        return None

    source = Path(job["path"])
    try:
        relative = source.relative_to(Path(library["watch_path"])).parent
    except ValueError:
        relative = Path(".")
    guess = originals_dir(library) / relative / source.name
    if guess.is_file():
        return guess

    # Last resort: the same filename anywhere under this library's Originals.
    root = originals_dir(library)
    if root.is_dir():
        for found in root.rglob(source.name):
            if found.is_file():
                return found
    return None


def restore_original(job, library):
    """Put the archived source back and remove the conversion.

    Returns (ok, message). Only possible when the library kept originals —
    with 'delete' or 'keep' there is nothing to restore from.
    """
    archived = find_archived(job, library)
    if not archived:
        if (library or {}).get("original_action") == "archive":
            return False, ("this library keeps originals, but the archived copy "
                           "of this file could not be found")
        return False, "this library is not set to keep originals"

    source = Path(job["path"])
    converted = Path(job["final_path"]) if job.get("final_path") else None

    try:
        source.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(archived), str(source))
    except OSError as exc:
        return False, f"could not put the original back: {exc}"

    # Only remove the conversion once the original is safely back.
    if converted and converted.is_file() and converted != source:
        try:
            converted.unlink()
        except OSError:
            pass

    db.forget_original(str(archived))
    db.forget_processed(str(source))
    if converted:
        db.forget_processed(str(converted))
    prune_empty_dirs()
    return True, "original restored"
