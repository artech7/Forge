"""Job matching. Rules describe intent; nodes advertise encoders.

The scheduler's only real job is answering: which of these idle nodes can
satisfy this spec, and should it read the file directly or have it streamed?
"""
import time
import db

LEASE_SECONDS = 120

# A job whose lease expires this many times in a row without a single
# progress check-in in between isn't a slow encode, it's stuck — most often
# FFmpeg hanging on a specific file with nothing coming out on stdout to
# report. LEASE_SECONDS is far shorter than the (opt-in, minutes-to-hours)
# auto_fail thresholds, so left alone a job like that never survives one
# lease long enough for auto_fail to ever see it: it just gets handed back
# out and re-leased forever, camping at the front of the oldest-first queue
# and starving everything behind it. This is the backstop that applies
# regardless of auto_fail being configured at all.
BOUNCE_LIMIT = 5

# How many jobs of one kind to consider per lease. Only the first one
# this node can actually run is taken, so this is just enough headroom
# to skip past jobs it can't (wrong encoder, file not reachable).
TIER_LIMIT = 200

# Intent codec -> encoder ids that satisfy it, in preference order.
# Hardware first: on a homelab, wall-clock beats the marginal quality gain.
CODEC_FAMILIES = {
    "hevc": ["hevc_nvenc", "hevc_qsv", "hevc_amf", "hevc_vaapi",
             "hevc_videotoolbox", "libx265"],
    "h264": ["h264_nvenc", "h264_qsv", "h264_amf", "h264_vaapi",
             "h264_videotoolbox", "libx264"],
    "av1":  ["av1_nvenc", "av1_qsv", "libsvtav1"],
}

# Remote nodes pay a transfer cost, so only hand them work worth the trip.
REMOTE_MIN_BYTES = 512 * 1024 * 1024


def _slashes(path):
    """One separator style, so Windows and Unix paths can be compared.

    A Windows worker reports C:\\Media\\file.mkv while its mount is written
    C:/Media. Without normalising, the two never match and the finished file
    is never placed.
    """
    return (path or "").replace("\\", "/").rstrip("/")


def resolve_path(node, path):
    """Return (transport, path_for_node).

    A node lists its mounts as {server: '/media', local: '/mnt/nas/media'}.
    If the file lives under a mapped prefix the node opens it directly;
    otherwise the server streams it and takes the result back.
    """
    normalised = _slashes(path)
    # Longest prefix first: a node with both /media and /media/4k mounted
    # must match the more specific one, or every file under it resolves
    # through the coarser mount instead (same hazard db.library_matcher
    # guards against for library watch_paths).
    mounts = sorted(node.get("mounts", []),
                     key=lambda m: -len(_slashes(m.get("server", ""))))
    for mount in mounts:
        server_prefix = _slashes(mount.get("server", ""))
        local_prefix = _slashes(mount.get("local", ""))
        if server_prefix and normalised.startswith(server_prefix + "/"):
            return "local", local_prefix + normalised[len(server_prefix):]
    return "stream", path


def node_can_encode(node, spec):
    # Copying the video stream, or a measurement-only pass with nothing to
    # encode at all, needs no encoder — any node will do.
    if spec.get("codec") == "copy" or spec.get("measure"):
        return True
    wanted = CODEC_FAMILIES.get(spec.get("codec", "hevc"), [])
    available = set(node.get("encoders", []))
    return any(enc in available for enc in wanted)


def active_job_count(node_id):
    with db.connect() as conn:
        return conn.execute(
            """SELECT COUNT(*) FROM jobs
               WHERE node_id=? AND state IN ('leased','running')""",
            (node_id,),
        ).fetchone()[0]


def requeue_expired():
    """Any lease that outlived its node goes back in the pool.

    A job that keeps expiring without ever checking in once (see
    BOUNCE_LIMIT above) is given up on here instead — putting it back in
    the pool again would just repeat the same hang.
    """
    now = time.time()
    with db.connect() as conn:
        expired = conn.execute(
            """SELECT id, path, bounces FROM jobs
               WHERE state IN ('leased','running') AND lease_expires < ?""",
            (now,),
        ).fetchall()
        given_up = 0
        for job in expired:
            if (job["bounces"] or 0) + 1 >= BOUNCE_LIMIT:
                conn.execute(
                    """UPDATE jobs SET state='failed', node_id=NULL,
                           lease_expires=NULL, finished_at=?, error=?
                       WHERE id=?""",
                    (now,
                     f"Lease expired {BOUNCE_LIMIT} times in a row with no "
                     "progress reported in between — the encoder is likely "
                     "hanging on this file. Given up on automatically.",
                     job["id"]),
                )
                given_up += 1
            else:
                conn.execute(
                    """UPDATE jobs
                       SET state='queued', node_id=NULL, lease_expires=NULL,
                           progress=0, fps=0, speed=0, phase=NULL,
                           bounces=bounces+1
                       WHERE id=?""",
                    (job["id"],),
                )
        return len(expired)


def lease_job(node_id):
    """Hand this node the best job it can actually do, or None."""
    node = db.get_node(node_id)
    if not node or not node["enabled"]:
        return None
    slots = node.get("slots")
    slots = int(slots) if slots is not None else int(node.get("max_jobs") or 1)
    if slots < 1 or active_job_count(node_id) >= slots:
        return None

    # Three tiers, strictly in this order:
    #
    #   conversions  what was actually asked for
    #   levelling    housekeeping, but it does rewrite the audio
    #   measuring    housekeeping, and it only reads — always last
    #
    # Read one tier at a time rather than taking a slice of the whole
    # queue and sorting it out here. A single window is ordered
    # oldest-first, so a measuring backlog deeper than the window filled
    # it completely and the conversions behind it were never even
    # considered — the queue showed them waiting while every node kept
    # picking up measurements. Moving one to the top by hand worked
    # because that dragged it into the window; nothing else did.
    tiers = [db.next_queued(kind, limit=TIER_LIMIT)
             for kind in ("convert", "level", "measure")]
    real_work = tiers[0]
    housekeeping = tiers[1] + tiers[2]

    def is_housekeeping(job):
        return db.job_kind(job.get("spec")) != "convert"

    # What this node has been set up to do at all.
    role = node.get("role") or "both"
    if role == "transcode":
        housekeeping = []          # never does loudness work
    elif role == "housekeeping":
        real_work = []             # never does conversions

    # A slice of this node's own capacity can be permanently earmarked for
    # housekeeping instead — the "one machine, two encoders" case: a real
    # conversion keeps the discrete GPU busy while a reserved slot keeps
    # loudness work moving on whatever's left over, rather than housekeeping
    # never getting a turn at all behind a backlog that never empties.
    #
    # Kept as its own independent branch rather than folded into the
    # opportunistic-idle check below: a reservation is an unconditional cap
    # ("always keep this many on housekeeping while there's any to do"),
    # not one more condition on top of "only when idle" — those are two
    # different rules and mixing them into one expression made this much
    # harder to convince yourself was correct than it needed to be.
    reserved = int(node.get("housekeeping_slots") or 0)
    active_housekeeping_here = sum(
        1 for j in db.node_active_jobs(node_id) if is_housekeeping(j))
    reserved_room_open = role == "both" and reserved > active_housekeeping_here

    if reserved_room_open and housekeeping:
        # Earmarked, not spare - offer it before anything else, and skip
        # the idle check entirely: not needing to wait for the node to be
        # idle is the whole point of reserving it.
        ordered = housekeeping + real_work
    else:
        # Outside any reserved capacity, the original rule: housekeeping
        # only runs opportunistically, when the node is otherwise idle.
        # A node dedicated to housekeeping is exempt from waiting at all —
        # waiting on conversions happening on other machines would leave
        # it idle for no reason, the opposite of why someone would
        # dedicate it.
        if (housekeeping and role == "both"
                and db.get_settings().get("housekeeping_when_idle", True)):
            busy_elsewhere = real_work or [
                j for j in db.list_jobs(states=["leased", "running"], limit=200)
                if not is_housekeeping(j)]
            if busy_elsewhere:
                housekeeping = []
        ordered = real_work + housekeeping

    for job in ordered:
        spec = job["spec"]
        if not node_can_encode(node, spec):
            continue

        transport, node_path = resolve_path(node, job["path"])
        if transport == "stream":
            # Copying the video means almost no CPU work, so shipping the file
            # across the network would cost far more than the job saves.
            # These wait for a node that has the share mounted.
            if spec.get("codec") == "copy":
                continue
            size = job.get("size_before") or 0
            if size and size < REMOTE_MIN_BYTES:
                continue  # leave the small stuff for a node that has it mounted

        # Claim it. The WHERE guard makes this safe against two nodes
        # asking at the same moment.
        #
        # started_at is only ever set once (COALESCE keeps the first value):
        # a job that keeps bouncing back into the pool and getting re-leased
        # is still the same stuck attempt, not a fresh one, and auto_fail's
        # limit/stall thresholds need the real elapsed time since it first
        # started to ever have a chance of catching it.
        with db.connect() as conn:
            claimed = conn.execute(
                """UPDATE jobs
                   SET state='leased', node_id=?, transport=?,
                       lease_expires=?, started_at=COALESCE(started_at, ?)
                   WHERE id=? AND state='queued'""",
                (node_id, transport, time.time() + LEASE_SECONDS,
                 time.time(), job["id"]),
            ).rowcount
        if claimed:
            return {
                "id": job["id"],
                "spec": spec,
                "transport": transport,
                "path": node_path,
                "source_path": job["path"],
                "encoders": CODEC_FAMILIES.get(spec.get("codec", "hevc"), []),
            }
    return None


def renew_lease(job_id, reset_bounces=True):
    # A check-in that proves the job is still doing work (not just that it
    # started) clears any bounces run up before this — those were a
    # different, now-resolved spell of not checking in, not a sign this
    # attempt is doomed too. reset_bounces=False is for a job's very first
    # check-in after a (re-)lease, which only proves it started; see the
    # long comment in app.py's /progress handler for why that distinction
    # matters.
    fields = {"lease_expires": time.time() + LEASE_SECONDS}
    if reset_bounces:
        fields["bounces"] = 0
    db.update_job(job_id, **fields)


def reverse_path(node, node_path):
    """Translate a worker's local path back into server path space."""
    if not node_path:
        return node_path
    normalised = _slashes(node_path)
    # Same longest-prefix-wins reasoning as resolve_path, mirrored here
    # since this is the reverse direction of the same mapping.
    mounts = sorted(node.get("mounts", []),
                     key=lambda m: -len(_slashes(m.get("local", ""))))
    for mount in mounts:
        server_prefix = _slashes(mount.get("server", ""))
        local_prefix = _slashes(mount.get("local", ""))
        if local_prefix and normalised.startswith(local_prefix + "/"):
            return server_prefix + normalised[len(local_prefix):]
    return node_path
