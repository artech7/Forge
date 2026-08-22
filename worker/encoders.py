"""Encoder detection and the intent -> FFmpeg translation.

This is the part that makes mixed hardware painless: the server sends
"HEVC at quality 22" and each node figures out how to say that in its
own encoder's dialect.
"""
import subprocess
import tempfile

import streams
import time
from pathlib import Path

CANDIDATES = [
    "hevc_nvenc", "h264_nvenc", "av1_nvenc",
    "hevc_qsv", "h264_qsv", "av1_qsv",
    "hevc_amf", "h264_amf",
    "hevc_vaapi", "h264_vaapi",
    "hevc_videotoolbox", "h264_videotoolbox",
    "libx265", "libx264", "libsvtav1",
]

def _family(encoder):
    for key in ("nvenc", "qsv", "amf", "vaapi", "videotoolbox"):
        if encoder.endswith(key):
            return key
    return encoder


# Quality knobs differ per encoder. These map a single 0-51-ish CRF scale
# onto whatever each encoder actually wants, plus its speed preset.
QUALITY_FLAGS = {
    "nvenc":   lambda q: ["-rc", "vbr", "-cq", str(q), "-preset", "p5"],
    "qsv":     lambda q: ["-global_quality", str(q), "-preset", "medium"],
    "amf":     lambda q: ["-rc", "cqp", "-qp_i", str(q), "-qp_p", str(q),
                          "-quality", "balanced"],
    "vaapi":   lambda q: ["-rc_mode", "CQP", "-qp", str(q)],
    "videotoolbox": lambda q: vt_flags(q),
    "libx265": lambda q: ["-crf", str(q), "-preset", "medium"],
    "libx264": lambda q: ["-crf", str(q), "-preset", "medium"],
    "libsvtav1": lambda q: ["-crf", str(q), "-preset", "6"],
}

# VideoToolbox is inconsistent across macOS and FFmpeg versions: some builds
# reject constant-quality mode for HEVC and demand an explicit bitrate,
# others need software fallback enabled. Rather than assume, each recipe is
# tried at startup and the first that actually encodes is remembered.
def _vt_quality(q):
    """CRF 18-28 -> VideoToolbox's inverted 1-100 scale."""
    return max(1, min(100, round(100 - q * 1.8)))


def _vt_bitrate(q):
    """Rough CRF-to-bitrate stand-in for builds that refuse quality mode."""
    return f"{max(2, round(26 - q * 0.7))}M"


# Order matters enormously. Every hardware-only recipe is tried before any
# recipe that permits software fallback, and the hardware ones pass
# -allow_sw 0 so VideoToolbox fails loudly instead of quietly dropping to a
# software encoder that runs at a fraction of the speed. A passing test that
# was secretly software is worse than a clean failure.
VT_RECIPES = [
    ("constant quality",
     lambda q: ["-allow_sw", "0", "-realtime", "0", "-q:v", str(_vt_quality(q))]),
    ("explicit bitrate",
     lambda q: ["-allow_sw", "0", "-realtime", "0", "-b:v", _vt_bitrate(q)]),
    ("main profile, bitrate",
     lambda q: ["-allow_sw", "0", "-profile:v", "main", "-b:v", _vt_bitrate(q)]),
    ("constant quality, no realtime flag",
     lambda q: ["-allow_sw", "0", "-q:v", str(_vt_quality(q))]),
    ("bitrate, no realtime flag",
     lambda q: ["-allow_sw", "0", "-b:v", _vt_bitrate(q)]),
    # Everything below runs in software. Slow, and only used if nothing above
    # works — the name carries the warning through to the UI.
    ("SOFTWARE fallback, quality",
     lambda q: ["-allow_sw", "1", "-q:v", str(_vt_quality(q))]),
    ("SOFTWARE fallback, bitrate",
     lambda q: ["-allow_sw", "1", "-b:v", _vt_bitrate(q)]),
]


def is_software_recipe(name):
    return "SOFTWARE" in (name or "")


# Filled in by detect(): encoder id -> (recipe name, flag builder)
WORKING_RECIPE = {}
BENCHMARKS = {}          # encoder id -> fps measured at 1080p30, 8-bit
BENCHMARKS_10BIT = {}    # same, but encoding 10-bit output
LAST_DEPTH_NOTE = []     # explanation from the most recent build_command


def vt_flags(q):
    """Flags for whichever VideoToolbox recipe was found to work."""
    for enc, (_name, builder) in WORKING_RECIPE.items():
        if enc.endswith("videotoolbox"):
            return builder(q)
    return ["-q:v", str(_vt_quality(q))]


def recipes_for(enc):
    if enc.endswith("videotoolbox"):
        return VT_RECIPES
    family = _family(enc)
    builder = QUALITY_FLAGS.get(family)
    return [("default", builder)] if builder else [("default", lambda q: [])]


_BENCH_CLIP = {}


def bench_clip(ten_bit=False):
    """A real 1080p file to benchmark against, made once per run.

    Generating frames with lavfi costs real CPU, and on a fast encoder that
    generation becomes the bottleneck — which made software x264 look faster
    than a hardware encoder. Decoding a prepared file is far cheaper and,
    more importantly, costs the same for every encoder being compared.
    """
    global _BENCH_CLIP
    key = "10" if ten_bit else "8"
    if _BENCH_CLIP.get(key) and Path(_BENCH_CLIP[key]).exists():
        return _BENCH_CLIP[key]
    path = Path(tempfile.gettempdir()) / f"forge-bench-1080p-{key}bit.mkv"
    if not path.exists():
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
             "-f", "lavfi", "-i", "testsrc2=size=1920x1080:rate=30:duration=8",
             "-c:v", "libx265" if ten_bit else "libx264",
             "-preset", "ultrafast", "-crf", "18",
             "-pix_fmt", "yuv420p10le" if ten_bit else "yuv420p", str(path)],
            capture_output=True, timeout=300)
    _BENCH_CLIP[key] = str(path)
    return _BENCH_CLIP[key]


def benchmark(enc, builder, frames=240, ten_bit=False):
    """Frames per second encoding a real 1080p clip.

    Run separately for 8-bit and 10-bit because hardware encoders often
    accept 10-bit and then quietly encode it in software, which is roughly
    thirty times slower. A single 8-bit figure hides that completely.
    """
    src = bench_clip(ten_bit)
    if not Path(src).exists():
        return 0.0
    pix_fmt = "p010le" if (ten_bit and _family(enc) in
                           ("videotoolbox", "nvenc", "qsv")) else (
        "yuv420p10le" if ten_bit else "yuv420p")
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", src,
           "-pix_fmt", pix_fmt]
    if enc.endswith("vaapi"):
        cmd += ["-vaapi_device", "/dev/dri/renderD128",
                "-vf", "format=nv12,hwupload"]
    cmd += ["-c:v", enc] + list(builder(22) or []) + \
           ["-frames:v", str(frames), "-f", "null", "-"]
    start = time.time()
    try:
        result = subprocess.run(cmd, capture_output=True, timeout=300)
    except (OSError, subprocess.TimeoutExpired):
        return 0.0
    elapsed = time.time() - start
    return round(frames / elapsed, 1) if result.returncode == 0 and elapsed else 0.0


def probe_encoder(enc, width=1280, height=720):
    """Find a parameter set this encoder actually accepts.

    Returns (recipe_name, builder) or (None, last_error). Testing with real
    encode settings matters: an encoder can accept being listed, accept a
    session, and still reject the specific rate-control mode you plan to use.
    """
    last_error = "no recipe worked"
    for name, builder in recipes_for(enc):
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "lavfi",
               "-i", f"testsrc=size={width}x{height}:rate=10:duration=1",
               "-pix_fmt", "yuv420p"]
        if enc.endswith("vaapi"):
            cmd += ["-vaapi_device", "/dev/dri/renderD128",
                    "-vf", "format=nv12,hwupload"]
        cmd += ["-c:v", enc]
        try:
            cmd += list(builder(22) or [])
        except Exception:
            continue
        cmd += ["-frames:v", "10", "-f", "null", "-"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    encoding="utf-8", errors="replace", timeout=90)
        except subprocess.TimeoutExpired:
            last_error = "timed out"
            continue
        except OSError as exc:
            return None, str(exc)
        if result.returncode == 0:
            return name, builder
        lines = (result.stderr or "").strip().splitlines()
        last_error = lines[-1] if lines else f"exit {result.returncode}"
    return None, last_error


def detect(verify=True, explain=False):
    """List encoders this machine can really use.

    FFmpeg advertises nvenc and videotoolbox whether or not the hardware
    is usable, so each candidate gets a real test encode at 720p. Small
    test frames are not a fair test — several hardware encoders reject
    them — which is why this uses a realistic size.
    """
    try:
        listed = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ([], {"ffmpeg": "not found"}) if explain else []

    available = [e for e in CANDIDATES if e in listed]
    if not verify:
        return (available, {}) if explain else available

    working, rejected = [], {}
    WORKING_RECIPE.clear()
    for enc in available:
        name, result = probe_encoder(enc)
        if name:
            working.append(enc)
            WORKING_RECIPE[enc] = (name, result)
            BENCHMARKS[enc] = benchmark(enc, result)
            # Only worth measuring where hardware might silently give up.
            if _family(enc) != enc:
                BENCHMARKS_10BIT[enc] = benchmark(enc, result, ten_bit=True)
        else:
            rejected[enc] = result
    return (working, rejected) if explain else working


def recipe_note():
    """Which encoders needed non-default settings, and how fast they ran."""
    out = {}
    for enc, (name, _b) in WORKING_RECIPE.items():
        if name == "default":
            continue
        fps = BENCHMARKS.get(enc)
        out[enc] = f"{name}" + (f" — {fps} fps at 1080p" if fps else "")
    return out


def ten_bit_warnings(ratio=0.4):
    """Encoders that are dramatically slower producing 10-bit output.

    Only reports encoders that CAN do 10-bit and do it badly. One that
    can't do it at all isn't a problem to warn about — it simply won't be
    chosen for 10-bit work, and saying otherwise is noise.
    """
    slow = {}
    for enc, eight in BENCHMARKS.items():
        ten = BENCHMARKS_10BIT.get(enc)
        if eight and ten and ten < eight * ratio:
            slow[enc] = {"eight_bit": eight, "ten_bit": ten}
    return slow


# Below this fraction of its 8-bit speed, a 10-bit encode is assumed to have
# fallen back to software rather than being merely slower.
TEN_BIT_CLIFF = 0.4


def can_do_ten_bit(enc):
    """False when the 10-bit test produced nothing at all.

    Zero doesn't mean slow, it means the encode failed. H.264 hardware
    encoders are the common case: no consumer chip does 10-bit H.264, so
    the test correctly comes back with nothing.
    """
    if enc not in BENCHMARKS_10BIT:
        return True                     # never measured; assume it can
    return bool(BENCHMARKS_10BIT.get(enc))


def effective_speed(enc, ten_bit):
    """Measured throughput for the kind of output actually being produced."""
    if not ten_bit:
        return BENCHMARKS.get(enc) or 0
    if not can_do_ten_bit(enc):
        return 0                        # can't do the job at all
    return BENCHMARKS_10BIT.get(enc) or BENCHMARKS.get(enc) or 0


def falls_off_a_cliff(enc):
    """Can produce 10-bit, but so slowly it must be running in software."""
    eight = BENCHMARKS.get(enc)
    ten = BENCHMARKS_10BIT.get(enc)
    return bool(eight and ten and ten < eight * TEN_BIT_CLIFF)


def choose_depth(encoder, spec, source_depth):
    """Decide the output bit depth for this file on this machine.

    "Match the original" means match it unless doing so would be dramatically
    slower here — a 10-bit file that drops the hardware encoder can take
    thirty times as long for a difference nobody watching would notice. An
    explicit 8-bit or 10-bit choice is always respected.
    """
    want = str(spec.get("bit_depth", "match"))
    if want in ("8", "10"):
        return want, None
    if (source_depth or 8) <= 8:
        return "8", None
    if encoder and not can_do_ten_bit(encoder):
        return "8", (f"source is 10-bit, but {encoder} can't produce 10-bit "
                     f"at all — using 8-bit")
    if encoder and falls_off_a_cliff(encoder):
        return "8", (f"source is 10-bit, but {encoder} manages only "
                     f"{BENCHMARKS_10BIT.get(encoder)}fps there against "
                     f"{BENCHMARKS.get(encoder)}fps at 8-bit — using 8-bit")
    return "10", None


def pick(spec_encoders, my_encoders, source_depth=8, spec=None):
    """Fastest encoder this node actually has for the requested codec.

    Measured throughput beats a hardcoded preference order. Hardware is
    usually faster, but not always: a VideoToolbox encoder that fell back to
    software can be slower than libx265, and picking it purely because it
    looks like hardware would be a real loss.
    """
    options = [e for e in spec_encoders if e in my_encoders]
    if not options:
        return None

    # Score each candidate on the depth it would actually produce for this
    # file. An encoder that collapses at 10-bit shouldn't be chosen for a
    # 10-bit job just because its 8-bit figure is high.
    spec = spec or {}
    scored = {}
    for enc in options:
        depth, _note = choose_depth(enc, spec, source_depth)
        speed = effective_speed(enc, depth == "10")
        if speed:
            scored[enc] = speed
    if not scored:
        return options[0]

    best = max(scored, key=lambda e: scored[e])
    # Benchmarks aren't precise enough to justify choosing software over
    # hardware on a small margin. Hardware also leaves the CPU free for
    # other jobs and draws far less power, so it wins any near-tie.
    hardware = [e for e in scored if _family(e) != e]
    if hardware:
        fastest_hw = max(hardware, key=lambda e: scored[e])
        if scored[fastest_hw] >= scored[best] * 0.75:
            return fastest_hw
    return best


def ranking():
    """Encoders sorted by measured speed, for reporting."""
    return sorted(BENCHMARKS.items(), key=lambda kv: -kv[1])


def build_command(src, dst, encoder, spec, info=None):
    """Turn an intent spec into an FFmpeg invocation for this encoder.

    When stream info is supplied, every stream is mapped explicitly and in
    final order. Without it this falls back to a simple map, which still
    works but can't reorder tracks or spot embedded cover art.
    """
    quality = int(spec.get("quality", 22))
    container = spec.get("container", "mkv")
    video_copy = spec.get("codec") == "copy" or encoder is None

    video_stream = None
    if info:
        video_stream = next(
            (st for st in info.get("streams", [])
             if st.get("codec_type") == "video" and not streams.is_image(st)),
            None)

    # ---- HDR handling ------------------------------------------------
    source_is_hdr = streams.is_hdr(video_stream)
    want_sdr = spec.get("hdr_mode") == "sdr" and source_is_hdr
    tonemapping = want_sdr and not video_copy
    if want_sdr and video_copy:
        tonemapping = False        # can't tonemap without re-encoding

    source_depth = streams.source_bit_depth(video_stream)
    depth_pref, depth_note = choose_depth(encoder, spec, source_depth)
    if want_sdr:
        depth_pref, depth_note = "8", None   # SDR at 10-bit gains nothing
    pix_fmt, _depth = streams.pixel_format(encoder, depth_pref, source_depth)
    if depth_note:
        LAST_DEPTH_NOTE.append(depth_note)

    cmd = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]

    # Hardware decoding. Frames come back to system memory rather than
    # staying on the GPU, which costs a copy but keeps filters and the
    # stream mapping working. Skipped when tonemapping, which needs software
    # frames anyway, and skipped for 10-bit H.264 (Hi10P), which no consumer
    # hardware decoder supports — asking for it just fails and falls back.
    hi10p = bool(video_stream
                 and video_stream.get("codec_name") == "h264"
                 and streams.source_bit_depth(video_stream) > 8)
    if encoder and not tonemapping and not hi10p:
        if encoder.endswith("videotoolbox"):
            cmd += ["-hwaccel", "videotoolbox"]
        elif encoder.endswith("nvenc"):
            cmd += ["-hwaccel", "cuda"]
        elif encoder.endswith("qsv"):
            cmd += ["-hwaccel", "qsv"]
    if encoder and encoder.endswith("vaapi") and not tonemapping:
        cmd += ["-vaapi_device", "/dev/dri/renderD128"]

    cmd += ["-i", src]

    # ---- stream selection --------------------------------------------
    if info:
        map_args, disposition_args, _notes = streams.plan_streams(info, spec)
        cmd += map_args
        # Titles are written against output positions, so they have to be
        # built from the same ordered lists the mapping used.
        kept_audio, kept_subs = streams.kept_tracks(info, spec)
        disposition_args += streams.naming_args(kept_audio, kept_subs, spec)
    else:
        cmd += ["-map", "0:v:0", "-map", "0:a?"]
        if spec.get("subtitle_mode") != "strip":
            cmd += ["-map", "0:s?"]
        disposition_args = []

    # ---- video --------------------------------------------------------
    if video_copy:
        cmd += ["-c:v", "copy"]
    else:
        filters = []
        if tonemapping:
            filters.append(streams.tonemap_filter())
        if encoder.endswith("vaapi"):
            filters.append("format=nv12,hwupload")
        if filters:
            cmd += ["-vf", ",".join(filters)]
        elif pix_fmt:
            cmd += ["-pix_fmt", pix_fmt]

        cmd += ["-c:v", encoder]
        cmd += QUALITY_FLAGS[_family(encoder)](quality)
        cmd += streams.colour_args(spec, video_stream, tonemapping)

    # ---- audio ---------------------------------------------------------
    audio = spec.get("audio", "copy")
    audio_filters = []
    downmix = spec.get("downmix")
    if downmix in ("stereo", "2"):
        cmd += ["-ac", "2"]
        # Without this, dialogue in a 5.1 mix gets buried when folded down.
        audio_filters.append("pan=stereo|FL=0.5*FC+0.707*FL+0.707*BL"
                             "|FR=0.5*FC+0.707*FR+0.707*BR")
    if spec.get("normalise_loudness"):
        audio_filters.append("loudnorm=I=-16:TP=-1.5:LRA=11")

    if audio == "copy" and not audio_filters:
        cmd += ["-c:a", "copy"]
    elif audio == "flac":
        cmd += ["-c:a", "flac"]          # lossless, no bitrate to set
    else:
        codec = audio if audio != "copy" else "aac"
        cmd += ["-c:a", codec]
        # An empty bitrate would become -b:a "" and FFmpeg refuses to open
        # the output at all, so a blank value falls back rather than passing
        # through. Jobs queued before this was fixed still carry one.
        bitrate = (spec.get("audio_bitrate") or "").strip()
        cmd += ["-b:a", bitrate or "160k"]
    if audio_filters:
        cmd += ["-af", ",".join(audio_filters)]

    # ---- subtitles ------------------------------------------------------
    if spec.get("subtitle_mode") == "strip" and not spec.get("keep_forced_subs"):
        cmd += ["-sn"]
    else:
        cmd += ["-c:s", "mov_text" if container == "mp4" else "copy"]

    cmd += disposition_args

    # Chapters are cheap to keep and painful to lose — they drive the
    # skip-intro behaviour in most players.
    cmd += ["-map_chapters", "0" if spec.get("keep_chapters", True) else "-1"]

    if spec.get("clean_metadata"):
        # Release groups stuff their name into the title field, which then
        # shows up instead of the film's name in some players.
        cmd += ["-map_metadata", "-1", "-metadata",
                f"title={spec.get('metadata_title', '')}"]
    else:
        cmd += ["-map_metadata", "0"]
        # mkvmerge stamps every stream with write-time statistics
        # (_STATISTICS_TAGS, _STATISTICS_WRITING_APP,
        # _STATISTICS_WRITING_DATE_UTC) describing the ORIGINAL encode -
        # stale the moment this file is touched, and FFmpeg has been seen
        # rejecting the whole command outright on some files while
        # carrying them through, taking a perfectly fine remux down with
        # it. There's no "every stream" specifier, so this clears them
        # per stream type instead.
        for stat_tag in ("_STATISTICS_TAGS", "_STATISTICS_WRITING_APP",
                         "_STATISTICS_WRITING_DATE_UTC"):
            for stream_type in ("v", "a", "s"):
                cmd += [f"-metadata:s:{stream_type}", f"{stat_tag}="]
    cmd += ["-max_muxing_queue_size", "1024"]
    if container == "mp4":
        cmd += ["-movflags", "+faststart"]
        if not video_copy:
            cmd += ["-tag:v", "hvc1" if spec.get("codec") == "hevc" else "avc1"]
    cmd += ["-progress", "pipe:1", "-nostats", dst]
    return cmd
