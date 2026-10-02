"""Checking a finished file before the server is told it's done.

Only for work that changes a codec level label. A relabel promises three
things: the label now says what it should, the file is as long as it
was, and the picture is exactly the picture it was. All three are cheap
to prove, so all three are proved — a failure here fails the job, and
the server never touches the original.

A re-encode makes a different promise (the label it was given is one the
new stream actually keeps), so it gets a different check.
"""
import re
import subprocess
import threading

# How far a copied file's length may drift from the source's. Nothing in
# a relabel should change it at all, but re-encoding the audio in the
# same pass (an audio conversion plus a relabel) moves the end of the
# file by the encoder's padding — a few tens of milliseconds.
DURATION_TOLERANCE_SECONDS = 0.5
DURATION_TOLERANCE_FRACTION = 0.001

# The parameter-set NAL units a level rewrite edits. The level lives in
# them, so these are the only packets' bytes allowed to differ: in a
# stream that repeats them before every keyframe, those keyframes come
# out with a different hash for the right reason. Everything else in the
# picture must match byte for byte.
PARAMETER_SETS = {"hevc": "32-34",        # VPS, SPS, PPS
                  "h264": "7-8|13|15"}    # SPS, PPS, SPS extension, subset SPS

# Reading every packet of two large files takes a while; this is generous
# rather than tight, because giving up early fails a perfectly good job.
PACKET_TIMEOUT = 7200


def _run(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)


def real_video(info):
    """The picture stream in a probe — never cover art."""
    for stream in (info or {}).get("streams", []):
        if stream.get("codec_type") != "video":
            continue
        if (stream.get("disposition") or {}).get("attached_pic"):
            continue
        if stream.get("codec_name") in ("mjpeg", "png", "bmp", "gif", "tiff",
                                         "webp", "jpeg"):
            continue
        return stream
    return None


def header_level(path, index, codec):
    """(level_idc, high_tier) from the container's own copy of the header.

    MKV keeps it as CodecPrivate and MP4 as the hvcC/avcC box; FFmpeg
    reports either as the stream's extradata. This is what a player
    reads before it decodes a single frame, so it's checked directly
    rather than trusted to agree with the stream.

    HEVC (hvcC): byte 1 carries the tier flag (0x20), byte 12 the level.
    H.264 (avcC): byte 3 is the level. Returns (None, None) when there is
    no such header, which in MKV or MP4 means something is wrong.
    """
    out = _run(["ffprobe", "-v", "error", "-select_streams", str(index),
                "-show_data", "-show_entries", "stream=extradata",
                "-of", "default=nw=1", path])
    data = bytearray()
    for line in (out.stdout or "").splitlines():
        match = re.match(r"\s*[0-9a-f]{8}:\s+((?:[0-9a-f]{4} ?)+)", line)
        if match:
            data += bytes.fromhex(match.group(1).replace(" ", ""))
    if not data or data[0] != 1:
        return None, None
    if codec == "hevc" and len(data) > 12:
        return data[12], bool(data[1] & 0x20)
    if codec == "h264" and len(data) > 3:
        return data[3], False
    return None, None


def _ffprobe_scale(codec):
    return {"hevc": 30, "h264": 10}[codec]


def check_label(path, info, codec, level):
    """None if the file is labelled `level` everywhere, else what's wrong."""
    video = real_video(info)
    if not video:
        return "the finished file has no video stream"
    if video.get("codec_name") != codec:
        return f"the finished file's video is {video.get('codec_name')}, not {codec}"
    want = int(round(float(level) * _ffprobe_scale(codec)))
    if int(video.get("level") or -1) != want:
        return (f"FFmpeg reads the finished file as level "
                f"{_as_level(video.get('level'), codec)}, not {level}")
    in_header, _tier = header_level(path, video["index"], codec)
    if in_header != want:
        return (f"the container's header still says level "
                f"{_as_level(in_header, codec)}, not {level} — players "
                "read that before anything else")
    return None


def _as_level(value, codec):
    try:
        value = int(value)
    except (TypeError, ValueError):
        return "nothing"
    return f"{value / _ffprobe_scale(codec):.1f}" if value > 0 else "nothing"


def packet_fingerprint(path, index, codec, results, key):
    """Every video packet's size and hash, parameter sets taken out.

    Runs FFmpeg's framemd5 over a stream copy, so it reads but decodes
    nothing. Stored in results[key] as a list, or an error string.
    """
    cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path,
           "-map", f"0:{index}", "-c", "copy",
           "-bsf:v", f"filter_units=remove_types={PARAMETER_SETS[codec]}",
           "-f", "framemd5", "-"]
    try:
        out = _run(cmd, timeout=PACKET_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired) as exc:
        results[key] = f"couldn't read its packets ({exc})"
        return
    if out.returncode != 0:
        lines = [l for l in (out.stderr or "").splitlines() if l.strip()]
        results[key] = f"couldn't read its packets ({lines[-1] if lines else out.returncode})"
        return
    packets = []
    for line in out.stdout.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        # stream, dts, pts, duration, size, hash. Timestamps are left out:
        # they're written in each container's own timebase, so a relabel
        # that also changes the container would differ there for no
        # reason that matters. What the picture is made of can't.
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 6:
            packets.append((parts[4], parts[5]))
    results[key] = packets


def same_packets(src, src_index, out, out_index, codec):
    """None if the picture came through untouched, else what differs.

    Both files are read at the same time — on a network share most of
    the cost is waiting on reads, and the two don't wait on each other.
    """
    results = {}
    threads = [threading.Thread(target=packet_fingerprint,
                                args=(src, src_index, codec, results, "src")),
               threading.Thread(target=packet_fingerprint,
                                args=(out, out_index, codec, results, "out"))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    before, after = results.get("src"), results.get("out")
    for name, got in (("the original", before), ("the finished file", after)):
        if not isinstance(got, list):
            return f"{name}: {got or 'nothing came back'}"
    if len(before) != len(after):
        return (f"the finished file has {len(after)} video packets where the "
                f"original had {len(before)}")
    for n, (a, b) in enumerate(zip(before, after)):
        if a != b:
            return (f"video packet {n + 1} of {len(before)} differs from the "
                    "original — the picture was changed, not just its label")
    if not before:
        return "no video packets were read from either file"
    return None


def _duration(info):
    try:
        return float(((info or {}).get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


def same_length(src_info, out_info):
    before, after = _duration(src_info), _duration(out_info)
    if not before or not after:
        return "couldn't read how long one of the files is"
    allowed = max(DURATION_TOLERANCE_SECONDS, before * DURATION_TOLERANCE_FRACTION)
    if abs(before - after) > allowed:
        return (f"the finished file is {after:.2f}s long where the original "
                f"was {before:.2f}s")
    return None


def verify_relabel(src, out, spec, src_info, out_info):
    """Prove a relabelled file is the original with a new label.

    Returns {"ok": bool, "checks": [...], "error": str|None}. "checks"
    lists what was confirmed, in words the queue can show.
    """
    codec, level = spec.get("relabel_codec"), spec.get("relabel_level")
    report = {"ok": False, "checks": [], "error": None, "level": level}
    problem = check_label(out, out_info, codec, level)
    if problem:
        report["error"] = problem
        return report
    report["checks"].append(f"labelled {level}")

    problem = same_length(src_info, out_info)
    if problem:
        report["error"] = problem
        return report
    report["checks"].append("same length")

    src_video, out_video = real_video(src_info), real_video(out_info)
    if not src_video:
        report["error"] = "the original has no video stream to compare with"
        return report
    problem = same_packets(src, src_video["index"], out, out_video["index"], codec)
    if problem:
        report["error"] = problem
        return report
    report["checks"].append("every video packet unchanged")
    report["ok"] = True
    return report


def video_bitrate(path, index, duration):
    """Bits per second the video stream actually used, from its packets.

    Summed from the packets rather than taken from the container, which
    in MKV usually doesn't record a per-stream rate at all.
    """
    try:
        out = _run(["ffprobe", "-v", "error", "-select_streams", str(index),
                    "-show_entries", "packet=size", "-of", "csv=p=0", path],
                   timeout=PACKET_TIMEOUT)
    except (OSError, subprocess.TimeoutExpired):
        return None
    total = 0
    for line in (out.stdout or "").splitlines():
        try:
            total += int(line.strip().rstrip(","))
        except ValueError:
            continue
    return total * 8 / duration if total and duration else None


def verify_encode(out, spec, out_info, enforced, encoder=None):
    """Check a re-encode wears the level it was given, and keeps to it.

    The label must be what was asked for, at Main tier (the tier device
    limits are written for). Then, for an encoder that couldn't be held
    to the level's bitrate while encoding, the result is measured: its
    average, with the same margin the server allows for peaks, has to fit
    under the level's ceiling. One that was held to it already does.
    """
    target = spec.get("encode_level") or {}
    codec, level = spec.get("codec"), target.get("level")
    report = {"ok": False, "checks": [], "error": None, "level": level}
    problem = check_label(out, out_info, codec, level)
    if problem:
        report["error"] = problem
        return report
    report["checks"].append(f"labelled {level}")

    video = real_video(out_info)
    if codec == "hevc":
        _level, high_tier = header_level(out, video["index"], codec)
        if high_tier:
            report["error"] = (f"{encoder or 'the encoder'} wrote High tier; "
                               "device level limits mean Main tier")
            return report
        report["checks"].append("Main tier")

    if enforced:
        report["checks"].append("bitrate held to the level while encoding")
    elif target.get("max_kbps"):
        bps = video_bitrate(out, video["index"], _duration(out_info))
        margin = float(target.get("margin") or 2.5)
        if bps is None:
            report["error"] = "couldn't measure the finished video's bitrate"
            return report
        needed = bps / 1000 * margin
        if needed > target["max_kbps"]:
            report["error"] = (
                f"{encoder or 'the encoder'} produced {bps / 1e6:.1f} Mbps, "
                f"too much for level {level} with room for peaks "
                f"({target['max_kbps'] / 1000 / margin:.1f} Mbps at most). "
                "It can't be held to a level while encoding, so the label "
                "would be a promise the file doesn't keep. A lower quality "
                "setting, or a worker with NVENC or x265, avoids this")
            return report
        report["checks"].append(f"{bps / 1e6:.1f} Mbps fits level {level}")
    report["ok"] = True
    return report
