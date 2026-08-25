"""SQLite storage for Forge. Single file, WAL mode, no ORM."""
import json
import os
import sqlite3
import time
from collections import Counter
from pathlib import Path

# Where the database lives. In a container this is a mounted volume; run
# directly, it sits beside the code. FORGE_DATA overrides both.
DB_PATH = Path(os.environ.get("FORGE_DATA")
               or Path(__file__).parent / "data") / "forge.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    encoders     TEXT NOT NULL DEFAULT '[]',   -- JSON list of verified encoder ids
    mounts       TEXT NOT NULL DEFAULT '[]',   -- JSON list of {server, local}
    max_jobs     INTEGER NOT NULL DEFAULT 1,
    slots        INTEGER,              -- server-controlled; overrides max_jobs
    cpus         INTEGER,
    recipes      TEXT NOT NULL DEFAULT '{}',
    benchmarks   TEXT NOT NULL DEFAULT '{}',
    benchmarks_10bit TEXT NOT NULL DEFAULT '{}',
    last_seen    REAL NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS libraries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT NOT NULL UNIQUE,
    watch_path    TEXT NOT NULL,
    output_path   TEXT,                 -- NULL = transcode in place
    profile       TEXT NOT NULL,        -- JSON wizard profile
    original_action TEXT NOT NULL DEFAULT 'archive',
    mirror_folders INTEGER NOT NULL DEFAULT 1,
    skip_matching  INTEGER NOT NULL DEFAULT 1,
    filters        TEXT NOT NULL DEFAULT '{}',
    naming         TEXT NOT NULL DEFAULT '{}',
    originals_path TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS lookup_cache (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    at    REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS originals (
    archived_path TEXT PRIMARY KEY,
    job_id        INTEGER,
    library_id    INTEGER,
    final_path    TEXT,
    size          INTEGER,
    archived_at   REAL
);

CREATE TABLE IF NOT EXISTS processed (
    path       TEXT PRIMARY KEY,
    mtime      REAL,
    size       INTEGER,
    library_id INTEGER,
    at         REAL
);

CREATE TABLE IF NOT EXISTS pending (
    path       TEXT PRIMARY KEY,
    size       INTEGER,
    first_seen REAL
);

CREATE TABLE IF NOT EXISTS files (
    path         TEXT PRIMARY KEY,
    size         INTEGER,
    duration     REAL,
    video_codec  TEXT,
    video_bitrate INTEGER,
    bit_depth    INTEGER,
    audio_codecs TEXT,
    width        INTEGER,
    height       INTEGER,
    bitrate      INTEGER,
    detail       TEXT,                 -- JSON: everything the columns above don't hold
    probed_at    REAL
);

CREATE TABLE IF NOT EXISTS jobs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    path          TEXT NOT NULL,
    library_id    INTEGER,
    spec          TEXT NOT NULL,      -- JSON intent: codec, quality, audio, container
    state         TEXT NOT NULL,      -- queued|leased|running|done|failed|cancelled|bloated|ignored|removed
    node_id       TEXT,
    transport     TEXT,               -- local|stream
    lease_expires REAL,
    progress      REAL DEFAULT 0,
    fps           REAL DEFAULT 0,
    speed         REAL DEFAULT 0,
    size_before   INTEGER,
    size_after    INTEGER,
    encoder_used  TEXT,
    error         TEXT,
    attempt       INTEGER NOT NULL DEFAULT 1,
    size_now      INTEGER,              -- bytes written so far, for a live ratio
    progress_at   REAL,                 -- when progress last actually moved
    outcome       TEXT,                 -- why it landed in the bloated list
    output_local  TEXT,                 -- where the worker wrote it, in node space
    final_path    TEXT,                 -- where the server put it, in server space
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL
);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active
    ON jobs(path) WHERE state IN ('queued','leased','running');
"""


def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init():
    with connect() as conn:
        conn.executescript(SCHEMA)


def parse_json(value, default=None):
    """Decode a stored JSON column, tolerating anything unexpected.

    A NULL column raises TypeError rather than JSONDecodeError, which is a
    different exception and was slipping past narrower handlers.
    """
    if value is None:
        return default
    if isinstance(value, (bytes, bytearray)):
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return default
    if not isinstance(value, str):
        return value if isinstance(value, (dict, list)) else default
    try:
        return json.loads(value)
    except (json.JSONDecodeError, ValueError):
        return default


def row_to_dict(row):
    if row is None:
        return None
    d = dict(row)
    for key in ("encoders", "mounts", "spec", "profile", "filters",
                "recipes", "benchmarks", "benchmarks_10bit", "naming"):
        if key in d:
            d[key] = parse_json(d[key], {} if key not in
                                ("encoders", "mounts") else [])
    return d


def upsert_node(node_id, name, encoders, mounts, max_jobs,
                recipes=None, benchmarks=None, cpus=None, benchmarks_10bit=None):
    """Register or refresh a node.

    slots is deliberately NOT overwritten on re-registration: it's set from
    the UI and must survive the worker checking in every twenty seconds.
    """
    with connect() as conn:
        conn.execute(
            """INSERT INTO nodes
               (id, name, encoders, mounts, max_jobs, slots, cpus,
                recipes, benchmarks, benchmarks_10bit, last_seen)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                 name=excluded.name, encoders=excluded.encoders,
                 mounts=excluded.mounts, max_jobs=excluded.max_jobs,
                 cpus=excluded.cpus,
                 recipes=excluded.recipes, benchmarks=excluded.benchmarks,
                 benchmarks_10bit=excluded.benchmarks_10bit,
                 last_seen=excluded.last_seen""",
            (node_id, name, json.dumps(encoders), json.dumps(mounts),
             max_jobs, max_jobs, cpus, json.dumps(recipes or {}),
             json.dumps(benchmarks or {}), json.dumps(benchmarks_10bit or {}),
             time.time()),
        )


def set_slots(node_id, slots):
    slots = max(0, min(16, int(slots)))
    with connect() as conn:
        conn.execute("UPDATE nodes SET slots=? WHERE id=?", (slots, node_id))
    return slots


def node_slots(node_id):
    node = get_node(node_id)
    if not node:
        return 0
    value = node.get("slots")
    return int(value) if value is not None else int(node.get("max_jobs") or 1)


def touch_node(node_id):
    with connect() as conn:
        conn.execute("UPDATE nodes SET last_seen=? WHERE id=?",
                     (time.time(), node_id))


def get_node(node_id):
    with connect() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM nodes WHERE id=?", (node_id,)).fetchone()
        )


def list_nodes():
    with connect() as conn:
        return [row_to_dict(r) for r in
                conn.execute("SELECT * FROM nodes ORDER BY name").fetchall()]


def enqueue(path, spec, size_before=None, library_id=None, attempt=1):
    """Returns job id, or None if this path already has an active job."""
    with connect() as conn:
        try:
            cur = conn.execute(
                """INSERT INTO jobs
                   (path, library_id, spec, state, size_before, attempt, created_at)
                   VALUES (?, ?, ?, 'queued', ?, ?, ?)""",
                (path, library_id, json.dumps(spec), size_before, attempt,
                 time.time()),
            )
            return cur.lastrowid
        except sqlite3.IntegrityError:
            return None


ACTIVE_STATES = ("queued", "leased", "running")
VIEWS = {
    "active": ACTIVE_STATES,
    "failed": ("failed",),
    "done": ("done",),
    "bloated": ("bloated",),
    "cancelled": ("cancelled",),
    # Failed once, then failed again even with audio left untouched — not
    # worth re-showing in "Failed" every time the queue is checked, but
    # not silently discarded either. See handle_audio_fail() in app.py.
    "ignored": ("ignored",),
    # The video itself was unrecoverable and Radarr/Sonarr agreed to fetch
    # a working copy — the source file is gone by design, not by failure.
    "removed": ("removed",),
}


def list_jobs(states=None, limit=200, offset=0, library_id=None):
    query = "SELECT * FROM jobs"
    clauses, params = [], []
    if states:
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if library_id is not None:
        clauses.append("library_id=?")
        params.append(library_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    # Active work reads best oldest-first (that's the running order);
    # history reads best newest-first.
    ascending = states and set(states) <= set(ACTIVE_STATES)
    query += " ORDER BY id ASC" if ascending else " ORDER BY id DESC"
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(query, params).fetchall()]


def count_jobs(states=None, library_id=None):
    query = "SELECT COUNT(*) FROM jobs"
    clauses, params = [], []
    if states:
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if library_id is not None:
        clauses.append("library_id=?")
        params.append(library_id)
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with connect() as conn:
        return conn.execute(query, params).fetchone()[0]


def job_counts(library_id=None):
    return {view: count_jobs(states, library_id) for view, states in VIEWS.items()}


def counts_by_library():
    """View counts for every library in one query, keyed by library_id.

    Used on every broadcast (a running job reports progress every second or
    two), so this is one GROUP BY rather than 4 queries per library — the
    difference matters once there are several libraries with jobs in flight.
    Jobs queued without a library (e.g. by hand via /api/queue) land under
    the None key, which the interface's "All" view already covers without
    needing a lookup here.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT library_id, state, COUNT(*) FROM jobs GROUP BY library_id, state"
        ).fetchall()
    per_lib = {}
    for library_id, state_name, n in rows:
        per_lib.setdefault(library_id, {})[state_name] = n
    return {
        lib_id: {view: sum(state_counts.get(s, 0) for s in states)
                 for view, states in VIEWS.items()}
        for lib_id, state_counts in per_lib.items()
    }


def delete_job(job_id):
    with connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id=?", (job_id,))


def delete_jobs(states, library_id=None):
    query = f"DELETE FROM jobs WHERE state IN ({','.join('?' * len(states))})"
    params = list(states)
    if library_id is not None:
        query += " AND library_id=?"
        params.append(library_id)
    with connect() as conn:
        cur = conn.execute(query, params)
        return cur.rowcount


def requeue_jobs(states, library_id=None):
    """Put jobs back in the queue, skipping any whose path is already active.

    The partial unique index would reject a duplicate, so those are counted
    and reported rather than raising.
    """
    moved, skipped = 0, 0
    for job in list_jobs(states=list(states), limit=2000, library_id=library_id):
        try:
            with connect() as conn:
                conn.execute(
                    """UPDATE jobs SET state='queued', node_id=NULL,
                       lease_expires=NULL, progress=0, fps=0, speed=0,
                       error=NULL WHERE id=?""", (job["id"],))
            moved += 1
        except sqlite3.IntegrityError:
            skipped += 1
    return moved, skipped


def cancel_active_jobs(library_id=None):
    """Cancel every currently active (queued/leased/running) job.

    A worker mid-encode finds out on its next progress report — the
    endpoint tells it to stop, same as cancelling one job by hand. A
    queued job is simply no longer eligible to be leased. Scoped to one
    library when given, so cancelling doesn't reach into other libraries'
    queues.
    """
    query = (f"UPDATE jobs SET state='cancelled', finished_at=? "
            f"WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})")
    params = [time.time(), *ACTIVE_STATES]
    if library_id is not None:
        query += " AND library_id=?"
        params.append(library_id)
    with connect() as conn:
        cur = conn.execute(query, params)
        return cur.rowcount


def get_job(job_id):
    with connect() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        )


def update_job(job_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE jobs SET {sets} WHERE id=?",
                     (*fields.values(), job_id))


def library_matcher(libraries):
    """A path -> library_id lookup, longest watch_path wins.

    Built once and reused per file rather than re-sorting the library
    list on every call — this runs once per file across a whole library
    during stats aggregation, so the sort only happening once matters.
    """
    by_path_length = sorted(libraries, key=lambda l: -len(l["watch_path"]))

    def library_for(path):
        for lib in by_path_length:
            if path.startswith(lib["watch_path"]):
                return lib
        return None

    return library_for


def get_cached_file(path):
    with connect() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM files WHERE path=?", (path,)).fetchone())


def cache_probe(path, info):
    with connect() as conn:
        conn.execute(
            """INSERT INTO files
               (path, size, duration, video_codec, audio_codecs,
                width, height, bitrate, video_bitrate, bit_depth, detail, probed_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 size=excluded.size, duration=excluded.duration,
                 video_codec=excluded.video_codec,
                 audio_codecs=excluded.audio_codecs,
                 width=excluded.width, height=excluded.height,
                 bitrate=excluded.bitrate,
                 video_bitrate=excluded.video_bitrate,
                 bit_depth=excluded.bit_depth,
                 detail=excluded.detail,
                 probed_at=excluded.probed_at""",
            (path, info.get("size"), info.get("duration"),
             info.get("video_codec"), json.dumps(info.get("audio_codecs", [])),
             info.get("width"), info.get("height"), info.get("bitrate"),
             info.get("video_bitrate"), info.get("bit_depth"),
             json.dumps(info.get("detail") or {}), time.time()),
        )


def record_completion(size_before, size_after):
    """Add to the lifetime tally.

    Kept separately from the job rows so clearing the completed list doesn't
    erase the record of how much space has been saved.
    """
    totals = get_settings().get("totals") or {"files": 0, "before": 0, "after": 0}
    totals["files"] = int(totals.get("files", 0)) + 1
    totals["before"] = int(totals.get("before", 0)) + int(size_before or 0)
    totals["after"] = int(totals.get("after", 0)) + int(size_after or 0)
    save_settings({"totals": totals})
    return totals


def stats():
    with connect() as conn:
        row = conn.execute(
            """SELECT
                 COUNT(*) FILTER (WHERE state='done')   AS done,
                 COUNT(*) FILTER (WHERE state='failed') AS failed,
                 COUNT(*) FILTER (WHERE state='queued') AS queued,
                 COALESCE(SUM(size_before) FILTER (WHERE state='done'), 0) AS before,
                 COALESCE(SUM(size_after)  FILTER (WHERE state='done'), 0) AS after
               FROM jobs"""
        ).fetchone()
        current = dict(row)

    # Lifetime figures survive a history clear; queue counts stay live.
    totals = get_settings().get("totals") or {}
    if totals.get("files"):
        current["done"] = max(current.get("done", 0), int(totals.get("files", 0)))
        current["before"] = int(totals.get("before", 0))
        current["after"] = int(totals.get("after", 0))
    return current


# ------------------------------------------------------------- migration

def repair_profiles():
    """Fix profiles holding empty values where a real setting is required.

    A blank audio bitrate reached FFmpeg as -b:a "" and failed every job in
    the library, so any library carrying one is corrected on startup.
    """
    fixed = 0
    for library in list_libraries():
        profile = library.get("profile") or {}
        changed = False
        for key, fallback in (("audio_bitrate", "160k"), ("audio_codec", "aac"),
                              ("video_codec", "hevc"), ("container", "mkv"),
                              ("subtitle_mode", "keep")):
            if key in profile and not str(profile[key] or "").strip():
                profile[key] = fallback
                changed = True
        if changed:
            update_library(library["id"], profile=profile)
            fixed += 1
    return fixed


def migrate():
    """Add columns to databases created by earlier versions."""
    additions = {
        "jobs": [("library_id", "INTEGER"), ("output_local", "TEXT"),
                 ("final_path", "TEXT"),
                 ("attempt", "INTEGER NOT NULL DEFAULT 1"),
                 ("size_now", "INTEGER"), ("outcome", "TEXT"),
                 ("progress_at", "REAL")],
        "libraries": [("filters", "TEXT NOT NULL DEFAULT '{}'"),
                      ("naming", "TEXT NOT NULL DEFAULT '{}'"),
                      ("originals_path", "TEXT")],
        "nodes": [("recipes", "TEXT NOT NULL DEFAULT '{}'"),
                  ("benchmarks", "TEXT NOT NULL DEFAULT '{}'"),
                  ("slots", "INTEGER"), ("cpus", "INTEGER"),
                  ("benchmarks_10bit", "TEXT NOT NULL DEFAULT '{}'")],
        "files": [("video_bitrate", "INTEGER"), ("bit_depth", "INTEGER"),
                  ("detail", "TEXT")],
    }
    with connect() as conn:
        for table, cols in additions.items():
            existing = {r["name"] for r in
                        conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# -------------------------------------------------------------- libraries

def create_library(name, watch_path, output_path, profile, original_action,
                   mirror_folders=True, skip_matching=True, filters=None,
                   naming=None, originals_path=None):
    with connect() as conn:
        cur = conn.execute(
            """INSERT INTO libraries
               (name, watch_path, output_path, profile, original_action,
                mirror_folders, skip_matching, filters, naming,
                originals_path, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (name, watch_path, output_path or None, json.dumps(profile),
             original_action, int(mirror_folders), int(skip_matching),
             json.dumps(filters or {}), json.dumps(naming or {}),
             originals_path or None, time.time()),
        )
        return cur.lastrowid


def update_library(lib_id, **fields):
    for blob in ("profile", "filters", "naming"):
        if blob in fields and not isinstance(fields[blob], str):
            fields[blob] = json.dumps(fields[blob])
    for flag in ("mirror_folders", "skip_matching", "enabled"):
        if flag in fields:
            fields[flag] = int(bool(fields[flag]))
    sets = ", ".join(f"{k}=?" for k in fields)
    with connect() as conn:
        conn.execute(f"UPDATE libraries SET {sets} WHERE id=?",
                     (*fields.values(), lib_id))


def delete_library(lib_id):
    with connect() as conn:
        conn.execute("DELETE FROM libraries WHERE id=?", (lib_id,))


def list_libraries():
    with connect() as conn:
        return [row_to_dict(r) for r in
                conn.execute("SELECT * FROM libraries ORDER BY name").fetchall()]


def get_library(lib_id):
    with connect() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM libraries WHERE id=?", (lib_id,)).fetchone())


# ------------------------------------------------- watch-folder bookkeeping

def mark_processed(path, mtime, size, library_id):
    with connect() as conn:
        conn.execute(
            """INSERT INTO processed (path, mtime, size, library_id, at)
               VALUES (?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET
                 mtime=excluded.mtime, size=excluded.size, at=excluded.at""",
            (path, mtime, size, library_id, time.time()))


def was_processed(path, mtime):
    """True if we've already handled this exact file, unchanged since."""
    with connect() as conn:
        row = conn.execute("SELECT mtime FROM processed WHERE path=?",
                           (path,)).fetchone()
    return row is not None and abs((row["mtime"] or 0) - mtime) < 1


def note_pending(path, size):
    """Track a file's size between scans so we can tell when a copy finishes."""
    with connect() as conn:
        row = conn.execute("SELECT size FROM pending WHERE path=?",
                           (path,)).fetchone()
        if row is None:
            conn.execute("INSERT INTO pending (path, size, first_seen) VALUES (?,?,?)",
                         (path, size, time.time()))
            return False
        stable = row["size"] == size
        if not stable:
            conn.execute("UPDATE pending SET size=? WHERE path=?", (size, path))
        return stable


def clear_pending(path):
    with connect() as conn:
        conn.execute("DELETE FROM pending WHERE path=?", (path,))


# --------------------------------------------------------------- settings

DEFAULT_SETTINGS = {
    "schedule": {
        "enabled": False,
        # Each window: days 0=Monday .. 6=Sunday, 24-hour clock.
        "windows": [{"days": [0, 1, 2, 3, 4, 5, 6], "start": "22:00", "end": "06:00"}],
        "finish_running": True,
    },
    "originals": {
        "enabled": False,
        "after_days": 14,
        "mode": "daily",          # daily | days | interval
        "run_at": "03:00",
        "days": [0, 1, 2, 3, 4, 5, 6],
        "interval_hours": 24,
    },
    "scan_seconds": 30,
    "auto_fail": {
        "enabled": False,
        "amount": 6,
        "unit": "hours",          # minutes | hours | days
        "stall_enabled": True,
        "stall_minutes": 30,
    },
    "totals": {"files": 0, "before": 0, "after": 0},
    # TMDB credential. Either a v3 API key or a v4 read access token.
    "tmdb": {"key": "", "enabled": False},
}


def get_settings():
    merged = json.loads(json.dumps(DEFAULT_SETTINGS))
    with connect() as conn:
        for row in conn.execute("SELECT key, value FROM settings").fetchall():
            value = parse_json(row["value"])
            if value is not None:
                merged[row["key"]] = value
    return merged


def save_settings(patch):
    with connect() as conn:
        for key, value in patch.items():
            conn.execute(
                """INSERT INTO settings (key, value) VALUES (?, ?)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value""",
                (key, json.dumps(value)))


# -------------------------------------------------------------- originals

def record_original(archived_path, job_id, library_id, final_path, size):
    with connect() as conn:
        conn.execute(
            """INSERT INTO originals
               (archived_path, job_id, library_id, final_path, size, archived_at)
               VALUES (?,?,?,?,?,?)
               ON CONFLICT(archived_path) DO UPDATE SET
                 job_id=excluded.job_id,
                 library_id=excluded.library_id,
                 size=excluded.size,
                 final_path=excluded.final_path,
                 archived_at=excluded.archived_at""",
            (archived_path, job_id, library_id, final_path, size, time.time()))


def list_originals(older_than_seconds=None):
    query = "SELECT * FROM originals"
    params = []
    if older_than_seconds is not None:
        query += " WHERE archived_at < ?"
        params.append(time.time() - older_than_seconds)
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(query, params).fetchall()]


def forget_original(archived_path):
    with connect() as conn:
        conn.execute("DELETE FROM originals WHERE archived_path=?", (archived_path,))


def originals_summary():
    """How many archived originals are being held, and how much space."""
    with connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(size),0) AS bytes FROM originals"
        ).fetchone()
        return dict(row)


def original_for_job(job_id):
    """The archived source for a job, if one was kept."""
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM originals WHERE job_id=?", (job_id,)).fetchone()
        return row_to_dict(row)


def forget_processed(path):
    """Let a path be picked up by the scanner again."""
    with connect() as conn:
        conn.execute("DELETE FROM processed WHERE path=?", (path,))


def unresolved_job_for(path):
    """The most recent Failed/Ignored/Got-bigger job still sitting for this path.

    Only those three states matter here: they leave the original file in
    place waiting for a person to look at it. Without this check, the
    scanner has no memory of a failure — was_processed() is only ever set
    on success — so every scan cycle re-queues the same broken file as a
    brand-new job, fails it again, and the Failed list refills itself.
    """
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM jobs WHERE path=? AND state IN
               ('failed','ignored','bloated') ORDER BY id DESC LIMIT 1""",
            (path,)).fetchone()
    return row_to_dict(row)


def has_job_for(path, states=None):
    """True if a job already exists for this path in any of these states."""
    query = "SELECT 1 FROM jobs WHERE path=?"
    params = [path]
    if states:
        query += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    with connect() as conn:
        return conn.execute(query + " LIMIT 1", params).fetchone() is not None


# ----------------------------------------------------------------- stats

def _resolution_bucket(width, height):
    if not width or not height:
        return "unknown"
    longest = max(width, height)
    if longest >= 3800: return "4K"
    if longest >= 2500: return "1440p"
    if longest >= 1900: return "1080p"
    if longest >= 1260: return "720p"
    return "SD"


def files_matching(attribute, value, library_id=None):
    """Files behind one composition-chart segment, for drilling in from Stats.

    Filtering happens in Python rather than SQL: video_codec is a plain
    column, but audio_codec is a JSON list per file and container/
    resolution/bit_depth are derived rather than stored, so there's no
    single SQL shape that fits all five — doing them the same way here
    keeps this one function instead of five near-duplicate ones.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, watch_path FROM libraries").fetchall()]
        files = [dict(r) for r in conn.execute("SELECT * FROM files").fetchall()]

    library_for = library_matcher(libraries)

    def audio_list(f):
        try:
            return json.loads(f.get("audio_codecs") or "[]") or ["none"]
        except (json.JSONDecodeError, TypeError):
            return ["unknown"]

    def matches(f):
        if library_id is not None and (library_for(f["path"]) or {}).get("id") != library_id:
            return False
        if attribute == "video_codec":
            return (f.get("video_codec") or "unknown") == value
        if attribute == "container":
            return (Path(f["path"]).suffix.lstrip(".").lower() or "unknown") == value
        if attribute == "resolution":
            return _resolution_bucket(f.get("width"), f.get("height")) == value
        if attribute == "bit_depth":
            return f"{f.get('bit_depth') or 8}-bit" == value
        if attribute == "audio_codec":
            return value in audio_list(f)
        return False

    out = []
    for f in files:
        if not matches(f):
            continue
        try:
            detail = json.loads(f.get("detail") or "{}")
        except (json.JSONDecodeError, TypeError):
            detail = {}
        out.append({
            "path": f["path"], "name": Path(f["path"]).name,
            "size": f.get("size"), "duration": f.get("duration"),
            "video_codec": f.get("video_codec"),
            "width": f.get("width"), "height": f.get("height"),
            "bit_depth": f.get("bit_depth"), "audio_codecs": audio_list(f),
            "detail": detail,
        })
    out.sort(key=lambda f: f["name"].lower())
    return out


def jobs_matching(encoder_used, library_id=None):
    """The completed jobs behind one encoder segment on the performance chart."""
    query = """SELECT path, size_before, size_after, encoder_used,
                      started_at, finished_at, library_id
               FROM jobs WHERE state IN ('done','bloated') AND encoder_used=?"""
    params = [encoder_used]
    if library_id is not None:
        query += " AND library_id=?"
        params.append(library_id)
    with connect() as conn:
        rows = [dict(r) for r in conn.execute(query, params).fetchall()]
    out = []
    for r in rows:
        seconds = ((r["finished_at"] - r["started_at"])
                  if r.get("started_at") and r.get("finished_at") else None)
        out.append({
            "path": r["path"], "name": Path(r["path"]).name,
            "size_before": r.get("size_before"), "size_after": r.get("size_after"),
            "seconds": seconds,
        })
    out.sort(key=lambda f: f["name"].lower())
    return out


def library_composition():
    """What's actually sitting in each library right now.

    Built from the probe cache ("files"), which is populated for every
    file a scan finds — not just ones that needed converting — so this
    reflects the real current shape of a library, not just what Forge has
    touched.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, name, watch_path FROM libraries ORDER BY name").fetchall()]
        files = [dict(r) for r in conn.execute("SELECT * FROM files").fetchall()]

    library_for = library_matcher(libraries)

    def new_bucket(name):
        return {"name": name, "files": 0, "video_codec": Counter(),
                "audio_codec": Counter(), "container": Counter(),
                "resolution": Counter(), "bit_depth": Counter()}

    per_library = {lib["id"]: new_bucket(lib["name"]) for lib in libraries}
    overall = new_bucket("All libraries")

    for f in files:
        container = Path(f["path"]).suffix.lstrip(".").lower() or "unknown"
        vcodec = f.get("video_codec") or "unknown"
        resolution = _resolution_bucket(f.get("width"), f.get("height"))
        depth = f"{f.get('bit_depth') or 8}-bit"
        try:
            audio_codecs = json.loads(f.get("audio_codecs") or "[]") or ["none"]
        except (json.JSONDecodeError, TypeError):
            audio_codecs = ["unknown"]

        for bucket in (per_library.get((library_for(f["path"]) or {}).get("id")), overall):
            if bucket is None:
                continue
            bucket["files"] += 1
            bucket["video_codec"][vcodec] += 1
            bucket["container"][container] += 1
            bucket["resolution"][resolution] += 1
            bucket["bit_depth"][depth] += 1
            for a in audio_codecs:
                bucket["audio_codec"][a] += 1

    def serialize(b):
        return {"name": b["name"], "files": b["files"],
                "video_codec": dict(b["video_codec"]),
                "audio_codec": dict(b["audio_codec"]),
                "container": dict(b["container"]),
                "resolution": dict(b["resolution"]),
                "bit_depth": dict(b["bit_depth"])}

    return {"overall": serialize(overall),
            "libraries": {lib_id: serialize(b) for lib_id, b in per_library.items()}}


def transcode_performance():
    """How conversions have actually gone, per library.

    Only jobs with both started_at and finished_at count toward timing —
    one that failed before starting has neither, and one still running
    hasn't finished, so there's nothing honest to average for either.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, name FROM libraries ORDER BY name").fetchall()]
        rows = [dict(r) for r in conn.execute(
            """SELECT library_id, encoder_used, size_before, size_after,
                      started_at, finished_at
               FROM jobs
               WHERE state IN ('done','bloated') AND started_at IS NOT NULL
                     AND finished_at IS NOT NULL""").fetchall()]

    def new_bucket(name):
        return {"name": name, "jobs": 0, "total_seconds": 0.0,
                "total_before": 0, "total_after": 0, "encoder_used": Counter()}

    per_library = {lib["id"]: new_bucket(lib["name"]) for lib in libraries}
    overall = new_bucket("All libraries")

    for r in rows:
        seconds = r["finished_at"] - r["started_at"]
        if seconds <= 0:
            continue
        before, after = r.get("size_before") or 0, r.get("size_after") or 0
        encoder = r.get("encoder_used") or "remux"
        for bucket in (per_library.get(r["library_id"]), overall):
            if bucket is None:
                continue
            bucket["jobs"] += 1
            bucket["total_seconds"] += seconds
            bucket["total_before"] += before
            bucket["total_after"] += after
            bucket["encoder_used"][encoder] += 1

    def serialize(b):
        saved = ((1 - b["total_after"] / b["total_before"]) * 100
                if b["total_before"] else 0)
        return {"name": b["name"], "jobs": b["jobs"],
                "avg_seconds": (b["total_seconds"] / b["jobs"]) if b["jobs"] else 0,
                "avg_saved_percent": saved,
                "encoder_used": dict(b["encoder_used"])}

    return {"overall": serialize(overall),
            "libraries": {lib_id: serialize(b) for lib_id, b in per_library.items()}}
