"""Stream-level decisions.

Everything Tdarr does with a node graph, expressed as plain settings. The
work is all in reading what's actually in the file and deciding which
streams survive, in what order, and how they're tagged.
"""
import json
import re
import subprocess

# Cover art and thumbnails are stored as video streams. Left in place they
# get picked up by a video map, and encoders choke on them — this is the
# single most common cause of a transcode that fails on one file in fifty.
IMAGE_CODECS = {"mjpeg", "png", "bmp", "gif", "tiff", "webp", "jpeg"}

# Transfer characteristics that mean the file is HDR.
HDR_TRANSFERS = {"smpte2084", "arib-std-b67", "smpte428", "bt2020-10", "bt2020-12"}

# ISO 639 codes to display names. Both the bibliographic and terminological
# forms appear in the wild (ger/deu, fre/fra, chi/zho), as do bare two-letter
# codes, so all three map to the same name.
LANGUAGE_NAMES = {
    "eng": "English", "en": "English",
    "spa": "Spanish", "es": "Spanish",
    "fre": "French", "fra": "French", "fr": "French",
    "ger": "German", "deu": "German", "de": "German",
    "ita": "Italian", "it": "Italian",
    "por": "Portuguese", "pt": "Portuguese",
    "rus": "Russian", "ru": "Russian",
    "jpn": "Japanese", "ja": "Japanese",
    "kor": "Korean", "ko": "Korean",
    "chi": "Chinese", "zho": "Chinese", "zh": "Chinese",
    "cmn": "Mandarin", "yue": "Cantonese",
    "ara": "Arabic", "ar": "Arabic",
    "hin": "Hindi", "hi": "Hindi",
    "ben": "Bengali", "tam": "Tamil", "tel": "Telugu", "mar": "Marathi",
    "urd": "Urdu", "pan": "Punjabi", "guj": "Gujarati", "mal": "Malayalam",
    "kan": "Kannada",
    "dut": "Dutch", "nld": "Dutch", "nl": "Dutch",
    "swe": "Swedish", "sv": "Swedish",
    "nor": "Norwegian", "nob": "Norwegian", "no": "Norwegian",
    "dan": "Danish", "da": "Danish",
    "fin": "Finnish", "fi": "Finnish",
    "ice": "Icelandic", "isl": "Icelandic",
    "pol": "Polish", "pl": "Polish",
    "cze": "Czech", "ces": "Czech", "cs": "Czech",
    "slo": "Slovak", "slk": "Slovak",
    "hun": "Hungarian", "hu": "Hungarian",
    "rum": "Romanian", "ron": "Romanian", "ro": "Romanian",
    "bul": "Bulgarian", "gre": "Greek", "ell": "Greek", "el": "Greek",
    "tur": "Turkish", "tr": "Turkish",
    "heb": "Hebrew", "he": "Hebrew",
    "tha": "Thai", "th": "Thai",
    "vie": "Vietnamese", "vi": "Vietnamese",
    "ind": "Indonesian", "id": "Indonesian",
    "may": "Malay", "msa": "Malay",
    "fil": "Filipino", "tgl": "Tagalog",
    "ukr": "Ukrainian", "uk": "Ukrainian",
    "srp": "Serbian", "hrv": "Croatian", "slv": "Slovenian",
    "est": "Estonian", "lav": "Latvian", "lit": "Lithuanian",
    "cat": "Catalan", "baq": "Basque", "eus": "Basque", "glg": "Galician",
    "per": "Persian", "fas": "Persian", "fa": "Persian",
    "lat": "Latin", "epo": "Esperanto", "wel": "Welsh", "cym": "Welsh",
    "gle": "Irish", "gla": "Scottish Gaelic",
    "afr": "Afrikaans", "swa": "Swahili", "zul": "Zulu", "amh": "Amharic",
    "mya": "Burmese", "khm": "Khmer", "lao": "Lao", "nep": "Nepali",
    "sin": "Sinhala", "mon": "Mongolian", "kaz": "Kazakh", "uzb": "Uzbek",
    "aze": "Azerbaijani", "geo": "Georgian", "kat": "Georgian",
    "arm": "Armenian", "hye": "Armenian", "alb": "Albanian", "sqi": "Albanian",
    "mac": "Macedonian", "mkd": "Macedonian", "bos": "Bosnian",
    "mul": "Multiple languages", "zxx": "No dialogue",
}

# Speaker layouts, in the words people use for them.
CHANNEL_NAMES = {1: "Mono", 2: "Stereo", 3: "2.1", 6: "5.1", 8: "7.1"}

TEXT_SUB_CODECS = {"subrip", "srt", "ass", "ssa", "mov_text", "webvtt", "text"}
IMAGE_SUB_CODECS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}


def analyze(path):
    """Full stream inventory for one file."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_streams", "-show_format", path],
            capture_output=True, text=True, timeout=120)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def _lang(stream):
    return (stream.get("tags") or {}).get("language", "und").lower()


def _title(stream):
    return ((stream.get("tags") or {}).get("title") or "").lower()


def _disp(stream, flag):
    return bool((stream.get("disposition") or {}).get(flag))


def is_image(stream):
    return (stream.get("codec_type") == "video"
            and (stream.get("codec_name") in IMAGE_CODECS
                 or _disp(stream, "attached_pic")))


def is_hdr(stream):
    if not stream:
        return False
    if stream.get("color_transfer") in HDR_TRANSFERS:
        return True
    return stream.get("color_primaries") == "bt2020"


def source_bit_depth(stream):
    if not stream:
        return 8
    bits = stream.get("bits_per_raw_sample")
    if bits:
        try:
            return int(bits)
        except ValueError:
            pass
    fmt = stream.get("pix_fmt") or ""
    if "12" in fmt:
        return 12
    return 10 if "10" in fmt else 8


def looks_forced(stream):
    """Forced subtitles translate foreign or invented dialogue.

    These are the tracks that render Klingon, Elvish or a few lines of
    Spanish for an otherwise English-speaking audience. They're small, they
    matter, and a blanket subtitle strip destroys them — so they're detected
    by flag first and by naming convention second, since plenty of releases
    never set the flag.
    """
    if _disp(stream, "forced"):
        return True
    title = _title(stream)
    return any(word in title for word in ("forced", "signs", "songs"))


def kept_tracks(info, spec):
    """The audio and subtitle streams that survive, in output order.

    Shares plan_streams' logic deliberately: two implementations of "which
    tracks are kept" would drift, and titles would end up on wrong tracks.
    """
    _maps, _disp, _notes, audios, subs = _plan(info, spec)
    return audios, subs


def plan_streams(info, spec):
    maps, disp, notes, _a, _s = _plan(info, spec)
    return maps, disp, notes


def _plan(info, spec):
    """Decide which streams to keep, in what order.

    Returns (map_args, codec_args, notes). Streams are mapped explicitly
    and in final order, so the output track order is exactly what's asked
    for rather than whatever the source happened to use.
    """
    streams = (info or {}).get("streams", [])
    notes = []

    videos = [s for s in streams if s.get("codec_type") == "video"]
    audios = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]

    # ---- video: the real one, never the cover art ------------------
    real_videos = [s for s in videos if not is_image(s)]
    images = [s for s in videos if is_image(s)]
    if images and spec.get("remove_images", True):
        notes.append(f"removed {len(images)} embedded image"
                     f"{'s' if len(images) > 1 else ''}")
    keep = list(real_videos[:1])
    if not spec.get("remove_images", True):
        keep += images

    # ---- audio: preferred languages first ---------------------------
    prefs = [l.lower() for l in (spec.get("audio_languages") or [])]
    if prefs:
        def audio_rank(stream):
            lang = _lang(stream)
            position = prefs.index(lang) if lang in prefs else len(prefs)
            # Within a language, a commentary track shouldn't outrank the film.
            commentary = 1 if "commentary" in _title(stream) else 0
            return (position, commentary, -(stream.get("channels") or 0))

        wanted = [s for s in audios if _lang(s) in prefs or _lang(s) == "und"]
        others = [s for s in audios if s not in wanted]

        if spec.get("remove_other_audio") and wanted:
            if others:
                dropped = sorted({_lang(s) for s in others})
                notes.append(f"dropped audio: {', '.join(dropped)}")
            audios = sorted(wanted, key=audio_rank)
        else:
            audios = sorted(wanted, key=audio_rank) + others
        if not audios:
            audios = [s for s in streams if s.get("codec_type") == "audio"]
            notes.append("kept all audio — none matched the preferred languages")

    # ---- subtitles ---------------------------------------------------
    mode = spec.get("subtitle_mode", "keep")
    sub_langs = [l.lower() for l in (spec.get("subtitle_languages") or [])]
    keep_forced = spec.get("keep_forced_subs", True)

    if mode == "strip":
        kept_subs = [s for s in subs if keep_forced and looks_forced(s)]
        if kept_subs:
            notes.append(f"kept {len(kept_subs)} forced subtitle track"
                         f"{'s' if len(kept_subs) > 1 else ''}")
    elif mode == "forced":
        kept_subs = [s for s in subs if looks_forced(s)]
    elif mode == "languages":
        kept_subs = [s for s in subs
                     if _lang(s) in sub_langs
                     or (keep_forced and looks_forced(s))]
    else:
        kept_subs = list(subs)

    # Forced tracks belong first — players pick the first matching track.
    kept_subs.sort(key=lambda s: (0 if looks_forced(s) else 1,
                                  sub_langs.index(_lang(s))
                                  if _lang(s) in sub_langs else len(sub_langs)))

    container = spec.get("container", "mkv")
    if container == "mp4":
        droppable = [s for s in kept_subs
                     if s.get("codec_name") in IMAGE_SUB_CODECS]
        if droppable:
            kept_subs = [s for s in kept_subs if s not in droppable]
            notes.append(f"dropped {len(droppable)} picture-based subtitle "
                         f"track{'s' if len(droppable) > 1 else ''} (MP4 can't hold them)")

    # ---- build the map in final order --------------------------------
    map_args, codec_args = [], []
    for stream in keep + audios + kept_subs:
        map_args += ["-map", f"0:{stream['index']}"]

    # Default flags: first audio and first subtitle, nothing else.
    for position, _stream in enumerate(audios):
        codec_args += [f"-disposition:a:{position}",
                       "default" if position == 0 else "0"]
    for position, stream in enumerate(kept_subs):
        flags = []
        if looks_forced(stream):
            flags.append("forced")
        if position == 0 and spec.get("default_first_subtitle"):
            flags.append("default")
        codec_args += [f"-disposition:s:{position}", "+".join(flags) or "0"]

    return map_args, codec_args, notes, audios, kept_subs


# ------------------------------------------------------------ colour

def tonemap_filter():
    """Convert HDR to SDR.

    Runs on the CPU: hardware encoders can't tonemap, so this is the one
    operation that stays slow no matter what silicon is available.
    """
    return ("zscale=transfer=linear:npl=100,format=gbrpf32le,"
            "zscale=primaries=bt709,tonemap=tonemap=hable:desat=0,"
            "zscale=transfer=bt709:matrix=bt709:range=tv,format=yuv420p")


def has_zscale():
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-filters"],
                             capture_output=True, text=True, timeout=30)
        return "zscale" in out.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


def pixel_format(encoder, want_depth, source_depth):
    """Pick an output pixel format for the requested bit depth."""
    depth = source_depth if want_depth in (None, "match") else int(want_depth)
    depth = 10 if depth >= 10 else 8
    if depth == 8:
        return "yuv420p", 8
    if encoder and encoder.endswith("videotoolbox"):
        return "p010le", 10
    if encoder and encoder.endswith("nvenc"):
        return "p010le", 10
    if encoder and encoder.endswith("qsv"):
        return "p010le", 10
    return "yuv420p10le", 10


def colour_args(spec, video_stream, tonemapped):
    """Tag the output so players interpret the colours correctly.

    An untagged HDR file looks washed out and grey; an SDR file wrongly
    tagged as HDR looks crushed. Tagging costs nothing and prevents both.
    """
    if not spec.get("tag_colours", True):
        return []
    if tonemapped or not is_hdr(video_stream):
        return ["-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-color_range", "tv"]
    return ["-colorspace", "bt2020nc", "-color_primaries", "bt2020",
            "-color_trc", "smpte2084", "-color_range", "tv"]


# ---------------------------------------------------------- track naming

def language_name(code):
    """Display name for a language code, or None if it isn't recognised."""
    if not code:
        return None
    code = code.strip().lower()
    if code in ("und", "unk", "", "none"):
        return None
    return LANGUAGE_NAMES.get(code) or LANGUAGE_NAMES.get(code[:2])


def language_from_title(title):
    """Guess a language code from a track's existing title.

    Plenty of releases leave the language tag empty but write it into the
    title. Only used when the tag itself is missing.
    """
    if not title:
        return None
    lowered = title.lower()
    for code, name in LANGUAGE_NAMES.items():
        if len(code) != 3:
            continue
        if re.search(rf"\b{re.escape(name.lower())}\b", lowered):
            return code
    return None


def channel_label(stream):
    channels = stream.get("channels")
    if not channels:
        return None
    layout = (stream.get("channel_layout") or "").lower()
    if "5.1" in layout:
        return "5.1"
    if "7.1" in layout:
        return "7.1"
    return CHANNEL_NAMES.get(int(channels), f"{channels}ch")


def is_commentary(stream):
    return (_disp(stream, "comment")
            or "commentary" in _title(stream)
            or "director" in _title(stream) and "cut" not in _title(stream))


def is_descriptive(stream):
    """Audio description for the visually impaired."""
    return (_disp(stream, "descriptions")
            or "descriptive" in _title(stream)
            or "audio description" in _title(stream))


def is_sdh(stream):
    return (_disp(stream, "hearing_impaired")
            or "sdh" in _title(stream)
            or "hearing impaired" in _title(stream)
            or "cc" == _title(stream).strip())


def describe_audio(stream):
    """A title a person would recognise: 'English 5.1', 'Japanese (Commentary)'."""
    code = _lang(stream)
    name = language_name(code) or language_name(language_from_title(_title(stream)))
    if not name:
        return None, None

    parts = [name]
    layout = channel_label(stream)
    if layout:
        parts.append(layout)

    extras = []
    if is_commentary(stream):
        extras.append("Commentary")
    if is_descriptive(stream):
        extras.append("Audio Description")
    title = " ".join(p for p in parts if p)
    if extras:
        title += " (" + ", ".join(extras) + ")"
    resolved = code if language_name(code) else language_from_title(_title(stream))
    return title, resolved


def describe_subtitle(stream):
    code = _lang(stream)
    name = language_name(code) or language_name(language_from_title(_title(stream)))
    if not name:
        return None, None
    extras = []
    if looks_forced(stream):
        extras.append("Forced")
    if is_sdh(stream):
        extras.append("SDH")
    title = name + (" (" + ", ".join(extras) + ")" if extras else "")
    resolved = code if language_name(code) else language_from_title(_title(stream))
    return title, resolved


def naming_args(audios, subs, spec):
    """FFmpeg arguments that give every kept track a readable name.

    Titles are written against the output stream positions, so this must
    match the order the streams were mapped in.
    """
    if not spec.get("tidy_track_names", True):
        return []

    args = []
    for position, stream in enumerate(audios):
        title, code = describe_audio(stream)
        if title:
            args += [f"-metadata:s:a:{position}", f"title={title}"]
        # Fill in a missing language tag when the title made it obvious;
        # players use the tag, not the title, to pick a default track.
        if code and not language_name(_lang(stream)):
            args += [f"-metadata:s:a:{position}", f"language={code}"]

    for position, stream in enumerate(subs):
        title, code = describe_subtitle(stream)
        if title:
            args += [f"-metadata:s:s:{position}", f"title={title}"]
        if code and not language_name(_lang(stream)):
            args += [f"-metadata:s:s:{position}", f"language={code}"]

    # Release groups often leave their name in the video stream's title.
    args += ["-metadata:s:v:0", "title="]
    return args
