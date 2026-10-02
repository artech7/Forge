"""Codec levels: what a stream actually needs, against what it says it needs.

A level is the label on a video stream that tells a player how much
decoding power it must have. Players refuse anything labelled above what
they support, and so does Jellyfin on their behalf — it transcodes rather
than send a file it thinks the device can't play. Plenty of encodes carry
a label far higher than their content needs: 1080p at 2 Mbps marked as
if it were 4K at 60 Mbps. The 1st-gen Fire TV Stick 4K stops at HEVC 5.1,
so a file marked 5.2 gets transcoded even though the stick would have
played the picture without trouble.

Everything here is a pure function of numbers ffprobe already reports,
so the scanner, the queue and the Library Health list all reach the same
answer from the same inputs.

"Level" in this module always means the codec level. Elsewhere in Forge
"levelling" means evening out audio loudness, which is unrelated.
"""

# Peak bitrate isn't something ffprobe can report without reading the
# whole file, and a level's bitrate ceiling applies to the peaks, not the
# average. So the average has to fit under the ceiling with room to
# spare. 2.5x is a cautious guess, not a measured figure: if a relabelled
# file ever stutters on a device, raising this is the knob to turn.
BITRATE_MARGIN = 2.5

# HEVC Main tier, from Table A.8 of the H.265 spec:
#   (level, MaxLumaPs samples, MaxLumaSr samples/s, MaxBR kbps, MaxCPB kbits)
# Main tier only, deliberately. High tier allows more bitrate at the same
# level, but a player that stops at "5.1" generally means Main tier, so
# judging every stream by the Main tier limits is the safe reading.
HEVC_LEVELS = [
    ("3.0", 552960, 16588800, 6000, 6000),
    ("3.1", 983040, 33177600, 10000, 10000),
    ("4.0", 2228224, 66846720, 12000, 12000),
    ("4.1", 2228224, 133693440, 20000, 20000),
    ("5.0", 8912896, 267386880, 25000, 25000),
    ("5.1", 8912896, 534773760, 40000, 40000),
    ("5.2", 8912896, 1069547520, 60000, 60000),
    ("6.0", 35651584, 1069547520, 60000, 60000),
    ("6.1", 35651584, 2139095040, 120000, 120000),
    ("6.2", 35651584, 4278190080, 240000, 240000),
]

# H.264, from Table A-1 of the H.264 spec:
#   (level, MaxFS macroblocks, MaxMBPS macroblocks/s, MaxBR, MaxCPB)
# MaxBR and MaxCPB are in units of 1000 bits for Baseline/Main/Extended;
# higher profiles get a multiple of them (H264_PROFILE_FACTOR below).
H264_LEVELS = [
    ("3.0", 1620, 40500, 10000, 10000),
    ("3.1", 3600, 108000, 14000, 14000),
    ("3.2", 5120, 216000, 20000, 20000),
    ("4.0", 8192, 245760, 20000, 25000),
    ("4.1", 8192, 245760, 50000, 62500),
    ("4.2", 8704, 522240, 50000, 62500),
    ("5.0", 22080, 589824, 135000, 135000),
    ("5.1", 36864, 983040, 240000, 240000),
    ("5.2", 36864, 2073600, 240000, 240000),
    ("6.0", 139264, 4177920, 240000, 240000),
    ("6.1", 139264, 8355840, 480000, 480000),
    ("6.2", 139264, 16711680, 800000, 800000),
]

# cpbBrVclFactor relative to Main (Table A-2): High is 1250/1000 and so
# on. Matched against ffprobe's profile name, longest first so "High 10"
# isn't read as "High".
H264_PROFILE_FACTOR = [
    ("high 4:4:4", 4.0), ("high 4:2:2", 4.0), ("high 10", 3.0),
    ("high", 1.25),
]

# What ffprobe's "level" number means: HEVC stores level x 30
# (general_level_idc), H.264 stores level x 10 (level_idc).
FFPROBE_SCALE = {"hevc": 30, "h264": 10}

# Where a device's limit usually sits, and what the wizard offers. The
# default is what the 1st-gen Fire TV Stick 4K and many TVs top out at.
DEFAULT_MAX = {"hevc": "5.1", "h264": "4.1"}
CHOICES = {
    "hevc": ["4.1", "5.0", "5.1", "5.2", "6.0", "6.1", "6.2"],
    "h264": ["4.0", "4.1", "4.2", "5.0", "5.1", "5.2"],
}


def _table(codec):
    return {"hevc": HEVC_LEVELS, "h264": H264_LEVELS}.get(codec)


def _num(level):
    """'5.1' -> 5.1, for comparing. None stays None."""
    try:
        return float(level)
    except (TypeError, ValueError):
        return None


def name(level):
    """A level as people write it: '4.1', '5.0' -> '5', and so on.

    Kept as one decimal everywhere in Forge so that '5' from one place and
    '5.0' from another are recognisably the same thing.
    """
    value = _num(level)
    return None if value is None else f"{value:.1f}"


def from_ffprobe(codec, value):
    """ffprobe's integer level -> '4.1'. None if it isn't a level at all.

    ffprobe reports -99 (or nothing) when a stream carries no level, which
    is not the same as a low level and must not be treated as one.
    """
    scale = FFPROBE_SCALE.get(codec)
    try:
        value = int(value)
    except (TypeError, ValueError):
        return None
    if not scale or value <= 0:
        return None
    return name(value / scale)


def to_ffprobe(codec, level):
    """'4.1' -> what ffprobe will report for it (123 for HEVC, 41 for H.264)."""
    scale = FFPROBE_SCALE.get(codec)
    value = _num(level)
    if not scale or value is None:
        return None
    return int(round(value * scale))


def minimum_for_shape(width, height):
    """The lowest level Forge will ever label a picture of this size.

    Not a spec limit. A relabel that goes as low as the tables allow is
    within the rules but well outside what encoders and players are used
    to seeing for the size, and the point of this is to make files play,
    not to win on paper. So: at least 4.1 for anything bigger than 720p up
    to 1080p (letterboxed 1920x818 counts as 1080p), and at least 5.0 for
    anything bigger than 1080p.
    """
    width, height = int(width or 0), int(height or 0)
    if width > 1920 or height > 1080:
        return "5.0"
    if width > 1280 or height > 720:
        return "4.1"
    return None


def _h264_factor(profile):
    text = (profile or "").lower()
    for prefix, factor in H264_PROFILE_FACTOR:
        if text.startswith(prefix):
            return factor
    return 1.0


def required_level(codec, width, height, fps, bitrate_bps=0, profile=None,
                   margin=BITRATE_MARGIN):
    """The minimum level that honestly describes this stream.

    Judged on picture size, samples per second (width x height x frame
    rate) and bitrate, with the average bitrate multiplied by `margin`
    first so peaks have somewhere to go. Never lower than
    minimum_for_shape(). Returns a level like '4.1', or None when nothing
    in the table is high enough — which happens, and is the clearest
    possible "don't relabel this".

    A bitrate of 0 or None means "judge the shape only", which is what a
    re-encode wants before it knows what bitrate it will produce.
    """
    table = _table(codec)
    width, height = int(width or 0), int(height or 0)
    if not table or not width or not height:
        return None
    fps = float(fps or 0)
    kbps = float(bitrate_bps or 0) / 1000 * margin

    if codec == "hevc":
        samples = width * height
        rate = samples * fps
        longest = max(width, height)
        chosen = None
        for level, max_ps, max_sr, max_br, _cpb in table:
            # Each dimension is limited too, not only the area, so a
            # very wide picture can't sneak under a small level.
            if (samples <= max_ps and longest <= (8 * max_ps) ** 0.5
                    and rate <= max_sr and kbps <= max_br):
                chosen = level
                break
    else:
        mbs_w, mbs_h = -(-width // 16), -(-height // 16)
        frame_mbs = mbs_w * mbs_h
        rate = frame_mbs * fps
        factor = _h264_factor(profile)
        chosen = None
        for level, max_fs, max_mbps, max_br, _cpb in table:
            side = (8 * max_fs) ** 0.5
            if (frame_mbs <= max_fs and mbs_w <= side and mbs_h <= side
                    and rate <= max_mbps and kbps <= max_br * factor):
                chosen = level
                break

    if chosen is None:
        return None
    floor = minimum_for_shape(width, height)
    if floor and _num(floor) > _num(chosen):
        chosen = floor
    return chosen


def limits(codec, level, profile=None):
    """(MaxBR kbps, MaxCPB kbits) for a level, or (None, None).

    What a re-encode has to stay under for its label to be honest — used
    as -maxrate/-bufsize on the encoders that can enforce it.
    """
    table = _table(codec)
    target = name(level)
    factor = _h264_factor(profile) if codec == "h264" else 1.0
    for row in table or []:
        if row[0] == target:
            return int(row[3] * factor), int(row[4] * factor)
    return None, None


def max_for(spec_or_profile, codec):
    """The library's ceiling for this codec, defaulting to the Fire TV's."""
    wanted = ((spec_or_profile or {}).get("max_video_level") or {}).get(codec)
    return name(wanted) or DEFAULT_MAX.get(codec)


def video_numbers(info):
    """Pull what the calculator needs out of Forge's probe summary.

    Works on a fresh probe and on a cached row alike. A cache row written
    before Forge recorded a bitrate for this purpose falls back on the
    older video_bitrate estimate, which subtracts a fixed allowance per
    audio track instead of what the tracks actually report.
    """
    info = info or {}
    detail = info.get("detail") or {}
    if isinstance(detail, str):
        import json
        try:
            detail = json.loads(detail)
        except ValueError:
            detail = {}
    return {
        "codec": info.get("video_codec"),
        "width": info.get("width"),
        "height": info.get("height"),
        "fps": detail.get("frame_rate"),
        "profile": detail.get("profile"),
        "level_raw": detail.get("level"),
        "bitrate": detail.get("level_bitrate") or info.get("video_bitrate") or 0,
    }


def assess(info, spec_or_profile):
    """Compare a file's level label with what its content needs.

    Returns a dict, or None for a file this doesn't apply to (not HEVC or
    H.264, no level recorded, no picture size):

        have    the level the file is labelled with, e.g. '5.2'
        need    the lowest honest level for it, e.g. '4.1'; None if
                nothing in the tables fits
        max     the library's ceiling for this codec, e.g. '5.1'
        verdict 'ok'        labelled at or under the ceiling — leave it
                'relabel'   labelled over the ceiling, but its content
                            fits under it: relabel to `need`, losslessly
                'too_high'  its content really does need more than the
                            ceiling — relabelling would be a lie that
                            stutters, so it needs a re-encode instead
    """
    numbers = video_numbers(info)
    codec = numbers["codec"]
    if codec not in FFPROBE_SCALE:
        return None
    have = from_ffprobe(codec, numbers["level_raw"])
    if not have or not numbers["width"] or not numbers["height"]:
        return None
    need = required_level(codec, numbers["width"], numbers["height"],
                          numbers["fps"], numbers["bitrate"], numbers["profile"])
    ceiling = max_for(spec_or_profile, codec)
    if _num(have) <= _num(ceiling):
        verdict = "ok"
    elif need is not None and _num(need) <= _num(ceiling):
        verdict = "relabel"
    else:
        verdict = "too_high"
    return {"codec": codec, "have": have, "need": need, "max": ceiling,
            "verdict": verdict}


def encode_target(codec, info, spec_or_profile, profile=None):
    """The level to ask an encoder for when re-encoding to `codec`.

    The source's own bitrate stands in for the output's, since a
    re-encode almost always comes out smaller. The answer is capped at the
    library's ceiling — and when the cap actually bites, the encoder has
    to be held to it (`enforce`), because otherwise the label could be a
    promise the stream doesn't keep.

    The one thing a cap can't fix is the picture itself: no bitrate makes
    4K at 120fps fit a level built for 4K at 60. Then the honest level is
    used uncapped and `too_high` says the file still won't play directly.

    Returns {"level", "enforce", "too_high", "max_kbps", "cpb_kbits",
    "margin"}, or
    None when `codec` has no levels to speak of (AV1) or the source has
    no measurable picture.
    """
    if codec not in FFPROBE_SCALE:
        return None
    numbers = video_numbers(info)
    if not numbers["width"] or not numbers["height"]:
        return None
    ceiling = max_for(spec_or_profile, codec)
    shape = required_level(codec, numbers["width"], numbers["height"],
                           numbers["fps"], 0, profile)
    if shape is None or _num(shape) > _num(ceiling):
        level, enforce, too_high = shape or _table(codec)[-1][0], False, True
    else:
        full = required_level(codec, numbers["width"], numbers["height"],
                              numbers["fps"], numbers["bitrate"], profile)
        if full is not None and _num(full) <= _num(ceiling):
            level, enforce = full, False
        else:
            level, enforce = ceiling, True
        too_high = False
    max_kbps, cpb = limits(codec, level, profile)
    # The margin travels with the target so the worker checks an
    # unenforced encode by the same rule the server judged files by.
    return {"level": level, "enforce": enforce, "too_high": too_high,
            "max_kbps": max_kbps, "cpb_kbits": cpb, "margin": BITRATE_MARGIN}


def describe(result):
    """One short phrase for a queue row or a details popup."""
    if not result:
        return None
    if result["verdict"] == "relabel":
        return f"Level relabel {result['have']} → {result['need']}"
    if result["verdict"] == "too_high":
        return ("Level too high for target devices — needs re-encode "
                f"({result['have']}, devices stop at {result['max']})")
    return None
