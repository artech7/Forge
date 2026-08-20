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
import app                                       # noqa: E402
import naming, profiles, schedule, watcher       # noqa: E402
import lookup, scheduler                         # noqa: E402

failures = []


def _run(coroutine):
    """Run one async endpoint from this synchronous script."""
    import asyncio
    return asyncio.run(coroutine)


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
check("was_processed", lambda: db.was_processed("/m/a.mkv", 1.0), lambda r: r is True)
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
      3199971767), lambda r: r.index("af#0:1") < r.index("out#0/matroska"))
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
check("savings_verdict bigger", lambda: profiles.savings_verdict(1000, 1200),
      lambda r: r[0] is False and r[1] < 0)
check("savings_verdict below threshold",
      lambda: profiles.savings_verdict(1000, 970, 10), lambda r: r[0] is False)
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
