#!/usr/bin/env python3
"""Exercise every server function against a throwaway database.

Catches the class of bug where an edit lands in the wrong function — the
code imports and parses fine, then fails at runtime on a specific call.
    python3 check-server.py
"""
import pathlib
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent / "server"))

import db                                        # noqa: E402
db.DB_PATH = pathlib.Path(tempfile.mkdtemp()) / "check.db"

from datetime import datetime as _DT             # noqa: E402
from fastapi import HTTPException                # noqa: E402
import app                                       # noqa: E402
import naming, profiles, schedule, watcher       # noqa: E402
import lookup, scheduler                         # noqa: E402

failures = []


def _run(coroutine):
    """Run one async endpoint from this synchronous script."""
    import asyncio
    return asyncio.run(coroutine)


def _status(coroutine):
    """Run an endpoint expected to reject the request, return its status code."""
    try:
        _run(coroutine)
    except HTTPException as exc:
        return exc.status_code
    return None


def check(label, fn, expect=None):
    try:
        result = fn()
    except Exception as exc:
        failures.append(f"{label}: {type(exc).__name__}: {exc}")
        print(f"  FAILED  {label} -> {type(exc).__name__}: {exc}")
        return None
    if expect and not expect(result):
        failures.append(f"{label}: unexpected result {result!r}")
        print(f"  WRONG   {label} -> {result!r}")
        return result
    print(f"  ok      {label}")
    return result


print("Setting up…")
db.init()
db.migrate()

base = db.DB_PATH.parent
watch = base / "inbox"
watch.mkdir(exist_ok=True)

print("\nDatabase:")
check("get_settings", db.get_settings, lambda r: "schedule" in r)
check("save_settings", lambda: db.save_settings({"scan_seconds": 45}))
check("stats", db.stats, lambda r: "done" in r and "before" in r)
check("originals_summary", db.originals_summary, lambda r: set(r) == {"n", "bytes"})
check("job_counts", db.job_counts, lambda r: set(r) >= {"active", "failed", "done"})
check("count_jobs", lambda: db.count_jobs(["queued"]), lambda r: isinstance(r, int))
check("list_nodes", db.list_nodes, lambda r: isinstance(r, list))
check("list_libraries", db.list_libraries, lambda r: isinstance(r, list))
check("list_originals", db.list_originals, lambda r: isinstance(r, list))
check("repair_profiles", db.repair_profiles, lambda r: isinstance(r, int))

check("upsert_node", lambda: db.upsert_node("n1", "test", ["libx265"],
      [{"server": str(base), "local": str(base)}], 1, {}, {"libx265": 50}, 8))
check("get_node", lambda: db.get_node("n1"), lambda r: r["name"] == "test")
check("node_slots", lambda: db.node_slots("n1"), lambda r: r == 1)
check("set_slots", lambda: db.set_slots("n1", 3), lambda r: r == 3)
check("touch_node", lambda: db.touch_node("n1"))

profile = {"video_codec": "hevc", "container": "mkv", "audio_codec": "aac",
           "audio_bitrate": "160k", "quality_level": "balanced",
           "subtitle_mode": "keep"}
lib_id = check("create_library", lambda: db.create_library(
    "Check", str(watch), str(base / "out"), profile, "archive",
    filters={}, naming={"enabled": True, "scheme": "jellyfin"}),
    lambda r: isinstance(r, int))
check("get_library", lambda: db.get_library(lib_id), lambda r: r["name"] == "Check")
check("update_library", lambda: db.update_library(lib_id, enabled=0))

spec = profiles.resolve(profile)
job_id = check("enqueue", lambda: db.enqueue("/m/a.mkv", spec, 10**9, lib_id),
               lambda r: isinstance(r, int))
check("get_job", lambda: db.get_job(job_id), lambda r: r["state"] == "queued")
check("update_job", lambda: db.update_job(job_id, progress=50))
check("list_jobs paged", lambda: db.list_jobs(["queued"], 10, 0), lambda r: len(r) == 1)
check("record_completion", lambda: db.record_completion(10**9, 4 * 10**8),
      lambda r: r["files"] == 1)
check("stats after completion", db.stats, lambda r: r["before"] == 10**9)
check("cache_probe", lambda: db.cache_probe("/m/a.mkv", {
    "size": 10**9, "duration": 60.0, "video_codec": "h264",
    "audio_codecs": ["ac3"], "width": 1920, "height": 1080,
    "bitrate": 8_000_000, "video_bitrate": 7_000_000}))
check("mark_processed", lambda: db.mark_processed("/m/a.mkv", 1.0, 10**9, lib_id))
check("was_processed", lambda: db.was_processed("/m/a.mkv", 1.0, 10**9), lambda r: r is True)
check("was_processed rejects a same-mtime, different-size file",
      lambda: db.was_processed("/m/a.mkv", 1.0, 123), lambda r: r is False)
check("note_pending", lambda: db.note_pending("/m/b.mkv", 100))
check("clear_pending", lambda: db.clear_pending("/m/b.mkv"))
check("record_original", lambda: db.record_original(
    "/o/a.mkv", job_id, lib_id, "/out/a.mkv", 10**9))
check("originals_summary with rows", db.originals_summary, lambda r: r["n"] == 1)
check("forget_original", lambda: db.forget_original("/o/a.mkv"))
check("requeue_jobs", lambda: db.requeue_jobs(["failed"]), lambda r: r == (0, 0))
check("delete_job", lambda: db.delete_job(job_id))
check("delete_jobs", lambda: db.delete_jobs(["done"]), lambda r: isinstance(r, int))
check("delete_library", lambda: db.delete_library(lib_id))

print("\nBulk actions stay scoped to an active search:")
# A "Retry all N" clicked after searching must only touch the N jobs the
# search matched, not every job in the view — this used to silently act
# on everything regardless of what was actually on screen.
_bulk_lib = db.create_library("BulkTest", "/bulk", None, {}, "archive")
for i in range(3):
    jid = db.enqueue(f"/bulk/Other Movie {i}.mkv", {"codec": "hevc"}, 1000, _bulk_lib)
    db.update_job(jid, state="ignored", error="x")
for i in range(2):
    jid = db.enqueue(f"/bulk/Divergent (2014) - part{i}.mkv", {"codec": "hevc"}, 1000, _bulk_lib)
    db.update_job(jid, state="ignored", error="x")
check("requeue_jobs with q only moves the matching jobs", lambda: (
      db.requeue_jobs(["ignored"], _bulk_lib, "Divergent"),
      db.count_jobs(["ignored"], _bulk_lib),
      db.count_jobs(["queued"], _bulk_lib))[1:],
      lambda r: r == (3, 2))
check("delete_jobs with q only removes the matching jobs", lambda: (
      db.delete_jobs(["ignored"], _bulk_lib, "Other"),
      db.count_jobs(["ignored"], _bulk_lib))[-1],
      lambda r: r == 0)
db.delete_library(_bulk_lib)

print("\nManual queue order:")
_qo_a = db.enqueue("/queueorder/A.mkv", {"codec": "copy"}, 1000)
_qo_b = db.enqueue("/queueorder/B.mkv", {"codec": "copy"}, 1000)
_qo_c = db.enqueue("/queueorder/C.mkv", {"codec": "copy"}, 1000)
check("natural order is oldest first", lambda: [j["id"] for j in db.list_jobs(["queued"])
      if j["id"] in (_qo_a, _qo_b, _qo_c)], lambda r: r == [_qo_a, _qo_b, _qo_c])
check("move_job_to_top beats everything else waiting", lambda: (
      db.move_job_to_top(_qo_c),
      [j["id"] for j in db.list_jobs(["queued"])
       if j["id"] in (_qo_a, _qo_b, _qo_c)])[-1],
      lambda r: r == [_qo_c, _qo_a, _qo_b])
check("reorder_jobs sets the exact sequence given", lambda: (
      db.reorder_jobs([_qo_b, _qo_a, _qo_c]),
      [j["id"] for j in db.list_jobs(["queued"])
       if j["id"] in (_qo_a, _qo_b, _qo_c)])[-1],
      lambda r: r == [_qo_b, _qo_a, _qo_c])
check("the scheduler leases in that same manual order", lambda: (
      db.upsert_node("qo-node", "QONode", ["libx264"],
                     [{"server": "/queueorder", "local": "/queueorder"}], 1),
      scheduler.lease_job("qo-node")["id"])[-1],
      lambda r: r == _qo_b)
db.delete_jobs(["queued"])

print("\nA job that only ever checks in once doesn't bounce forever:")
# A loudness measurement reports progress exactly once (a "started" ping)
# then blocks on one uninterruptible FFmpeg call until it finishes. If that
# call hangs, the lease expires, the reaper bounces it back to queued, and
# it gets re-leased and reports that same single ping again — which must
# NOT look like proof the new attempt is healthy, or it can bounce forever
# without ever tripping BOUNCE_LIMIT.
_zombie = db.enqueue("/zombie/Stuck.mkv", {"measure": "loudness"}, 1000)
db.update_job(_zombie, state="leased", lease_expires=time.time() + 120,
              started_at=time.time())
for cycle in range(scheduler.BOUNCE_LIMIT):
    row = db.get_job(_zombie)
    if row["state"] == "failed":
        break
    scheduler.renew_lease(_zombie, reset_bounces=row["state"] != "leased")
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET state='running', lease_expires=? WHERE id=?",
                     (time.time() - 1, _zombie))
    scheduler.requeue_expired()
    if db.get_job(_zombie)["state"] != "failed":
        with db.connect() as conn:
            conn.execute("UPDATE jobs SET state='leased' WHERE id=?", (_zombie,))
check("a job that only ever checks in once eventually gets given up on",
      lambda: db.get_job(_zombie)["state"], lambda r: r == "failed")

# A job that genuinely keeps working (a second check-in within the same
# attempt) must be completely unaffected by this.
_healthy = db.enqueue("/zombie/Healthy.mkv", {"codec": "hevc"}, 1000)
db.update_job(_healthy, state="leased", lease_expires=time.time() + 120,
              started_at=time.time(), bounces=3)
scheduler.renew_lease(_healthy, reset_bounces=False)   # first check-in
check("a job's first check-in alone doesn't clear its bounces",
      lambda: db.get_job(_healthy)["bounces"], lambda r: r == 3)
db.update_job(_healthy, state="running")
scheduler.renew_lease(_healthy, reset_bounces=True)    # second check-in
check("but a second check-in (genuinely still running) does",
      lambda: db.get_job(_healthy)["bounces"], lambda r: r == 0)
db.delete_jobs(["failed", "leased", "running"])

print("\nReserving housekeeping capacity on a mixed-role node:")
for i in range(6):
    db.enqueue(f"/hk/Backlog {i}.mkv", {"codec": "hevc", "quality": 22}, 1000)
for i in range(3):
    db.enqueue(f"/hk/Loud {i}.mkv", {"measure": "loudness"}, 1000)
db.upsert_node("hk-node", "HKNode", ["libx265"],
               [{"server": "/hk", "local": "/hk"}], 1)
db.set_slots("hk-node", 2)

check("with nothing reserved, both slots go to real work (unchanged default)",
      lambda: sorted("Loud" in (scheduler.lease_job("hk-node") or {"source_path": ""})
                     ["source_path"] for _ in range(2)),
      lambda r: r == [False, False])
db.requeue_jobs(["leased", "running"])   # give the two claimed jobs back

check("set_housekeeping_slots is clamped to the node's own slot count",
      lambda: db.set_housekeeping_slots("hk-node", 99), lambda r: r == 2)
db.set_housekeeping_slots("hk-node", 1)

check("with 1 reserved, exactly one of two concurrent leases is housekeeping",
      lambda: sorted("Loud" in (scheduler.lease_job("hk-node") or {"source_path": ""})
                     ["source_path"] for _ in range(2)),
      lambda r: r == [False, True])
check("a third lease is refused — the node is already at its 2-slot cap",
      lambda: scheduler.lease_job("hk-node"), lambda r: r is None)
db.delete_jobs(["queued", "leased", "running"])

print("\nOther modules:")
check("profiles.catalog", profiles.catalog, lambda r: "video" in r and "naming" in r)
check("profiles.resolve", lambda: profiles.resolve(profile),
      lambda r: r["audio_bitrate"] == "160k")
check("profiles.warnings_for", lambda: profiles.warnings_for(profile),
      lambda r: isinstance(r, list))
check("schedule.is_open", lambda: schedule.is_open(db.get_settings()),
      lambda r: r is True)
check("schedule.describe", lambda: schedule.describe(db.get_settings()),
      lambda r: isinstance(r, str))
check("naming.parse", lambda: naming.parse("Wrath.of.Man.2021.1080p.x265.mkv"),
      lambda r: r["title"] == "Wrath of Man" and r["year"] == 2021)
check("naming.format_path", lambda: str(naming.format_path(
    naming.parse("Wrath.of.Man.2021.1080p.mkv"))),
    lambda r: r == "Wrath of Man (2021)/Wrath of Man (2021).mkv")
check("naming.preview", lambda: naming.preview("x.mkv"), lambda r: "path" in r)
check("lookup.TMDB unconfigured", lambda: lookup.TMDB("").test(),
      lambda r: r[0] is False)
check("lookup.enrich passthrough", lambda: lookup.enrich(
    naming.parse("A.Film.2020.mkv"), None), lambda r: r["looked_up"] is False)
check("scheduler.requeue_expired", scheduler.requeue_expired,
      lambda r: isinstance(r, int))
check("scheduler.resolve_path", lambda: scheduler.resolve_path(
    db.get_node("n1"), str(base / "x.mkv")), lambda r: r[0] == "local")
check("scheduler.reverse_path", lambda: scheduler.reverse_path(
    db.get_node("n1"), str(base / "x.mkv")), lambda r: isinstance(r, str))
check("watcher.filter_verdict", lambda: watcher.filter_verdict(
    pathlib.Path("/m/a.mkv"), 10**9, None, {"skip_extensions": ["mp4"]}),
    lambda r: r is None)
check("watcher.plan_conversion", lambda: watcher.plan_conversion(
    pathlib.Path("/m/a.mkv"), {"video_codec": "hevc", "audio_codecs": ["ac3"]},
    spec, {}), lambda r: r[0] == "audio_only")
check("watcher.scan_all", lambda: watcher.scan_all(lambda p: None),
      lambda r: isinstance(r, dict))
check("watcher.sweep_originals", lambda: watcher.sweep_originals(db.get_settings()),
      lambda r: "deleted" in r or "reason" in r)

print("\nFiling files that need no conversion:")
check("has_job_for", lambda: db.has_job_for("/nothing.mkv"), lambda r: r is False)
check("file_as_is with no destination", lambda: watcher.file_as_is(
      {"output_path": None}, pathlib.Path("/x.mkv"), {}), lambda r: r[0] is False)
check("scan report carries filed and conflicts", lambda: watcher.scan_all(
      lambda p: None), lambda r: isinstance(r, dict))

print("\nOriginals cleanup scheduling:")
check("cleanup off", lambda: schedule.cleanup_due({"enabled": False}),
      lambda r: r[0] is False)
check("cleanup daily", lambda: schedule.cleanup_due(
      {"enabled": True, "mode": "daily", "run_at": "00:00"}),
      lambda r: r[0] is True)
check("cleanup not on this day", lambda: schedule.cleanup_due(
      {"enabled": True, "mode": "days", "run_at": "00:00", "days": []}),
      lambda r: r[0] is False)
check("cleanup interval first run", lambda: schedule.cleanup_due(
      {"enabled": True, "mode": "interval", "interval_hours": 24}),
      lambda r: r[0] is True)
check("describe_cleanup off", lambda: schedule.describe_cleanup({}),
      lambda r: "by hand" in r)
check("describe_cleanup interval", lambda: schedule.describe_cleanup(
      {"enabled": True, "mode": "interval", "interval_hours": 24}),
      lambda r: "every 24 hours" in r)
check("cleanup waits for the set time", lambda: schedule.cleanup_due(
      {"enabled": True, "mode": "daily", "run_at": "03:00"}, None,
      _DT.fromisoformat("2026-08-18 02:00")), lambda r: r[0] is False)
check("cleanup does not repeat the same day", lambda: schedule.cleanup_due(
      {"enabled": True, "mode": "daily", "run_at": "03:00"},
      _DT.fromisoformat("2026-08-18 03:05"),
      _DT.fromisoformat("2026-08-18 04:00")), lambda r: r[0] is False)
check("cleanup interval waits", lambda: schedule.cleanup_due(
      {"enabled": True, "mode": "interval", "interval_hours": 6},
      _DT.fromisoformat("2026-08-18 02:00"),
      _DT.fromisoformat("2026-08-18 04:00")), lambda r: r[0] is False)
check("only one cleanup_due exists", lambda: __import__("inspect").getsource(
      schedule).count("def cleanup_due"), lambda r: r == 1)
check("only one describe_cleanup exists", lambda: __import__("inspect").getsource(
      schedule).count("def describe_cleanup"), lambda r: r == 1)
check("find_archived with nothing", lambda: watcher.find_archived(
      {"id": 9999, "path": "/x.mkv"}, None), lambda r: r is None)
check("record_original re-points the job", lambda: (
      db.record_original("/a/x.mkv", 101, 1, "/b/x.mkv", 10),
      db.record_original("/a/x.mkv", 102, 1, "/b/x.mkv", 10),
      db.original_for_job(102) is not None)[-1], lambda r: r is True)

print("\nPath mapping across platforms:")
_WIN = {"mounts": [{"server": "/media", "local": "Z:/Media"}]}
_UNC = {"mounts": [{"server": "/media", "local": "//nas/media"}]}
_NIX = {"mounts": [{"server": "/media", "local": "/mnt/nas/media"}]}
check("Windows drive letter maps home", lambda: scheduler.reverse_path(
      _WIN, "Z:\\Media\\Movies\\.forge-7.mkv"),
      lambda r: r == "/media/Movies/.forge-7.mkv")
check("Windows UNC path maps home", lambda: scheduler.reverse_path(
      _UNC, "\\\\nas\\media\\Movies\\.forge-7.mkv"),
      lambda r: r == "/media/Movies/.forge-7.mkv")
check("a mount written with backslashes still matches", lambda:
      scheduler.reverse_path({"mounts": [{"server": "/media",
                                          "local": "Z:\\Media"}]},
                             "Z:\\Media\\a.mkv"),
      lambda r: r == "/media/a.mkv")
check("Unix still works", lambda: scheduler.reverse_path(
      _NIX, "/mnt/nas/media/Movies/.forge-7.mkv"),
      lambda r: r == "/media/Movies/.forge-7.mkv")
check("forward mapping to Windows", lambda: scheduler.resolve_path(
      _WIN, "/media/Movies/a.mkv"),
      lambda r: r == ("local", "Z:/Media/Movies/a.mkv"))
check("an unmapped path is streamed", lambda: scheduler.resolve_path(
      _WIN, "/other/a.mkv")[0], lambda r: r == "stream")

# A node with one mount nested inside another must match the more specific
# one regardless of registration order, or files under the nested mount
# resolve through the coarser (wrong) local path.
_NESTED = {"mounts": [{"server": "/media", "local": "Z:/Media"},
                      {"server": "/media/4k", "local": "D:/4K"}]}
check("nested mount matches the more specific prefix", lambda: scheduler.resolve_path(
      _NESTED, "/media/4k/movie.mkv"),
      lambda r: r == ("local", "D:/4K/movie.mkv"))
check("the coarser mount still matches outside the nested one", lambda:
      scheduler.resolve_path(_NESTED, "/media/movie.mkv"),
      lambda r: r == ("local", "Z:/Media/movie.mkv"))
check("reverse mapping also prefers the more specific mount", lambda:
      scheduler.reverse_path(_NESTED, "D:\\4K\\movie.mkv"),
      lambda r: r == "/media/4k/movie.mkv")

print("\nWhere originals go:")
check("in place, no choice", lambda: str(watcher.originals_dir(
      {"name": "Movies", "watch_path": "/media/Movies", "output_path": None})),
      lambda r: r == "/media/Movies/Originals")
check("in place, chosen folder", lambda: str(watcher.originals_dir(
      {"name": "Movies", "watch_path": "/media/Movies", "output_path": None,
       "originals_path": "/originals"})), lambda r: r == "/originals/Movies")
check("staged, chosen folder", lambda: str(watcher.originals_dir(
      {"name": "TV", "watch_path": "/in", "output_path": "/media/TV",
       "originals_path": "/originals"})), lambda r: r == "/originals/TV")
check("a blank choice falls back", lambda: str(watcher.originals_dir(
      {"name": "TV", "watch_path": "/in", "output_path": "/media/TV",
       "originals_path": "  "})), lambda r: r == "/media/Originals/TV")

print("\nDisposing of the original once the new file is placed:")
_orig_dir = pathlib.Path(tempfile.mkdtemp())
_no_lib_source = _orig_dir / "no-library.mkv"
_no_lib_source.write_bytes(b"x")
_no_lib_final = _orig_dir / "no-library.mp4"
_no_lib_final.write_bytes(b"y")
# /api/queue jobs have no library, so original_action defaults to
# "archive" with nowhere to archive into — that must leave the file
# alone, not quietly delete someone's source video.
check("archive with no library leaves the original alone", lambda: (
      app.handle_original({"id": 1, "path": str(_no_lib_source)},
                          None, _no_lib_final),
      _no_lib_source.exists())[-1], lambda r: r is True)

_delete_source = _orig_dir / "delete-me.mkv"
_delete_source.write_bytes(b"x")
_delete_final = _orig_dir / "delete-me.mp4"
_delete_final.write_bytes(b"y")
check("an explicit delete action still removes the original", lambda: (
      app.handle_original({"id": 2, "path": str(_delete_source)},
                          {"original_action": "delete"}, _delete_final),
      _delete_source.exists())[-1], lambda r: r is False)

print("\nReworking an already-converted file without losing the true original:")
_rw_dir = pathlib.Path(tempfile.mkdtemp())
_rw_lib = {"id": 9501, "name": "Rework", "watch_path": str(_rw_dir),
          "original_action": "archive"}
_rw_baseline_n = db.originals_summary()["n"]

# The common Radarr/Sonarr-integrated setup: converts in place, no rename,
# same container in and out — so source and final are the exact same path.
# Before the fix this was never archived at all: handle_original() ran
# after the new file had already overwritten it.
_samepath = _rw_dir / "Show - S01E01.mkv"
_samepath.write_bytes(b"true original bytes")
check("an in-place, same-path conversion still archives the original",
      lambda: (app.handle_original({"id": 9601, "path": str(_samepath)},
                                   _rw_lib, _samepath),
               # Stand in for the os.replace(staged, final) that real
               # completion does immediately after handle_original() —
               # without it, nothing simulates the converted file actually
               # landing back at this path.
               _samepath.write_bytes(b"job 1's converted output"),
               db.original_for_path(str(_samepath)))[-1],
      lambda r: r is not None and pathlib.Path(r["archived_path"]).read_bytes()
                == b"true original bytes")

# Now rework that same (already-converted, already-archived) file again —
# a loudness pass, a retranscode, another remux. This must find the
# existing archive by lineage and update it, not create a second entry
# that clobbers the one true original or gets lost because the container
# changed.
_reworked_final = _rw_dir / "Show - S01E01.mp4"
check("reworking it again doesn't touch the true original's bytes",
      lambda: (app.handle_original({"id": 9602, "path": str(_samepath)},
                                   _rw_lib, _reworked_final),
               db.original_for_job(9602))[-1],
      lambda r: (r is not None
                 and r["final_path"] == str(_reworked_final)
                 and pathlib.Path(r["archived_path"]).read_bytes()
                     == b"true original bytes"))
check("...and there's still only one archived copy, not two",
      lambda: db.originals_summary()["n"] - _rw_baseline_n, lambda r: r == 1)

# A second, unrelated file that happens to share an archived name must
# never silently overwrite the first one.
_dupe_dir = pathlib.Path(tempfile.mkdtemp())
_dupe_lib = {"id": 9502, "name": "Dupes", "watch_path": str(_dupe_dir),
            "original_action": "archive"}
_dupe_a = _dupe_dir / "Same Name.mkv"
_dupe_a.write_bytes(b"first release")
app.handle_original({"id": 9701, "path": str(_dupe_a)}, _dupe_lib,
                    _dupe_dir / "Same Name.mp4")
_dupe_b = _dupe_dir / "Same Name.mkv"
_dupe_b.write_bytes(b"second release, different file")
check("a second archive of the same name doesn't clobber the first",
      lambda: (app.handle_original({"id": 9702, "path": str(_dupe_b)},
                                   _dupe_lib, _dupe_dir / "Same Name.mp4"),
               [p.read_bytes() for p in
                (watcher.originals_dir(_dupe_lib)).glob("Same Name*")])[-1],
      lambda r: sorted(r) == [b"first release", b"second release, different file"])

print("\nGiving up on stuck jobs:")
_AF = {"enabled": True, "amount": 2, "unit": "hours",
       "stall_enabled": True, "stall_minutes": 30}
_NOW = time.time()
check("off means never", lambda: schedule.overrun_reason(
      {"state": "running", "started_at": 0}, {"enabled": False}),
      lambda r: r is None)
check("a healthy job is left alone", lambda: schedule.overrun_reason(
      {"state": "running", "started_at": _NOW - 600,
       "progress_at": _NOW - 30}, _AF), lambda r: r is None)
check("an overrunning job is failed", lambda: schedule.overrun_reason(
      {"state": "running", "started_at": _NOW - 9000,
       "progress_at": _NOW - 30}, _AF), lambda r: r and "longer than" in r)
check("a stalled job is failed", lambda: schedule.overrun_reason(
      {"state": "running", "started_at": _NOW - 3000,
       "progress_at": _NOW - 2700}, _AF), lambda r: r and "no progress" in r)
check("a waiting job is never failed", lambda: schedule.overrun_reason(
      {"state": "queued", "started_at": None}, _AF), lambda r: r is None)
check("a finished job is never failed", lambda: schedule.overrun_reason(
      {"state": "done", "started_at": 0}, _AF), lambda r: r is None)
check("stall check can be turned off alone", lambda: schedule.overrun_reason(
      {"state": "running", "started_at": _NOW - 3000, "progress_at": _NOW - 2700},
      {**_AF, "stall_enabled": False}), lambda r: r is None)
check("minutes unit", lambda: schedule.limit_seconds(
      {"enabled": True, "amount": 90, "unit": "minutes"}), lambda r: r == 5400)
check("days unit", lambda: schedule.limit_seconds(
      {"enabled": True, "amount": 2, "unit": "days"}), lambda r: r == 172800)
check("zero means no limit", lambda: schedule.limit_seconds(
      {"enabled": True, "amount": 0, "unit": "hours"}), lambda r: r is None)
check("readable durations", lambda: [schedule.human_duration(x)
      for x in (60, 5400, 172800)],
      lambda r: r == ["1 minute", "1.5 hours", "2 days"])
check("describe_auto_fail off", lambda: schedule.describe_auto_fail({}),
      lambda r: "as long as they need" in r)

print("\nBit depth and hardware decoding:")
check("probe reports bit depth", lambda: "_bit_depth" in dir(
      __import__("app")), lambda r: r is True)
check("8-bit detected", lambda: __import__("app")._bit_depth(
      {"pix_fmt": "yuv420p"}), lambda r: r == 8)
check("10-bit from pix_fmt", lambda: __import__("app")._bit_depth(
      {"pix_fmt": "yuv420p10le"}), lambda r: r == 10)
check("10-bit from bits_per_raw_sample", lambda: __import__("app")._bit_depth(
      {"bits_per_raw_sample": "10", "pix_fmt": "yuv420p"}), lambda r: r == 10)

print("\nTrack naming:")
import sys as _sys
_sys.path.insert(0, str(pathlib.Path(__file__).parent / "worker"))
import streams as _st                              # noqa: E402
check("language name from a 3-letter code",
      lambda: _st.language_name("ger"), lambda r: r == "German")
check("language name from a 2-letter code",
      lambda: _st.language_name("es"), lambda r: r == "Spanish")
check("terminological code maps too",
      lambda: _st.language_name("deu"), lambda r: r == "German")
check("undetermined stays unknown",
      lambda: _st.language_name("und"), lambda r: r is None)
check("audio title includes the layout", lambda: _st.describe_audio(
      {"tags": {"language": "eng"}, "channels": 6,
       "channel_layout": "5.1", "disposition": {}})[0],
      lambda r: r == "English 5.1")
check("commentary is marked", lambda: _st.describe_audio(
      {"tags": {"language": "eng", "title": "Director Commentary"},
       "channels": 2, "disposition": {"comment": 1}})[0],
      lambda r: "Commentary" in r)
check("language guessed from the old title", lambda: _st.describe_audio(
      {"tags": {"language": "und", "title": "French Audio"}, "channels": 6,
       "channel_layout": "5.1", "disposition": {}})[1], lambda r: r == "fre")
check("unknown language gets no title", lambda: _st.describe_audio(
      {"tags": {}, "channels": 2, "disposition": {}})[0], lambda r: r is None)
check("forced subtitle labelled", lambda: _st.describe_subtitle(
      {"tags": {"language": "eng"}, "disposition": {"forced": 1}})[0],
      lambda r: r == "English (Forced)")
check("SDH subtitle labelled", lambda: _st.describe_subtitle(
      {"tags": {"language": "eng"}, "disposition": {"hearing_impaired": 1}})[0],
      lambda r: r == "English (SDH)")
check("naming can be turned off", lambda: _st.naming_args(
      [{"tags": {"language": "eng"}, "channels": 2, "disposition": {}}], [],
      {"tidy_track_names": False}), lambda r: r == [])
_enc = __import__("encoders")
_enc.BENCHMARKS.update({"hevc_videotoolbox": 170.0, "libx265": 60.0})
_enc.BENCHMARKS_10BIT.update({"hevc_videotoolbox": 5.0})
_HEVC = ["hevc_videotoolbox", "libx265"]

check("8-bit source keeps hardware", lambda: _enc.pick(
      _HEVC, _HEVC, 8, {"bit_depth": "match"}),
      lambda r: r == "hevc_videotoolbox")
check("10-bit source on Match drops to 8-bit", lambda: _enc.choose_depth(
      "hevc_videotoolbox", {"bit_depth": "match"}, 10)[0], lambda r: r == "8")
check("and explains why", lambda: _enc.choose_depth(
      "hevc_videotoolbox", {"bit_depth": "match"}, 10)[1],
      lambda r: r and "8-bit" in r)
check("forcing 10-bit routes to a capable encoder", lambda: _enc.pick(
      _HEVC, _HEVC, 10, {"bit_depth": "10"}), lambda r: r == "libx265")
check("forcing 10-bit is obeyed", lambda: _enc.choose_depth(
      "libx265", {"bit_depth": "10"}, 10)[0], lambda r: r == "10")
check("forcing 8-bit is obeyed", lambda: _enc.choose_depth(
      "hevc_videotoolbox", {"bit_depth": "8"}, 10)[0], lambda r: r == "8")
check("an 8-bit source never becomes 10-bit", lambda: _enc.choose_depth(
      "libx265", {"bit_depth": "match"}, 8)[0], lambda r: r == "8")
check("no cliff means 10-bit is kept", lambda: (
      _enc.BENCHMARKS_10BIT.update({"libx265": 55.0}),
      _enc.choose_depth("libx265", {"bit_depth": "match"}, 10)[0])[-1],
      lambda r: r == "10")
# A zero measurement means the encode failed, not that it was slow. H.264
# hardware encoders always come back zero, because no consumer chip does
# 10-bit H.264 — reporting that as a fault is noise.
_enc.BENCHMARKS.update({"h264_amf": 269.5, "hevc_amf": 284.5})
_enc.BENCHMARKS_10BIT.update({"h264_amf": 0, "hevc_amf": 279.8})
check("zero 10-bit is not reported as slow", lambda: _enc.ten_bit_warnings(),
      lambda r: "h264_amf" not in r)
check("an encoder with no 10-bit is recognised", lambda:
      _enc.can_do_ten_bit("h264_amf"), lambda r: r is False)
check("and scores zero for a 10-bit job", lambda:
      _enc.effective_speed("h264_amf", True), lambda r: r == 0)
check("a capable encoder keeps 10-bit", lambda: _enc.choose_depth(
      "hevc_amf", {"bit_depth": "match"}, 10)[0], lambda r: r == "10")
check("an incapable one falls back with a clear reason", lambda:
      _enc.choose_depth("h264_amf", {"bit_depth": "match"}, 10)[1],
      lambda r: r and "can't produce 10-bit" in r)

print("\nReadable FFmpeg failures:")
_agent_path = str(pathlib.Path(__file__).parent / "worker")
if _agent_path not in sys.path:
    sys.path.insert(0, _agent_path)
import agent as _agent                            # noqa: E402
check("a Windows unsigned code is decoded", lambda:
      _agent.describe_exit(3199971767),
      lambda r: "wasn't valid" in r)
check("a plain exit code survives", lambda: _agent.describe_exit(1),
      lambda r: r == "FFmpeg exited 1")
check("the cause is put before the consequence", lambda: _agent.explain_failure(
      "[out#0/matroska @ 0x1] Could not write header (incorrect codec "
      "parameters ?): Invalid data found when processing input\n"
      "[af#0:1 @ 0x2] Error sending frames to consumers: Invalid data found",
      3199971767), lambda r:
      r.index("audio track") < r.index("Could not write header"))
check("and audio trouble gets a suggestion", lambda: _agent.explain_failure(
      "[af#0:1 @ 0x2] Error sending frames to consumers: Invalid data found",
      3199971767), lambda r: "Leave audio alone" in r)

print("\nRetrying a failed job:")
_spec = {"codec": "copy", "audio": "aac", "container": "mkv"}
_dup_path = "/m/dup.mkv"
_failed = db.enqueue(_dup_path, _spec, 1000)
db.update_job(_failed, state="failed", error="x")
db.enqueue(_dup_path, _spec, 1000)
check("a duplicate is cleared rather than erroring", lambda: _run(
      app.retry_job(_failed)), lambda r: r.get("removed") is True)
check("and the live job is untouched", lambda: db.count_jobs(["queued"]),
      lambda r: r >= 1)

_solo = db.enqueue("/m/solo.mkv", _spec, 1000)
db.update_job(_solo, state="failed", error="x")
check("a lone failed job requeues", lambda: (_run(app.retry_job(_solo)),
      db.get_job(_solo)["state"])[-1], lambda r: r == "queued")
check("and its error is cleared", lambda: db.get_job(_solo)["error"],
      lambda r: r is None)

# A job's own row is always "active" by the time someone hits Restart on
# it (that's the only state where the button appears) — has_job_for must
# not mistake that for a duplicate and delete the job out from under a
# worker that's still processing it.
_active = db.enqueue("/m/active.mkv", _spec, 1000)
db.update_job(_active, state="running")
check("restarting a running job requeues it, not deletes it", lambda: (
      _run(app.retry_job(_active)), db.get_job(_active))[-1], lambda r:
      r is not None and r["state"] == "queued")

print("\nCancelling a job:")
check("cancelling a running job succeeds", lambda: (
      _run(app.cancel(_active)), db.get_job(_active)["state"])[-1],
      lambda r: r == "cancelled")
check("cancelling a job that doesn't exist 404s",
      lambda: _status(app.cancel(999999)), lambda r: r == 404)

_done = db.enqueue("/m/done.mkv", _spec, 1000)
db.update_job(_done, state="done", finished_at=time.time())
check("cancelling a job that already finished is refused, not overwritten",
      lambda: _status(app.cancel(_done)), lambda r: r == 409)
check("and its state is untouched", lambda: db.get_job(_done)["state"],
      lambda r: r == "done")

print("\nA corrupt video handed off to the *arr:")
_arr_lib = db.create_library(
    "Corrupt", str(watch), str(base / "out2"),
    {**profile, "arr": {"on_unhealthy_video": "delete_and_research",
                        "url": "http://fake-arr", "kind": "radarr",
                        "api_key": "x"}},
    "archive")
_corrupt_path = "/m/corrupt.mkv"
_corrupt_job = db.enqueue(_corrupt_path, _spec, 1000, _arr_lib)
db.cache_probe(_corrupt_path, {"size": 1000, "duration": 1.0,
    "video_codec": "h264", "audio_codecs": ["ac3"]})
app.arr.find_and_research = lambda *a, **k: (True, "found a replacement")
check("a successful *arr research clears the stale probe-cache row", lambda: (
      _run(app.handle_unhealthy_video(db.get_job(_corrupt_job), "decode error")),
      db.get_cached_file(_corrupt_path))[-1], lambda r: r is None)
db.delete_library(_arr_lib)

print("\nTolerating odd stored values:")
check("parse_json handles NULL", lambda: db.parse_json(None, {}),
      lambda r: r == {})
check("parse_json handles rubbish", lambda: db.parse_json("not json", {}),
      lambda r: r == {})
check("parse_json handles bytes", lambda: db.parse_json(b'{"a":1}'),
      lambda r: r == {"a": 1})
check("parse_json passes a dict through", lambda: db.parse_json({"a": 1}),
      lambda r: r == {"a": 1})
def _write_bad_setting():
    with db.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO settings (key, value) "
                     "VALUES ('junk', '{{{')")
    return db.get_settings()


check("settings survive a bad row", _write_bad_setting,
      lambda r: "schedule" in r)

check("hardware decode for an 8-bit source", lambda: " ".join(
      __import__("encoders").build_command("i.mkv", "o.mp4", "hevc_videotoolbox",
        {"codec": "hevc", "quality": 22, "container": "mp4", "audio": "copy"},
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264",
                      "pix_fmt": "yuv420p", "disposition": {}}]})),
      lambda r: "-hwaccel videotoolbox" in r)
check("no hardware decode for 10-bit H.264", lambda: " ".join(
      __import__("encoders").build_command("i.mkv", "o.mp4", "hevc_videotoolbox",
        {"codec": "hevc", "quality": 22, "container": "mp4", "audio": "copy"},
        {"streams": [{"index": 0, "codec_type": "video", "codec_name": "h264",
                      "pix_fmt": "yuv420p10le", "bits_per_raw_sample": "10",
                      "disposition": {}}]})),
      lambda r: "-hwaccel" not in r)
check("ten_bit_warnings spots a cliff", lambda: (
      __import__("encoders").BENCHMARKS.update({"x": 170.0}),
      __import__("encoders").BENCHMARKS_10BIT.update({"x": 5.0}),
      __import__("encoders").ten_bit_warnings())[-1], lambda r: "x" in r)
check("titles follow the mapped order", lambda: _st.kept_tracks(
      {"streams": [
        {"index": 0, "codec_type": "video", "codec_name": "h264", "disposition": {}},
        {"index": 1, "codec_type": "audio", "tags": {"language": "jpn"},
         "channels": 2, "disposition": {}},
        {"index": 2, "codec_type": "audio", "tags": {"language": "eng"},
         "channels": 2, "disposition": {}}]},
      {"audio_languages": ["eng"], "subtitle_mode": "keep"})[0][0]["tags"]["language"],
      lambda r: r == "eng")

print("\nSize-check logic:")
check("savings_verdict smaller", lambda: profiles.savings_verdict(1000, 400),
      lambda r: r[0] is True)
check("savings_verdict bigger",
      # Real file-scale numbers, well past GROWTH_NOISE_FLOOR — the old
      # toy-scale 1000->1200 stopped meaning anything once "bigger" became
      # a real byte comparison rather than a pure percentage.
      lambda: profiles.savings_verdict(200_000_000, 240_000_000),
      lambda r: r[0] is False and r[1] < 0)
check("savings_verdict below threshold",
      lambda: profiles.savings_verdict(1000, 970, 10), lambda r: r[0] is False)
check("savings_verdict remux landing on the same size isn't 'bigger'",
      lambda: profiles.savings_verdict(213_000_000, 213_000_000),
      lambda r: r[0] is True)
check("savings_verdict a real regression still isn't hidden by the noise floor",
      lambda: profiles.savings_verdict(
          200_000_000, 200_000_000 + profiles.GROWTH_NOISE_FLOOR + 1),
      lambda r: r[0] is False)
check("base_quality", lambda: profiles.base_quality({"quality_level": "balanced"}),
      lambda r: r == 22)
check("retry_ladder off", lambda: profiles.retry_ladder({"quality_level": "balanced"}),
      lambda r: r == [])
check("retry_ladder on", lambda: profiles.retry_ladder(
      {"quality_level": "balanced", "auto_retry": True,
       "auto_retry_steps": ["small", "smaller"]}), lambda r: r == [26, 30])
check("manual_steps", lambda: profiles.manual_steps({"quality_level": "balanced"}),
      lambda r: len(r) == 4 and r[0]["quality"] == 26)
check("original_for_job", lambda: db.original_for_job(9999), lambda r: r is None)
check("forget_processed", lambda: db.forget_processed("/nothing.mkv"))
check("restore_original without one", lambda: watcher.restore_original(
      {"id": 9999, "path": "/x.mkv", "final_path": None}, {"id": 1}),
      lambda r: r[0] is False)

print()
if failures:
    print(f"{len(failures)} problem(s):")
    for f in failures:
        print("  " + f)
    sys.exit(1)
print("Every server function works.")
