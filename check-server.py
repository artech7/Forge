#!/usr/bin/env python3
"""Exercise every server function against a throwaway database.

Catches the class of bug where an edit lands in the wrong function — the
code imports and parses fine, then fails at runtime on a specific call.
    python3 check-server.py
"""
import inspect
import pathlib
import re
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
import arr                                       # noqa: E402


class _FakeReq:
    """Enough of a Request for endpoints that only read the body."""
    def __init__(self, body): self._body = body
    async def json(self): return self._body


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

print("\nThe worker command shown for each platform:")
# Two rounds of copy-paste failures came from this line, so it is
# asserted rather than eyeballed. PowerShell has no "VAR=value command"
# form, and the assignment must be a separate statement.
_ui = (pathlib.Path(__file__).parent / "server" / "static" / "index.html").read_text()
_runlines = _ui[_ui.index("function runLines("):_ui.index("function renderDeepScanBar(")]
check("the Windows line separates the two statements",
      lambda: '"; .\\\\run-node.ps1' in _runlines, lambda r: r is True)
check("and never uses bash's VAR=value prefix form",
      lambda: "FORGE_TOKEN=${NODE_TOKEN} .\\\\run-node" in _runlines,
      lambda r: r is False)
check("the Mac/Linux line does use it, which is correct there",
      lambda: "FORGE_TOKEN=${NODE_TOKEN} ./run-node.sh" in _runlines,
      lambda r: r is True)
check("both platforms are labelled",
      lambda: ("Windows (PowerShell)" in _runlines
               and "Mac or Linux" in _runlines), lambda r: r is True)
_ps1 = (pathlib.Path(__file__).parent / "run-node.ps1").read_text()
check("run-node.ps1 takes a -Token parameter",
      lambda: "[string]$Token" in _ps1, lambda r: r is True)
check("and passes it to the worker",
      lambda: "$env:FORGE_TOKEN = $Token" in _ps1, lambda r: r is True)

print("\nChecking a file's tracks decode:")
_hc_src = str(base / "hc.mkv")
pathlib.Path(_hc_src).write_bytes(b"not really a video")
_hc_info = {"streams": [
    {"index": 0, "codec_type": "video", "codec_name": "h264"},
    {"index": 1, "codec_type": "audio", "codec_name": "aac"},
    {"index": 2, "codec_type": "audio", "codec_name": "ac3"},
    {"index": 3, "codec_type": "subtitle", "codec_name": "subrip"}]}
_hc_calls = []
_real_decode = _st._decode_streams
# A healthy file: the combined pass says yes and nothing else runs.
_st._decode_streams = lambda path, idx: (_hc_calls.append(list(idx)), (True, None))[1]
_hc = _st.health_check(_hc_src, _hc_info)
check("a healthy file is read once, not once per track",
      lambda: len(_hc_calls), lambda r: r == 1)
check("and that one pass covers video plus every audio track",
      lambda: _hc_calls[0], lambda r: r == [0, 1, 2])
check("subtitles are left out of it",
      lambda: 3 in _hc_calls[0], lambda r: r is False)
check("every checked track comes back healthy",
      lambda: (_hc["video"], sorted(_hc["audio"])),
      lambda r: r == ((True, None), [1, 2]))

# A file with something wrong: fall back to one pass per track to find it.
_hc_calls.clear()
_st._decode_streams = lambda path, idx: (
    _hc_calls.append(list(idx)),
    (True, None) if idx == [1] else (False, "broken"))[1]
_hc2 = _st.health_check(_hc_src, _hc_info)
check("a bad file falls back to checking each track alone",
      lambda: _hc_calls, lambda r: r == [[0, 1, 2], [0], [1], [2]])
check("and says which track is at fault",
      lambda: (_hc2["video"][0], _hc2["audio"][1][0], _hc2["audio"][2][0]),
      lambda r: r == (False, True, False))
_st._decode_streams = _real_decode
pathlib.Path(_hc_src).unlink(missing_ok=True)

print("\nSaying what a job is doing before there's a percentage:")
_ph_lib = db.create_library("Phases", str(base / "ph"), "", profile, "archive")
_ph_job = db.enqueue("/ph/big.mkv", {"codec": "hevc"}, 5_000_000_000, _ph_lib)
db.upsert_node("ph-node", "ph-node", ["libx265"], [], 1)
_leased = scheduler.lease_job("ph-node")
check("the job leases", lambda: _leased and _leased["id"], lambda r: r == _ph_job)

def _expire(job_id):
    with db.connect() as conn:
        conn.execute("UPDATE jobs SET lease_expires=? WHERE id=?",
                     (time.time() - 1, job_id))

# Without a heartbeat this is the old behaviour: a check that outlasts
# the lease bounces the job back to the queue, and starts over forever.
_expire(_ph_job)
scheduler.requeue_expired()
check("silence past the lease sends it back to the queue",
      lambda: db.get_job(_ph_job)["state"], lambda r: r == "queued")
check("and counts against it",
      lambda: db.get_job(_ph_job)["bounces"], lambda r: r == 1)

_release = scheduler.lease_job("ph-node")
_run(app.progress(_ph_job, _FakeReq(
    {"heartbeat": True, "phase": "checking every track plays — track 2 of 3"})))
check("a heartbeat says what it's doing",
      lambda: db.get_job(_ph_job)["phase"],
      lambda r: r == "checking every track plays — track 2 of 3")
check("and keeps the lease, so the check isn't restarted",
      lambda: (scheduler.requeue_expired(), db.get_job(_ph_job)["state"])[1],
      lambda r: r in ("leased", "running"))
check("but never clears the bounces already counted",
      lambda: db.get_job(_ph_job)["bounces"], lambda r: r == 1)
check("nor invents progress",
      lambda: db.get_job(_ph_job)["progress"], lambda r: not r)
# The stall timer is what stops a job wedged in a phase from heartbeating
# forever, so it has to still see this job as making no progress.
check("a job stuck in a phase is still caught as stalled",
      lambda: schedule.overrun_reason(
          {**db.get_job(_ph_job), "state": "running",
           "started_at": time.time() - 7200, "progress_at": None},
          {"enabled": True, "stall_enabled": True, "stall_minutes": 30}),
      lambda r: bool(r) and "no progress" in r)
# Real progress behaves as before.
_run(app.progress(_ph_job, _FakeReq({"progress": 12, "phase": "converting"})))
check("real progress still records a percentage",
      lambda: db.get_job(_ph_job)["progress"], lambda r: r == 12)
check("requeueing clears the phase, so nothing describes stale work",
      lambda: (db.requeue_jobs(["leased", "running"], library_id=_ph_lib),
               db.get_job(_ph_job)["phase"])[1],
      lambda r: r is None)
db.delete_jobs(["queued"], library_id=_ph_lib)
db.delete_library(_ph_lib)

print("\nWhat the worker launchers probe before starting:")
# Both scripts check the server is there first. Probing an address that
# needs a session made a healthy Forge report "can't reach" as soon as a
# login existed — PowerShell raises on 401 like any other failure.
_root = pathlib.Path(__file__).parent
_sh = (_root / "run-node.sh").read_text()
_ps1 = (_root / "run-node.ps1").read_text()
# Comments stripped first: both files explain in prose which address
# they deliberately avoid, and naming it there isn't using it.
_code = "\n".join(line for line in (_sh + "\n" + _ps1).splitlines()
                  if not line.lstrip().startswith("#"))
_probed = set(re.findall(r"/api/[a-z/-]+", _code))
check("every address the launchers touch is reachable signed out",
      lambda: sorted(_probed - app.OPEN_PATHS), lambda r: r == [])
check("both probe the address that answers either way",
      lambda: ("/api/auth/state" in _sh, "/api/auth/state" in _ps1),
      lambda r: r == (True, True))
check("and the probed address really is open",
      lambda: "/api/auth/state" in app.OPEN_PATHS, lambda r: r is True)
check("each launcher stops early when a token is needed",
      lambda: ("FORGE_TOKEN" in _sh and "needs its token" in _sh,
               "Token" in _ps1 and "needs its token" in _ps1),
      lambda r: r == (True, True))

print("\nThe out-of-date-files banner:")
# A name in REQUIRED with no module behind it reported every one of its
# functions as missing, which is a false alarm pointing at a file that
# was never wrong.
check("every module the check demands is actually loaded",
      lambda: app.check_modules(), lambda r: r == [])

print("\nPasswords:")
import auth as _auth                               # noqa: E402
_hash = _auth.hash_password("correct horse battery")
check("the right password verifies",
      lambda: _auth.verify_password("correct horse battery", _hash),
      lambda r: r is True)
check("a wrong one does not",
      lambda: _auth.verify_password("Correct horse battery", _hash),
      lambda r: r is False)
check("an empty one does not",
      lambda: _auth.verify_password("", _hash), lambda r: r is False)
check("the password itself is never in the stored value",
      lambda: "correct horse" in _hash, lambda r: r is False)
check("the same password hashes differently each time (salted)",
      lambda: _auth.hash_password("x") == _auth.hash_password("x"),
      lambda r: r is False)
check("a corrupt stored hash is refused, not an error",
      lambda: _auth.verify_password("x", "nonsense"), lambda r: r is False)
check("tokens are long enough to be worth having",
      lambda: len(_auth.new_token()), lambda r: r >= 32)
check("two tokens are never the same",
      lambda: _auth.new_token() == _auth.new_token(), lambda r: r is False)

print("\nSlowing down password guessing:")
_att = _auth.Attempts()
check("a few fumbles cost nothing",
      lambda: [(_att.record_failure(), _att.blocked_for())[1]
               for _ in range(_auth.BACKOFF_AFTER)],
      lambda r: r == [0] * _auth.BACKOFF_AFTER)
check("then it starts making you wait",
      lambda: (_att.record_failure(), _att.blocked_for())[1],
      lambda r: r > 0)
check("the wait never becomes a lockout",
      lambda: ([_att.record_failure() for _ in range(40)],
               _att.blocked_for())[1],
      lambda r: 0 < r <= _auth.BACKOFF_CAP + 1)
check("and getting it right clears it at once",
      lambda: (_att.clear(), _att.blocked_for())[1], lambda r: r == 0)

print("\nSessions:")
_sess = db.create_session("a browser")
check("a new session is valid", lambda: db.session_valid(_sess),
      lambda r: r is True)
check("a made-up one is not", lambda: db.session_valid("not-a-session"),
      lambda r: r is False)
check("and neither is nothing at all", lambda: db.session_valid(""),
      lambda r: r is False)
check("signing out ends it",
      lambda: (db.end_session(_sess), db.session_valid(_sess))[1],
      lambda r: r is False)
_expired = db.create_session("old", days=-1)
check("an expired session is refused", lambda: db.session_valid(_expired),
      lambda r: r is False)
check("changing the password ends every session",
      lambda: (db.create_session("a"), db.create_session("b"),
               db.end_all_sessions(), db.count_sessions())[-1],
      lambda r: r == 0)

print("\nWhat a worker's token may reach:")
_open = ["/api/nodes/register", "/api/nodes/desktop/lease",
         "/api/jobs/12/progress", "/api/jobs/12/complete",
         "/api/jobs/12/fail", "/api/jobs/12/measured", "/api/jobs/12/source"]
_shut = ["/api/state", "/api/libraries", "/api/settings", "/api/jobs/bulk",
         "/api/originals/sweep", "/api/scan", "/api/auth/change",
         "/api/nodes/desktop/housekeeping-slots", "/api/files/replace-missing-audio"]
check("everything a worker needs is reachable with it",
      lambda: [p for p in _open if not app.WORKER_PATHS.match(p)],
      lambda r: r == [])
check("and nothing else is",
      lambda: [p for p in _shut if app.WORKER_PATHS.match(p)],
      lambda r: r == [])
check("the login screen is reachable signed out",
      lambda: sorted(app.OPEN_PATHS),
      lambda r: r == ["/", "/api/auth/login", "/api/auth/setup",
                      "/api/auth/state", "/favicon.ico"])

print("\nPicking an audio encoder FFmpeg will actually run:")
import encoders as _enc                            # noqa: E402
# A real slice of "ffmpeg -encoders". The fourth flag is X for
# experimental; a trailing "(codec x)" means the encoder writes a
# format of a different name.
_enc._encoder_table = None
_real_subprocess = _enc.subprocess
_enc.subprocess = type("_S", (), {
    "run": staticmethod(lambda *a, **k: type("_R", (), {"stdout": """\
 A....D aac                  AAC (Advanced Audio Coding)
 A..X.D opus                 Opus
 A....D libopus              libopus Opus (codec opus)
 A..X.D vorbis               Vorbis
 A....D libmp3lame           libmp3lame MP3 (MPEG audio layer 3) (codec mp3)
 A....D eac3                 ATSC A/52B (AC-3, E-AC-3)
 A....D flac                 FLAC (Free Lossless Audio Codec)
 V....D libx265              libx265 H.265 / HEVC
"""})()),
    "SubprocessError": Exception})
_table = _enc.available_encoders(refresh=True)
check("the encoder list is read as codec -> encoders",
      lambda: sorted(_table), lambda r: r == ["aac", "eac3", "flac", "mp3",
                                              "opus", "vorbis"])
check("video encoders are left out of it",
      lambda: "libx265" in _table, lambda r: r is False)
check("an experimental built-in is swapped for the library encoder",
      lambda: _enc.audio_encoder("opus"), lambda r: r == "libopus")
check("a codec whose encoder has another name still resolves",
      lambda: _enc.audio_encoder("mp3"), lambda r: r == "libmp3lame")
check("an ordinary codec is passed through untouched",
      lambda: _enc.audio_encoder("aac"), lambda r: r == "aac")
check("experimental with no library encoder falls back to AAC",
      lambda: _enc.audio_encoder("vorbis"), lambda r: r == "aac")
check("so does a format this build cannot write",
      lambda: _enc.audio_encoder("truehd"), lambda r: r == "aac")
check("and a track with no codec at all",
      lambda: _enc.audio_encoder(None), lambda r: r == "aac")
# The failure Dylan hit: levelling a file with an Opus track asked for
# the experimental built-in and killed the whole job.
_opus_info = {"streams": [
    {"codec_type": "video", "codec_name": "h264"},
    {"codec_type": "audio", "codec_name": "aac", "channels": 2},
    {"codec_type": "audio", "codec_name": "opus", "channels": 2}]}
_level = _enc.build_level_command(
    pathlib.Path("/in.mp4"), pathlib.Path("/out.mp4"),
    {"level_only": True, "loudness_target_i": -16}, _opus_info)
check("levelling never asks for the bare opus encoder",
      lambda: "opus" in _level, lambda r: r is False)
check("levelling asks for libopus instead",
      lambda: "libopus" in _level, lambda r: r is True)
check("and still keeps the AAC track as AAC",
      lambda: _level[_level.index("-c:a:0") + 1], lambda r: r == "aac")
_enc.subprocess = _real_subprocess                 # leave no fixture behind
_enc._encoder_table = None

print("\nThe schema can still be run against an older database:")
# init() executes SCHEMA in full on every start, including on databases
# made by earlier versions. CREATE TABLE IF NOT EXISTS is a no-op there,
# but every other statement still runs — so an index in SCHEMA naming a
# column that migrate() adds fails outright and the server won't boot.
# That shipped once; this makes it impossible to ship twice.
def _schema_indexes_only_touch_original_columns():
    import re
    # Read the column list straight out of migrate()'s own source, so a
    # column added there in future is covered without touching this test.
    src = inspect.getsource(db.migrate)
    additions = eval(src[src.index("additions = {") + len("additions = "):
                         src.index("}\n    added")] + "}")
    offenders = []
    for stmt in re.findall(r"CREATE INDEX[^;]+;", db.SCHEMA, re.I | re.S):
        m = re.search(r"ON\s+(\w+)\s*\(([^)]*)\)", stmt, re.I | re.S)
        if not m:
            continue
        table, cols = m.group(1), m.group(2)
        named = {c.strip().split()[0] for c in cols.split(",") if c.strip()}
        migrated = {n for n, _ in additions.get(table, [])}
        for col in named & migrated:
            offenders.append(f"{table}.{col}")
    return offenders
check("no index in SCHEMA names a column migrate() adds later",
      _schema_indexes_only_touch_original_columns, lambda r: r == [])

print("\nConversions always outrank loudness work:")
_tier_lib = db.create_library("Tiers", str(base / "tiers"), "", profile, "archive")
_before = db.queued_by_kind()          # earlier tests leave jobs behind
# More measuring work than the scheduler's old fixed window, queued
# first — exactly the shape that starved conversions in production.
for _i in range(2100):
    db.enqueue(f"/tiers/m{_i}.mkv", {"measure": "loudness"}, 1, _tier_lib)
for _i in range(20):
    db.enqueue(f"/tiers/l{_i}.mkv", {"level_only": True, "codec": "copy"},
               1, _tier_lib)
for _i in range(30):
    db.enqueue(f"/tiers/c{_i}.mkv", {"codec": "copy", "audio": "aac"},
               1, _tier_lib)
check("each kind is counted separately",
      lambda: {k: db.queued_by_kind()[k] - _before.get(k, 0)
               for k in ("measure", "level", "convert")},
      lambda r: r == {"measure": 2100, "level": 20, "convert": 30})
check("a conversion is found behind a backlog deeper than any one window",
      lambda: len(db.next_queued("convert", 200)) - len(
          [j for j in db.next_queued("convert", 200)
           if j["library_id"] != _tier_lib]),
      lambda r: r == 30)
check("next_queued returns only the kind asked for",
      lambda: {db.job_kind(j["spec"]) for j in db.next_queued("level", 50)},
      lambda r: r == {"level"})
check("and hands them back oldest-first",
      lambda: [j["id"] for j in db.next_queued("convert", 3)],
      lambda r: r == sorted(r))
db.delete_jobs(["queued"], library_id=_tier_lib)
db.delete_library(_tier_lib)

print("\nStandardize records the same fields a scan does:")
_std_lib = db.create_library(
    "Standardize", str(watch), "", {**profile, "video_codec": "hevc",
                                    "audio_codec": "aac", "container": "mkv"},
    "delete")
_std_path = str(watch / "Spy.mkv")
pathlib.Path(_std_path).write_bytes(b"x" * 2048)
_std_info = {"video_codec": "hevc", "audio_codecs": ["eac3", "eac3"],
             "streams": [], "duration": 60.0, "size": 2048}
_std_action, _std_spec, _std_why = watcher.plan_conversion(
    pathlib.Path(_std_path), _std_info,
    profiles.resolve(db.get_library(_std_lib)["profile"]), {})
check("an HEVC file with EAC3 audio is an audio-only job",
      lambda: _std_action, lambda r: r == "audio_only")
check("and plan_conversion leaves the video copied",
      lambda: (_std_spec.get("codec"), _std_spec.get("audio")),
      lambda r: r == ("copy", "aac"))
# plan_conversion itself doesn't stamp the action — both callers do, and
# the standardize endpoint used to forget, which is what made an
# eac3-to-AAC conversion show up in the queue as "leveling audio".
check("plan_conversion does not stamp it itself",
      lambda: "action" in _std_spec, lambda r: r is False)
# stats_queue probes the file for real; this one is 2KB of padding.
_real_probe = app.probe
app.probe = lambda path: _std_info
_queued = _run(app.stats_queue(_FakeReq({"paths": [_std_path],
                                         "use_library_defaults": True})))
app.probe = _real_probe
check("queueing it through Standardize works",
      lambda: _queued["queued"], lambda r: r == 1)
_std_job = db.list_jobs(states=["queued"], limit=50)
_std_job = [j for j in _std_job if j["path"] == _std_path]
check("the job records what the work actually is",
      lambda: _std_job[0]["spec"].get("action"), lambda r: r == "audio_only")
check("it records why, as a scan would",
      lambda: bool(_std_job[0]["spec"].get("why")), lambda r: r is True)
check("and the library's own original handling, not the default",
      lambda: _std_job[0]["spec"].get("original_action"), lambda r: r == "delete")
for _j in _std_job:
    db.delete_job(_j["id"])
db.delete_library(_std_lib)
pathlib.Path(_std_path).unlink(missing_ok=True)

print("\nTranslating a path for Radarr/Sonarr:")
_tv = "/media/TV Shows/Naruto/S01E01.mkv"
check("a plain prefix swap",
      lambda: arr.remap_path(_tv, "/media/TV Shows", "/tvshows"),
      lambda r: r == "/tvshows/Naruto/S01E01.mkv")
check("a trailing slash typed into either box is forgiven",
      lambda: arr.remap_path(_tv, "/media/TV Shows/", "/tvshows/"),
      lambda r: r == "/tvshows/Naruto/S01E01.mkv")
check("no mapping set leaves the path alone",
      lambda: arr.remap_path(_tv, "", ""), lambda r: r == _tv)
check("a prefix that doesn't match leaves the path alone",
      lambda: arr.remap_path(_tv, "/media/Anime", "/anime"), lambda r: r == _tv)
check("two libraries on one Sonarr translate independently",
      lambda: (arr.remap_path("/media/Anime/Bleach/S01E01.mkv",
                              "/media/Anime", "/anime"),
               arr.remap_path(_tv, "/media/TV Shows", "/tvshows")),
      lambda r: r == ("/anime/Bleach/S01E01.mkv", "/tvshows/Naruto/S01E01.mkv"))

print("\nWhere a library's *arr address comes from:")
_lib_only = {"profile": {"arr": {"kind": "radarr", "url": "http://on-library",
                                 "api_key": "old", "path_to": "/movies"}}}
check("with nothing set globally, an address on the library is still used",
      lambda: app.arr_for(_lib_only)["url"], lambda r: r == "http://on-library")
db.save_settings({"radarr": {"url": "http://global-radarr", "api_key": "new"}})
check("once set in Settings, the global address wins",
      lambda: app.arr_for(_lib_only)["url"], lambda r: r == "http://global-radarr")
check("and brings its own key, not the stale one",
      lambda: app.arr_for(_lib_only)["api_key"], lambda r: r == "new")
check("the library's own path mapping survives the swap",
      lambda: app.arr_for(_lib_only)["path_to"], lambda r: r == "/movies")
check("a Sonarr library doesn't pick up the Radarr address",
      lambda: app.arr_for({"profile": {"arr": {"kind": "sonarr"}}}).get("url"),
      lambda r: not r)
check("a library managed by neither resolves to no address",
      lambda: app.arr_for({"profile": {"arr": {"kind": "none",
                                               "url": "http://ignored"}}}),
      lambda r: r.get("kind") == "none")
check("and a library with no *arr section at all is handled",
      lambda: app.arr_for({}), lambda r: r == {})
db.save_settings({"radarr": {"url": "", "api_key": ""}})
_warn = lambda arr: [w for w in profiles.warnings_for(
    {**profile, "arr": arr}) if "Managed by" in w]
check("asking to replace files with no *arr chosen is called out",
      lambda: _warn({"kind": "none", "auto_replace_missing_audio": True}),
      lambda r: len(r) == 1)
check("so is researching a corrupt file with no *arr chosen",
      lambda: _warn({"kind": "none", "on_unhealthy_video": "delete_and_research"}),
      lambda r: len(r) == 1)
check("but not once one is chosen",
      lambda: _warn({"kind": "sonarr", "auto_replace_missing_audio": True}),
      lambda r: r == [])
check("and not when nothing asks for a replacement",
      lambda: _warn({"kind": "none"}), lambda r: r == [])
_wdraft = lambda **d: [w for w in profiles.warnings_for({**profile, **d})
                       if "Managed by" in w]
check("the wizard's own flat draft is read the same way",
      lambda: _wdraft(arr_kind="none", auto_replace_missing_audio=True),
      lambda r: len(r) == 1)
check("and clears once the draft picks an *arr",
      lambda: _wdraft(arr_kind="sonarr", auto_replace_missing_audio=True),
      lambda r: r == [])

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
