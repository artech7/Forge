"""SQLite storage for Forge. Single file, WAL mode, no ORM."""
import auth
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
    enabled      INTEGER NOT NULL DEFAULT 1,
    role         TEXT NOT NULL DEFAULT 'both',  -- both|transcode|housekeeping
    housekeeping_slots INTEGER NOT NULL DEFAULT 0  -- of this node's slots, how many
                                                    -- stay reserved for loudness work
                                                    -- regardless of the conversion backlog
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
    phase         TEXT,                 -- what the worker is doing right now
    outcome       TEXT,                 -- why it landed in the bloated list
    output_local  TEXT,                 -- where the worker wrote it, in node space
    final_path    TEXT,                 -- where the server put it, in server space
    created_at    REAL NOT NULL,
    started_at    REAL,
    finished_at   REAL,
    bounces       INTEGER NOT NULL DEFAULT 0, -- consecutive lease expiries with no check-in
    queue_order   REAL,                       -- manual position; NULL means "natural (by id)"
    -- convert|level|measure, derived from spec by job_kind() at enqueue.
    -- Denormalised deliberately: the scheduler has to ask "is there a
    -- conversion waiting?" without pulling a backlog of loudness work
    -- into memory first, and that has to stay cheap with a queue
    -- thousands deep.
    kind          TEXT NOT NULL DEFAULT 'convert'
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    label      TEXT                  -- which browser this was, roughly
);

CREATE INDEX IF NOT EXISTS idx_sessions_expiry ON sessions(expires_at);

CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state);
-- NOTE: idx_jobs_queue is deliberately NOT here. It indexes "kind",
-- which migrate() adds to databases made before that column existed --
-- and init() runs this whole script first, on every start. On an
-- existing database CREATE TABLE IF NOT EXISTS is a no-op but the index
-- statement still runs, against a table with no such column, and the
-- server cannot start at all. Anything indexing a migrated column
-- belongs in migrate(), after the ALTER that adds it.
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_active
    ON jobs(path) WHERE state IN ('queued','leased','running');
-- Safe here, unlike idx_jobs_queue above: "path" has been part of the
-- jobs table since the first version, so this indexes a column that
-- exists in every database this will ever run against.
--
-- idx_jobs_active can't serve a lookup by path on its own, because it
-- only contains rows in the three active states. The scanner's
-- unresolved_job_for() asks the opposite question -- is there a
-- failed/ignored/bloated job for this path -- so without this index
-- SQLite falls back to idx_jobs_state and walks every failed, ignored
-- and bloated row in the table, once per file, on every scan. That is
-- linear in the size of the backlog and the scanner runs every 30
-- seconds, so the cost grows as the backlog does: measured at 0.37ms
-- per file against 2,000 jobs and 6.16ms against 64,000. With this
-- index it stays flat at about 0.15ms.
CREATE INDEX IF NOT EXISTS idx_jobs_path ON jobs(path);
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


def set_housekeeping_slots(node_id, slots):
    """How many of this node's slots stay reserved for loudness work,
    immune to the usual "hold back until nothing real is waiting"
    behaviour. Clamped to what the node actually has, since reserving more
    than its total slots doesn't mean anything.
    """
    total = node_slots(node_id)
    slots = max(0, min(total, int(slots)))
    with connect() as conn:
        conn.execute("UPDATE nodes SET housekeeping_slots=? WHERE id=?",
                     (slots, node_id))
    return slots


def node_active_jobs(node_id):
    """This node's currently leased/running jobs, for judging how much of
    its reserved housekeeping capacity is already spoken for."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM jobs WHERE node_id=? AND state IN ('leased','running')",
            (node_id,)).fetchall()
    return [row_to_dict(r) for r in rows]


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
    """Returns job id, or None if this path already has an active job.

    Only one active job is allowed per path, which is what stops the
    scanner queueing the same file twice. But that also meant a queued
    loudness measurement could block a real conversion for the same
    file — and with a measuring backlog thousands deep, that blocked a
    lot of them. Loudness work is housekeeping by design, so real work
    displaces it rather than being turned away: the measurement is
    cancelled and comes back on the next pass, since a measurement is
    cheap to redo and a conversion is what the person actually asked
    for.
    """
    def insert(conn):
        cur = conn.execute(
            """INSERT INTO jobs
               (path, library_id, spec, state, size_before, attempt,
                created_at, kind)
               VALUES (?, ?, ?, 'queued', ?, ?, ?, ?)""",
            (path, library_id, json.dumps(spec), size_before, attempt,
             time.time(), job_kind(spec)),
        )
        return cur.lastrowid

    with connect() as conn:
        try:
            return insert(conn)
        except sqlite3.IntegrityError:
            pass

        # Real work only: one housekeeping job never displaces another.
        if job_kind(spec) != "convert":
            return None

        blocking = conn.execute(
            f"""SELECT id, spec, state FROM jobs WHERE path=? AND state IN
               ({','.join('?' * len(ACTIVE_STATES))})""",
            (path, *ACTIVE_STATES)).fetchall()
        if not blocking:
            return None
        # Only a job still sitting in the queue can be set aside. One
        # already leased or running is mid-flight on a worker, and
        # cancelling that to queue something else wastes the work
        # already done and leaves a scratch file behind — waiting the
        # few minutes for it to finish is strictly better.
        if any(r["state"] != "queued"
               or job_kind(parse_json(r["spec"], {})) == "convert"
               for r in blocking):
            return None
        ids = [r["id"] for r in blocking]
        conn.execute(
            f"UPDATE jobs SET state='cancelled', finished_at=?, "
            f"outcome='Set aside so a conversion could be queued' "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            [time.time(), *ids])
        try:
            return insert(conn)
        except sqlite3.IntegrityError:
            return None


ACTIVE_STATES = ("queued", "leased", "running")
# Job kinds, for splitting a long waiting list into something readable.
# A spec marker rather than a column: these are all ordinary jobs, and
# what makes one "loudness work" is what it was asked to do.
JOB_KINDS = {
    "convert": "Conversions",
    "measure": "Loudness measuring",
    "level": "Loudness levelling",
}


def job_kind(spec):
    spec = spec or {}
    if spec.get("measure"):
        return "measure"
    if spec.get("level_only"):
        return "level"
    return "convert"


VIEWS = {
    # Split so the short list of what's actually running isn't buried in
    # however many thousand files are still waiting their turn.
    "working": ("leased", "running"),
    "waiting": ("queued",),
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


def _filter_kind(jobs, kind):
    if not kind or kind == "all":
        return jobs
    return [j for j in jobs if job_kind(j.get("spec")) == kind]


def queued_by_kind(library_id=None):
    """How many jobs are waiting, per kind."""
    jobs = list_jobs(states=["queued"], limit=20000, library_id=library_id)
    out = {k: 0 for k in JOB_KINDS}
    for j in jobs:
        out[job_kind(j.get("spec"))] += 1
    out["all"] = len(jobs)
    return out


def create_session(label=None, days=30):
    """A new signed-in browser. Returns the cookie value."""
    token = auth.new_token()
    now = time.time()
    with connect() as conn:
        conn.execute(
            "INSERT INTO sessions (token, created_at, expires_at, label) "
            "VALUES (?,?,?,?)",
            (token, now, now + days * 86400, (label or "")[:200]))
    return token


def session_valid(token):
    """True if this cookie is a live session. Expired ones are swept."""
    if not token:
        return False
    with connect() as conn:
        row = conn.execute(
            "SELECT expires_at FROM sessions WHERE token=?", (token,)).fetchone()
        if not row:
            return False
        if row["expires_at"] < time.time():
            conn.execute("DELETE FROM sessions WHERE token=?", (token,))
            return False
    return True


def end_session(token):
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE token=?", (token,))


def end_all_sessions():
    """Sign every browser out — used when the password changes."""
    with connect() as conn:
        conn.execute("DELETE FROM sessions")


def purge_expired_sessions():
    with connect() as conn:
        conn.execute("DELETE FROM sessions WHERE expires_at < ?", (time.time(),))


def count_sessions():
    with connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM sessions WHERE expires_at >= ?",
            (time.time(),)).fetchone()[0]


def next_queued(kind, limit=200):
    """The oldest queued jobs of one kind, in the order they'd be run.

    Separate from list_jobs() so that asking "is there a conversion
    waiting?" costs one indexed lookup rather than reading a window of
    the whole queue and sorting it out afterwards. With a measuring
    backlog thousands deep, that window filled up with measuring work
    and conversions behind it were never even considered.
    """
    with connect() as conn:
        rows = conn.execute(
            """SELECT * FROM jobs WHERE state='queued' AND kind=?
               ORDER BY COALESCE(queue_order, id) ASC, id ASC LIMIT ?""",
            (kind, limit)).fetchall()
    return [row_to_dict(r) for r in rows]


def list_jobs(states=None, limit=200, offset=0, library_id=None, q=None,
              sort=None):
    query = "SELECT * FROM jobs"
    clauses, params = [], []
    if states:
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if library_id is not None:
        clauses.append("library_id=?")
        params.append(library_id)
    if q:
        # SQLite's LIKE is case-insensitive for ASCII by default, which
        # covers the common case (filenames) without extra handling.
        clauses.append("path LIKE ?")
        params.append(f"%{q}%")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    # Whitelisted rather than interpolated, since this lands directly in
    # SQL. "growth" is how much bigger the result got than the source —
    # the thing you'd actually triage the Got Bigger list by, and not a
    # stored column, so it's computed here.
    SORTS = {
        "newest": "id DESC",
        "oldest": "id ASC",
        "largest": "size_before DESC",
        "smallest": "size_before ASC",
        "growth": "(COALESCE(size_after,0) - COALESCE(size_before,0)) DESC",
        "name": "path ASC",
    }
    if sort in SORTS:
        query += f" ORDER BY {SORTS[sort]}"
    else:
        # Active work reads best oldest-first (that's the running order);
        # history reads best newest-first. queue_order lets a person move
        # a specific job up or down within that order by hand — it's NULL
        # until someone actually does that, so id is still what breaks
        # ties (and is the whole order for anyone who never touches it).
        ascending = states and set(states) <= set(ACTIVE_STATES)
        query += (" ORDER BY COALESCE(queue_order, id) ASC, id ASC" if ascending
                  else " ORDER BY id DESC")
    query += " LIMIT ? OFFSET ?"
    params.extend([limit, offset])
    with connect() as conn:
        return [row_to_dict(r) for r in conn.execute(query, params).fetchall()]


def count_jobs(states=None, library_id=None, q=None):
    query = "SELECT COUNT(*) FROM jobs"
    clauses, params = [], []
    if states:
        clauses.append(f"state IN ({','.join('?' * len(states))})")
        params.extend(states)
    if library_id is not None:
        clauses.append("library_id=?")
        params.append(library_id)
    if q:
        clauses.append("path LIKE ?")
        params.append(f"%{q}%")
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    with connect() as conn:
        return conn.execute(query, params).fetchone()[0]


def job_counts(library_id=None, q=None):
    return {view: count_jobs(states, library_id, q) for view, states in VIEWS.items()}


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


def delete_jobs(states, library_id=None, q=None):
    query = f"DELETE FROM jobs WHERE state IN ({','.join('?' * len(states))})"
    params = list(states)
    if library_id is not None:
        query += " AND library_id=?"
        params.append(library_id)
    if q:
        query += " AND path LIKE ?"
        params.append(f"%{q}%")
    with connect() as conn:
        cur = conn.execute(query, params)
        return cur.rowcount


def requeue_jobs(states, library_id=None, q=None):
    """Put jobs back in the queue, skipping any whose path is already active.

    The partial unique index would reject a duplicate, so those are counted
    and reported rather than raising. q narrows this to whatever's actually
    on screen when a bulk button is search-filtered — without it, a "Retry
    all 3" clicked after searching would silently act on every job in the
    view instead of just the 3 the search turned up.
    """
    moved, skipped = 0, 0
    for job in list_jobs(states=list(states), limit=2000, library_id=library_id, q=q):
        try:
            with connect() as conn:
                conn.execute(
                    """UPDATE jobs SET state='queued', node_id=NULL,
                       lease_expires=NULL, progress=0, fps=0, speed=0,
                       phase=NULL,
                       error=NULL, started_at=NULL, finished_at=NULL,
                       bounces=0 WHERE id=?""", (job["id"],))
            moved += 1
        except sqlite3.IntegrityError:
            skipped += 1
    return moved, skipped


def move_job_to_top(job_id):
    """Put one queued job ahead of everything else waiting.

    Just needs to beat the current lowest position, not renumber anything
    else — so this is one UPDATE regardless of how long the queue is.
    """
    with connect() as conn:
        row = conn.execute(
            "SELECT MIN(COALESCE(queue_order, id)) AS m FROM jobs WHERE state='queued'"
        ).fetchone()
        floor = (row["m"] if row and row["m"] is not None else 0) - 1
        conn.execute("UPDATE jobs SET queue_order=? WHERE id=?", (floor, job_id))


def reorder_jobs(ids):
    """Set these jobs' relative order to match the sequence given.

    Anchored at the lowest position already held by any job in the list,
    so reordering (a drag within one page of the waiting list, say)
    doesn't disturb anything before or after that group — just the order
    within it.
    """
    ids = [int(i) for i in ids]
    if not ids:
        return
    with connect() as conn:
        placeholders = ",".join("?" * len(ids))
        rows = conn.execute(
            f"""SELECT id, COALESCE(queue_order, id) AS q FROM jobs
                WHERE id IN ({placeholders})""", ids).fetchall()
        current = {r["id"]: r["q"] for r in rows}
        if not current:
            return
        base = min(current.values())
        for position, job_id in enumerate(ids):
            if job_id in current:
                conn.execute("UPDATE jobs SET queue_order=? WHERE id=?",
                            (base + position, job_id))


def cancel_loudness_jobs(library_id=None):
    """Cancel only measurement-only jobs, leaving real conversions in the
    same queue untouched.

    One real difference from cancel_active_jobs: a real encode job polls
    for a stop signal on every progress report and can be interrupted
    mid-file. A measurement job is one blocking ffmpeg call with no such
    check — a queued one is cancelled instantly (it just never gets
    leased), but one already running will finish that single file before
    the worker notices anything changed. job_measured() checks for this
    and won't un-cancel a job that's already been marked cancelled by the
    time it reports back.
    """
    query = f"SELECT id, spec FROM jobs WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})"
    params = list(ACTIVE_STATES)
    if library_id is not None:
        query += " AND library_id=?"
        params.append(library_id)
    with connect() as conn:
        rows = conn.execute(query, params).fetchall()
        ids = [r["id"] for r in rows if parse_json(r["spec"], {}).get("measure") == "loudness"]
        if ids:
            conn.execute(
                f"UPDATE jobs SET state='cancelled', finished_at=? "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                [time.time(), *ids])
    return len(ids)


def count_active_by_kind(library_id=None):
    """How much active work there is, split into conversions and loudness.

    Counted in the database rather than from the live job list the
    interface holds: that list is capped at 50 for the sake of the
    websocket, so anything reading its length badly understates a large
    queue — and a "cancel everything" button that says 50 when there are
    three thousand is worse than no button.
    """
    query = (f"SELECT spec FROM jobs WHERE state IN "
            f"({','.join('?' * len(ACTIVE_STATES))})")
    params = list(ACTIVE_STATES)
    if library_id is not None:
        query += " AND library_id=?"
        params.append(library_id)
    conversions = housekeeping = 0
    with connect() as conn:
        for row in conn.execute(query, params):
            spec = parse_json(row["spec"], {}) or {}
            if spec.get("measure") or spec.get("level_only"):
                housekeeping += 1
            else:
                conversions += 1
    return {"conversions": conversions, "housekeeping": housekeeping,
            "total": conversions + housekeeping}


def cancel_active_jobs(library_id=None, kind="all"):
    """Cancel every currently active (queued/leased/running) job.

    A worker mid-encode finds out on its next progress report — the
    endpoint tells it to stop, same as cancelling one job by hand. A
    queued job is simply no longer eligible to be leased. Scoped to one
    library when given, so cancelling doesn't reach into other libraries'
    queues.
    """
    if kind == "all":
        query = (f"UPDATE jobs SET state='cancelled', finished_at=? "
                f"WHERE state IN ({','.join('?' * len(ACTIVE_STATES))})")
        params = [time.time(), *ACTIVE_STATES]
        if library_id is not None:
            query += " AND library_id=?"
            params.append(library_id)
        with connect() as conn:
            return conn.execute(query, params).rowcount

    # Telling conversions from loudness work means reading each spec, so
    # these are selected first and cancelled by id.
    select = (f"SELECT id, spec FROM jobs WHERE state IN "
             f"({','.join('?' * len(ACTIVE_STATES))})")
    params = list(ACTIVE_STATES)
    if library_id is not None:
        select += " AND library_id=?"
        params.append(library_id)
    with connect() as conn:
        rows = conn.execute(select, params).fetchall()
        ids = []
        for row in rows:
            spec = parse_json(row["spec"], {}) or {}
            is_housekeeping = bool(spec.get("measure") or spec.get("level_only"))
            if (kind == "housekeeping") == is_housekeeping:
                ids.append(row["id"])
        if not ids:
            return 0
        conn.execute(
            f"UPDATE jobs SET state='cancelled', finished_at=? "
            f"WHERE id IN ({','.join('?' * len(ids))})",
            [time.time(), *ids])
        return len(ids)


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


def files_missing_language(kind, language=None, library_id=None):
    """Files with no audio or subtitle track in a wanted language.

    Pulled straight from the same detail blob a regular scan already
    produces — this isn't a new measurement pass like loudness, so
    there's nothing to queue or wait on, and no worker involvement at
    all. Purely informational: Forge can't manufacture a subtitle track
    that doesn't exist, so this is a report to act on yourself (Bazarr,
    a different release, etc.), not something with a "fix it" button.

    With no language given, each library is judged against the languages
    it was actually set up to want, rather than one hardcoded choice —
    a library configured for German or Japanese should be reported on in
    those terms, not told everything is "missing English".
    """
    if kind not in ("audio", "subtitle"):
        raise ValueError("kind must be 'audio' or 'subtitle'")
    key = "audio_tracks" if kind == "audio" else "subtitle_tracks"
    profile_key = "audio_languages_list" if kind == "audio" else "subtitle_languages"
    wanted_override = [language.lower()] if language else None

    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, watch_path, profile FROM libraries").fetchall()]
        rows = [dict(r) for r in conn.execute(
            "SELECT path, detail FROM files WHERE detail IS NOT NULL").fetchall()]

    def wanted_for(lib):
        if wanted_override:
            return wanted_override
        langs = ((lib or {}).get("profile") or {}).get(profile_key) or []
        return [str(l).lower() for l in langs] or ["eng"]

    library_for = library_matcher(libraries)
    out = []
    for f in rows:
        lib = library_for(f["path"])
        if library_id is not None and (lib or {}).get("id") != library_id:
            continue
        try:
            detail = json.loads(f.get("detail") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if key not in detail:
            continue   # never scanned with the version that captures this
        tracks = detail.get(key) or []
        present = sorted({(t.get("language") or "").lower() for t in tracks} - {""})
        wanted = wanted_for(lib)
        # Any one of the wanted languages being present is enough — a
        # library that accepts English or Japanese isn't missing anything
        # just because it only has one of them.
        if not any(w in present for w in wanted):
            out.append({"path": f["path"], "name": Path(f["path"]).name,
                       "languages_present": present, "wanted": wanted})
    out.sort(key=lambda f: f["name"].lower())
    return out


def files_without_audio(library_id=None):
    """Files with no audio stream at all.

    A different question from files_missing_language(): that asks whether
    a wanted language is there, this asks whether there is any audio
    whatsoever. Objectively broken rather than a preference unmet — the
    only real fix is a different copy of the file, which is why this is
    the one that gets an action attached to it.

    Read from the probe cache, so only files actually probed can appear:
    a file nobody has looked at yet has no audio codecs on record either,
    and that is not the same thing as having none.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, name, watch_path FROM libraries").fetchall()]
        rows = [dict(r) for r in conn.execute(
            """SELECT path, size, video_codec, audio_codecs FROM files
               WHERE probed_at IS NOT NULL""").fetchall()]

    library_for = library_matcher(libraries)
    out = []
    for f in rows:
        if parse_json(f.get("audio_codecs"), []) or []:
            continue
        lib = library_for(f["path"]) or {}
        if library_id is not None and lib.get("id") != library_id:
            continue
        out.append({
            "path": f["path"], "name": Path(f["path"]).name,
            "size": f.get("size"), "video_codec": f.get("video_codec"),
            "library_id": lib.get("id"), "library_name": lib.get("name"),
        })
    out.sort(key=lambda f: f["name"].lower())
    return out


def library_inventory(library_id):
    """Every scanned file in one library with the bits Standardize needs.

    Deliberately thin — codec, audio codecs and container only. Deciding
    what counts as "out of line" happens against the library's own
    settings, which the interface already has, so shipping the full
    detail blob for thousands of files would be wasted bandwidth.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, watch_path FROM libraries").fetchall()]
        rows = [dict(r) for r in conn.execute(
            "SELECT path, video_codec, audio_codecs FROM files").fetchall()]

    library_for = library_matcher(libraries)
    out = []
    for f in rows:
        if (library_for(f["path"]) or {}).get("id") != library_id:
            continue
        out.append({
            "path": f["path"], "name": Path(f["path"]).name,
            "video_codec": f.get("video_codec"),
            "audio_codecs": parse_json(f.get("audio_codecs"), []) or [],
            "container": Path(f["path"]).suffix.lstrip(".").lower(),
        })
    out.sort(key=lambda f: f["name"].lower())
    return out


def files_missing_chapters(library_id=None):
    """Files with no chapter markers at all.

    Same as files_missing_language: read from the existing structural
    scan, not a new pass. A file probed before chapter counting was
    added simply won't have the key yet and is skipped rather than
    guessed at — it'll show up correctly on its next regular scan.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, watch_path FROM libraries").fetchall()]
        rows = [dict(r) for r in conn.execute(
            "SELECT path, detail, duration FROM files WHERE detail IS NOT NULL").fetchall()]

    library_for = library_matcher(libraries)
    out = []
    for f in rows:
        if library_id is not None and (library_for(f["path"]) or {}).get("id") != library_id:
            continue
        try:
            detail = json.loads(f.get("detail") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        if "chapters" not in detail:
            continue
        if detail["chapters"] == 0:
            out.append({"path": f["path"], "name": Path(f["path"]).name,
                       "duration": f.get("duration")})
    out.sort(key=lambda f: f["name"].lower())
    return out


def files_with_loudness(library_id=None):
    """Every file with a loudness reading on record.

    "Needs leveling" isn't decided here — where to draw the line on
    loudness range (how wide a volume swing counts as a problem) is a
    judgment call, so the raw measurements are returned and the interface
    applies whatever threshold it wants, rather than one fixed opinion
    getting baked into the query.
    """
    with connect() as conn:
        libraries = [row_to_dict(r) for r in conn.execute(
            "SELECT id, watch_path FROM libraries").fetchall()]
        rows = [dict(r) for r in conn.execute(
            "SELECT path, detail FROM files WHERE detail IS NOT NULL").fetchall()]

    library_for = library_matcher(libraries)
    measured = []
    for f in rows:
        if library_id is not None and (library_for(f["path"]) or {}).get("id") != library_id:
            continue
        try:
            detail = json.loads(f.get("detail") or "{}")
        except (json.JSONDecodeError, TypeError):
            detail = {}
        loud = detail.get("loudness")
        if not loud:
            continue
        # A reading is only actionable if the file is still there. Stale
        # rows are pruned on the next scan, but not listing them in the
        # meantime avoids offering work that can only fail.
        if not Path(f["path"]).exists():
            continue
        measured.append({
            "path": f["path"], "name": Path(f["path"]).name,
            "integrated": loud.get("integrated"), "range": loud.get("range"),
            "true_peak": loud.get("true_peak"),
        })
    measured.sort(key=lambda f: -(f["range"] or 0))
    return measured


def set_node_role(node_id, role):
    if role not in ("both", "transcode", "housekeeping"):
        raise ValueError("role must be both, transcode or housekeeping")
    with connect() as conn:
        conn.execute("UPDATE nodes SET role=? WHERE id=?", (role, node_id))


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


def set_file_detail(path, detail):
    """Merge into a file's detail blob directly, without a full re-probe.

    Loudness measurement runs as its own pass and shouldn't need the
    structural scan to have happened first — if this file has never been
    probed at all, this still creates a minimal row for it.
    """
    with connect() as conn:
        conn.execute(
            """INSERT INTO files (path, detail, probed_at) VALUES (?,?,?)
               ON CONFLICT(path) DO UPDATE SET detail=?, probed_at=?""",
            (path, json.dumps(detail), time.time(), json.dumps(detail), time.time()))


def protected_job_ids(grace=900):
    """Job ids whose scratch file must not be swept, ever.

    Two groups. Active work, obviously. And anything that finished very
    recently: a job auto-failed or cancelled by the server goes terminal
    at once, but the worker only learns that on its next check-in and
    keeps writing until then, so for a minute or two a finished job
    still owns a file on disk.

    Deliberately unlimited. This used to be list_jobs(limit=500), which
    orders by queue position -- so with a few thousand loudness jobs
    waiting, the window filled entirely with work that hadn't started
    and the job actually encoding fell outside it. The sweep then read
    that as "nobody owns this file" and deleted the output from under a
    running FFmpeg. On Linux the unlink succeeds silently even though
    the worker still holds the handle, so nothing anywhere objects.

    Cheap despite the missing limit: one indexed scan returning integers.
    """
    placeholders = ",".join("?" * len(ACTIVE_STATES))
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT id FROM jobs
                WHERE state IN ({placeholders})
                   OR (finished_at IS NOT NULL AND finished_at > ?)""",
            (*ACTIVE_STATES, time.time() - grace)).fetchall()
    return {r["id"] for r in rows}


def purge_work_file_jobs():
    """Drop jobs that were queued against Forge's own scratch files.

    These only exist because the scanner used to pick up its own
    in-progress output. Each one occupies a worker slot converting a
    half-written file, so they're cleared out rather than left to run.
    """
    with connect() as conn:
        cur = conn.execute(
            "DELETE FROM jobs WHERE path LIKE '%/.forge-%' "
            "OR path LIKE '%\\.forge-%' OR path LIKE '%.forge-part%'")
        return cur.rowcount


def forget_cached_file(path):
    """Drop one probe-cache row, for a file that has been replaced."""
    with connect() as conn:
        conn.execute("DELETE FROM files WHERE path=?", (path,))


def forget_missing_files(watch_path):
    """Drop probe-cache rows under watch_path whose file is gone.

    Forge renames files as part of converting them, so the pre-rename
    path stays in the cache forever and anything attached to it — a
    loudness reading especially — points at a file that no longer
    exists. That's what produces a Library Health list offering work on
    files it can't actually touch.

    Deliberately scoped to one library and only called when that
    library's folder is readable: an unmounted share looks exactly like
    "every file was deleted", and wiping the cache on that basis would
    throw away every measurement in it.
    """
    prefix = str(watch_path)
    with connect() as conn:
        rows = [r["path"] for r in conn.execute(
            "SELECT path FROM files WHERE path LIKE ?", (prefix + "%",)).fetchall()]
        gone = [p for p in rows if not Path(p).exists()]
        if gone:
            conn.executemany("DELETE FROM files WHERE path=?",
                             [(p,) for p in gone])
    return len(gone)


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
                 ("progress_at", "REAL"), ("phase", "TEXT"),
                 ("bounces", "INTEGER NOT NULL DEFAULT 0"),
                 ("queue_order", "REAL"),
                 ("kind", "TEXT NOT NULL DEFAULT 'convert'")],
        "libraries": [("filters", "TEXT NOT NULL DEFAULT '{}'"),
                      ("naming", "TEXT NOT NULL DEFAULT '{}'"),
                      ("originals_path", "TEXT")],
        "nodes": [("recipes", "TEXT NOT NULL DEFAULT '{}'"),
                  ("benchmarks", "TEXT NOT NULL DEFAULT '{}'"),
                  ("slots", "INTEGER"), ("cpus", "INTEGER"),
                  ("benchmarks_10bit", "TEXT NOT NULL DEFAULT '{}'"),
                  ("role", "TEXT NOT NULL DEFAULT 'both'"),
                  ("housekeeping_slots", "INTEGER NOT NULL DEFAULT 0")],
        "files": [("video_bitrate", "INTEGER"), ("bit_depth", "INTEGER"),
                  ("detail", "TEXT")],
    }
    added = set()
    with connect() as conn:
        for table, cols in additions.items():
            existing = {r["name"] for r in
                        conn.execute(f"PRAGMA table_info({table})").fetchall()}
            for name, decl in cols:
                if name not in existing:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                    added.add((table, name))
        # Every existing row defaulted to 'convert' when the column was
        # added. Left that way, a queue full of older loudness jobs would
        # all look like conversions and jump the priority order they were
        # meant to sit behind.
        if ("jobs", "kind") in added:
            rows = conn.execute("SELECT id, spec FROM jobs").fetchall()
            fixed = [(job_kind(parse_json(r["spec"], {})), r["id"])
                     for r in rows]
            fixed = [(k, i) for k, i in fixed if k != "convert"]
            conn.executemany("UPDATE jobs SET kind=? WHERE id=?", fixed)
            if fixed:
                print(f"migrate: labelled {len(fixed)} existing loudness job(s)")
        # Created here rather than in SCHEMA: it indexes a column that
        # only exists after the ALTER above. See the note in SCHEMA.
        conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_queue "
                     "ON jobs(state, kind, queue_order, id)")


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


def was_processed(path, mtime, size):
    """True if we've already handled this exact file, unchanged since.

    mtime alone isn't enough: SMB copies routinely preserve or coarsen
    mtimes, so a replacement file landing on the same path can land
    within the 1-second tolerance below and get mistaken for the file
    already on record. Size is stored for exactly this comparison.
    """
    with connect() as conn:
        row = conn.execute("SELECT mtime, size FROM processed WHERE path=?",
                           (path,)).fetchone()
    return (row is not None and abs((row["mtime"] or 0) - mtime) < 1
            and row["size"] == size)


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
    # Loudness measuring and levelling only run when no real conversion
    # is queued or running anywhere. Off means they merely go last,
    # which still lets them fill a spare slot alongside a transcode.
    "housekeeping_when_idle": True,
    # Bazarr owns finding subtitles; Forge only points it at files whose
    # tracks are missing. Stored globally rather than per library since
    # one Bazarr instance normally covers everything.
    "bazarr": {"url": "", "api_key": "", "path_from": "", "path_to": ""},
    # Same reasoning as Bazarr: one Radarr and one Sonarr normally cover
    # everything, so the connection belongs here rather than being
    # retyped into every library. Which of them a library belongs to —
    # and how its paths translate — stays on the library, because one
    # Sonarr routinely sees two libraries at two different paths
    # (/tvshows and /anime, say) and a single global mapping couldn't
    # describe both.
    # Empty username means no login has been set up, and Forge stays
    # open — exactly as it was before this existed. That's deliberate:
    # updating the server should never lock someone out of their own
    # queue, and the person has to choose to turn it on.
    #
    # "password" holds a scrypt hash, never the password itself.
    # node_token is what workers authenticate with, since a worker
    # can't type a password and shouldn't be trusted with the admin's.
    "auth": {"username": "", "password": "", "node_token": ""},
    "radarr": {"url": "", "api_key": ""},
    "sonarr": {"url": "", "api_key": ""},
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


def original_for_path(path):
    """The archived original standing behind whatever currently sits at
    this path, if any.

    Matches on final_path rather than the archived path itself: a rework
    job's source *is* an earlier conversion's output, so this is how a
    second or third pass on the same file finds the original that's
    already safely archived, instead of losing track of it or archiving a
    duplicate over it. Most recent match wins, in case a path was ever
    reused for something unrelated.
    """
    with connect() as conn:
        row = conn.execute(
            """SELECT * FROM originals WHERE final_path=?
               ORDER BY archived_at DESC LIMIT 1""", (path,)).fetchone()
        return row_to_dict(row)


def update_original(archived_path, job_id, final_path):
    """Point an already-archived original at whichever job and output most
    recently reworked it, without touching the archived bytes themselves."""
    with connect() as conn:
        conn.execute(
            "UPDATE originals SET job_id=?, final_path=? WHERE archived_path=?",
            (job_id, final_path, archived_path))


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


def paths_with_failed_measurement():
    """Files whose loudness measurement already failed and is waiting on
    a person.

    The same memory unresolved_job_for() gives the scanner, but for the
    automatic loudness loop and as one set rather than a query per file —
    that loop walks an entire library, so asking per file would double
    its database work for no reason.

    Measurement jobs only: a file whose *conversion* failed may still
    measure perfectly well, and shouldn't be written off for it.
    """
    with connect() as conn:
        rows = conn.execute(
            "SELECT path, spec FROM jobs WHERE state IN ('failed','ignored')"
        ).fetchall()
    return {row["path"] for row in rows
            if (parse_json(row["spec"], {}) or {}).get("measure")}


def has_job_for(path, states=None, exclude_id=None):
    """True if a job already exists for this path in any of these states."""
    query = "SELECT 1 FROM jobs WHERE path=?"
    params = [path]
    if states:
        query += f" AND state IN ({','.join('?' * len(states))})"
        params.extend(states)
    if exclude_id is not None:
        query += " AND id != ?"
        params.append(exclude_id)
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
