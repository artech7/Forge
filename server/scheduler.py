"""Job matching. Rules describe intent; nodes advertise encoders.

The scheduler's only real job is answering: which of these idle nodes can
satisfy this spec, and should it read the file directly or have it streamed?
"""
import time
import db

LEASE_SECONDS = 120

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
    for mount in node.get("mounts", []):
        server_prefix = _slashes(mount.get("server", ""))
        local_prefix = _slashes(mount.get("local", ""))
        if server_prefix and normalised.startswith(server_prefix + "/"):
            return "local", local_prefix + normalised[len(server_prefix):]
    return "stream", path


def node_can_encode(node, spec):
    # Copying the video stream needs no encoder at all — any node will do.
    if spec.get("codec") == "copy":
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
    """Any lease that outlived its node goes back in the pool."""
    now = time.time()
    with db.connect() as conn:
        cur = conn.execute(
            """UPDATE jobs
               SET state='queued', node_id=NULL, lease_expires=NULL,
                   progress=0, fps=0, speed=0
               WHERE state IN ('leased','running') AND lease_expires < ?""",
            (now,),
        )
        return cur.rowcount


def lease_job(node_id):
    """Hand this node the best job it can actually do, or None."""
    node = db.get_node(node_id)
    if not node or not node["enabled"]:
        return None
    slots = node.get("slots")
    slots = int(slots) if slots is not None else int(node.get("max_jobs") or 1)
    if slots < 1 or active_job_count(node_id) >= slots:
        return None

    for job in db.list_jobs(states=["queued"], limit=100):
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
        with db.connect() as conn:
            claimed = conn.execute(
                """UPDATE jobs
                   SET state='leased', node_id=?, transport=?,
                       lease_expires=?, started_at=?
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


def renew_lease(job_id):
    db.update_job(job_id, lease_expires=time.time() + LEASE_SECONDS)


def reverse_path(node, node_path):
    """Translate a worker's local path back into server path space."""
    if not node_path:
        return node_path
    normalised = _slashes(node_path)
    for mount in node.get("mounts", []):
        server_prefix = _slashes(mount.get("server", ""))
        local_prefix = _slashes(mount.get("local", ""))
        if local_prefix and normalised.startswith(local_prefix + "/"):
            return server_prefix + normalised[len(local_prefix):]
    return node_path
