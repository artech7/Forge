"""The catalog behind the setup wizard.

Every description here is written for someone who does not know what a
codec is. If a line needs jargon, it explains the jargon.
"""

VIDEO_CODECS = [
    {
        "id": "hevc",
        "name": "H.265",
        "also": "HEVC",
        "summary": "Best all-round choice",
        "detail": "Files come out roughly half the size of H.264 at the same "
                  "quality. Plays on most things made since about 2016 — "
                  "modern phones, smart TVs, streaming boxes.",
        "when": "Use this unless you have a specific reason not to.",
        "recommended": True,
    },
    {
        "id": "h264",
        "name": "H.264",
        "also": "AVC",
        "summary": "Plays on absolutely everything",
        "detail": "The safest possible choice. Older TVs, every browser, every "
                  "phone. The trade-off is file size — expect roughly double "
                  "what H.265 gives you.",
        "when": "Use this if you have older devices that stutter on H.265.",
    },
    {
        "id": "av1",
        "name": "AV1",
        "summary": "Smallest files, newest tech",
        "detail": "Squeezes files smaller than H.265, but takes much longer to "
                  "encode and only recent devices can play it without "
                  "struggling.",
        "when": "Good for archiving things you rarely watch.",
    },
    {
        "id": "copy",
        "name": "Leave video alone",
        "summary": "Don't re-encode the picture",
        "detail": "Keeps the original video exactly as it is. Useful when you "
                  "only want to change the audio, drop subtitles, or switch "
                  "the container. Very fast — nothing is re-compressed.",
        "when": "Use with a container change to repackage without quality loss.",
    },
]

CONTAINERS = [
    {
        "id": "mkv",
        "name": "MKV",
        "summary": "Best for a home library",
        "detail": "Holds anything: any codec, unlimited audio tracks, and "
                  "picture-based subtitles from Blu-rays. Jellyfin, Plex and "
                  "VLC all handle it natively.",
        "recommended": True,
    },
    {
        "id": "mp4",
        "name": "MP4",
        "summary": "Best for phones and browsers",
        "detail": "The most widely supported container, and the right pick if "
                  "you cast to a TV or watch in a browser. It cannot carry "
                  "picture-based subtitles — those get dropped or converted.",
    },
]

AUDIO_CODECS = [
    {
        "id": "aac",
        "name": "AAC",
        "summary": "The safe default",
        "detail": "Good quality at a small size, and plays on everything. "
                  "Stereo or surround.",
        "recommended": True,
        "bitrates": ["128k", "160k", "192k", "256k", "384k"],
        "default_bitrate": "160k",
    },
    {
        "id": "eac3",
        "name": "E-AC3",
        "also": "Dolby Digital Plus",
        "summary": "For a surround sound system",
        "detail": "Made for home theatre receivers. Keeps 5.1 and 7.1 surround "
                  "intact and passes through to your amp cleanly.",
        "bitrates": ["384k", "448k", "640k", "768k"],
        "default_bitrate": "640k",
    },
    {
        "id": "ac3",
        "name": "AC3",
        "also": "Dolby Digital",
        "summary": "Older surround standard",
        "detail": "The previous generation of surround audio. Slightly larger "
                  "than E-AC3 for the same quality, but supported by even very "
                  "old receivers.",
        "bitrates": ["384k", "448k", "640k"],
        "default_bitrate": "448k",
    },
    {
        "id": "opus",
        "name": "Opus",
        "summary": "Best quality for the size",
        "detail": "Beats everything else per megabyte, but older devices and "
                  "receivers often can't play it. Not allowed in MP4.",
        "bitrates": ["96k", "128k", "160k", "192k"],
        "default_bitrate": "128k",
    },
    {
        "id": "flac",
        "name": "FLAC",
        "summary": "Lossless, large",
        "detail": "Perfect audio with no quality loss, at several times the "
                  "size of AAC.",
        "bitrates": [],
    },
    {
        "id": "copy",
        "name": "Leave audio alone",
        "summary": "Keep the original tracks",
        "detail": "Passes every audio track through untouched. Fastest option, "
                  "and guarantees you lose nothing.",
        "bitrates": [],
    },
]

SUBTITLE_MODES = [
    {
        "id": "keep",
        "name": "Keep all subtitles",
        "detail": "Every subtitle track comes along. Best if you watch foreign "
                  "films or anime with different subtitle options.",
        "recommended": True,
    },
    {
        "id": "languages",
        "name": "Keep only certain languages",
        "detail": "Keeps the languages you list and drops the rest. Useful for "
                  "discs that ship with thirty tracks you'll never use.",
    },
    {
        "id": "strip",
        "name": "Remove all subtitles",
        "detail": "Drops every subtitle track. Smaller files, and it avoids "
                  "problems in MP4, which can't hold picture-based subtitles.",
    },
]

QUALITY_LEVELS = [
    {"id": "archive", "name": "Near-lossless", "crf": 18,
     "detail": "Visually identical to the source. Largest files."},
    {"id": "high", "name": "High", "crf": 20,
     "detail": "Very slightly softer than the source on close inspection."},
    {"id": "balanced", "name": "Balanced", "crf": 22, "recommended": True,
     "detail": "The sweet spot. Looks great on a TV, roughly half the size."},
    {"id": "small", "name": "Small files", "crf": 26,
     "detail": "Noticeably compressed on a big screen, but very space-efficient."},
    {"id": "advanced", "name": "Advanced", "crf": 22,
     "detail": "Pick an exact point between quality and size yourself, "
              "instead of one of the presets above."},
]

HDR_MODES = [
    {"id": "preserve", "name": "Keep HDR as it is", "recommended": True,
     "detail": "HDR files stay HDR. Right choice if your TV supports it — "
               "converting would throw away the extra colour and brightness."},
    {"id": "sdr", "name": "Convert HDR to normal (SDR)",
     "detail": "Useful if HDR films look washed out or grey on your devices, "
               "which happens on older TVs, phones and computer monitors. "
               "This runs on the processor rather than the graphics chip, so "
               "it is noticeably slower."},
]

BIT_DEPTHS = [
    {"id": "match", "name": "Match the original", "recommended": True,
     "detail": "A 10-bit file stays 10-bit, an 8-bit file stays 8-bit. "
               "Safest option and avoids pointless conversion."},
    {"id": "10", "name": "Always use 10-bit",
     "detail": "Smoother gradients in skies and dark scenes, and slightly "
               "better compression even for 8-bit sources. Some older "
               "devices can't play 10-bit H.265."},
    {"id": "8", "name": "Always use 8-bit",
     "detail": "Maximum compatibility with older hardware, at the cost of "
               "visible banding in gradients."},
]

AUDIO_LANGUAGE_MODES = [
    {"id": "keep_all", "name": "Keep every audio track", "recommended": True,
     "detail": "Nothing is removed. Preferred languages are still moved to "
               "the front so they play by default."},
    {"id": "preferred_only", "name": "Keep only my languages",
     "detail": "Removes audio in other languages entirely. Big space saving "
               "on films that ship with eight dubs, but it is permanent. "
               "If a file has none of your chosen languages at all — a "
               "Japanese-only anime episode when English is preferred, say "
               "— every track on it is kept instead, so you never end up "
               "with a silent video."},
]

SUBTITLE_MODES_EXTRA = [
    {"id": "forced", "name": "Keep only forced subtitles",
     "detail": "Forced subtitles translate the bits of a film in another "
               "language — Klingon, Elvish, a scene in Spanish. You get "
               "those translations without subtitles over the whole film."},
]

NAMING_SCHEMES = [
    {"id": "jellyfin", "name": "Jellyfin", "recommended": True,
     "detail": "Movie Name (Year)/Movie Name (Year).mkv, and for shows, "
               "Show/Season 01/Show - S01E01 - Title.mkv. Database IDs go in "
               "square brackets."},
    {"id": "emby", "name": "Emby",
     "detail": "The same layout as Jellyfin, including the square-bracket "
               "style for database IDs."},
    {"id": "plex", "name": "Plex",
     "detail": "The same layout, but database IDs and edition names use curly "
               "braces, which is what Plex reads."},
]

# Steps down from whatever quality the library normally uses. Expressed as
# CRF offsets because that scale is the common language across encoders.
RETRY_STEPS = [
    {"id": "small", "name": "A bit smaller", "offset": 4,
     "detail": "Usually enough. Hard to tell apart on a TV."},
    {"id": "smaller", "name": "Noticeably smaller", "offset": 8,
     "detail": "Visible softening on close inspection, roughly half the size."},
    {"id": "smallest", "name": "Much smaller", "offset": 12,
     "detail": "Clearly compressed on a big screen. For files you keep but "
               "rarely watch."},
    {"id": "custom", "name": "A quality I choose", "offset": None,
     "detail": "Set the number yourself. Higher means smaller and softer; "
               "the usual range is 18 to 34."},
]

# One shared scale, so the same words appear wherever a quality is chosen.
# Lower numbers mean better quality and larger files. These are FFmpeg CRF
# values, but nothing in the interface should ever say "CRF".
QUALITY_SCALE = [
    {"value": 18, "name": "Near-lossless", "detail": "Indistinguishable from "
     "the source. Largest files."},
    {"value": 20, "name": "Very high", "detail": "No visible loss on a big "
     "screen."},
    {"value": 22, "name": "High", "detail": "The usual default. Looks great, "
     "roughly half the size."},
    {"value": 24, "name": "Good", "detail": "A touch softer, meaningfully "
     "smaller."},
    {"value": 26, "name": "Moderate", "detail": "Slight softening in detailed "
     "scenes."},
    {"value": 28, "name": "Smaller", "detail": "Visible on close inspection, "
     "fine on a phone or tablet."},
    {"value": 30, "name": "Small", "detail": "Noticeably compressed on a TV."},
    {"value": 32, "name": "Quite small", "detail": "Clearly compressed. Good "
     "for things you rarely watch."},
    {"value": 34, "name": "Very small", "detail": "Obvious artefacts in motion "
     "and dark scenes."},
    {"value": 36, "name": "Heavily compressed", "detail": "Space above all "
     "else."},
]

ORIGINAL_ACTIONS = [
    {"id": "archive", "name": "Move it to an Originals folder", "recommended": True,
     "detail": "The source file is kept next to the library in an 'Originals' "
               "folder so you can check the result and delete it yourself later."},
    {"id": "delete", "name": "Delete it",
     "detail": "The source file is removed once the new one is written "
               "successfully. Saves space immediately; there is no undo."},
    {"id": "keep", "name": "Leave it where it is",
     "detail": "Nothing is removed. The watch folder will keep filling up, so "
               "only pick this if you tidy up by hand."},
]

UNHEALTHY_VIDEO_ACTIONS = [
    {"id": "ignore", "name": "Flag it for me to review", "recommended": True,
     "detail": "The file is marked Ignored, separately from ordinary "
               "failures, so it's clear at a glance this isn't just a slow "
               "retry — the video itself won't decode and needs a person "
               "to decide what happens to it."},
    {"id": "delete_and_research", "name": "Remove it and ask Radarr/Sonarr "
     "to fetch it again",
     "detail": "Deleted through Radarr or Sonarr's own API — so their "
               "library stays correct, not just Forge's view of it — then "
               "a new search is requested automatically. Needs Radarr or "
               "Sonarr connected below."},
]

ARR_KINDS = [
    {"id": "none", "name": "Neither",
     "detail": "Forge won't ask anything to re-download files for this library."},
    {"id": "radarr", "name": "Radarr", "detail": "For a movie library."},
    {"id": "sonarr", "name": "Sonarr", "detail": "For a TV library."},
]


def catalog():
    return {
        "hdr": HDR_MODES,
        "depth": BIT_DEPTHS,
        "audio_languages": AUDIO_LANGUAGE_MODES,
        "video": VIDEO_CODECS,
        "containers": CONTAINERS,
        "audio": AUDIO_CODECS,
        "subtitles": SUBTITLE_MODES[:1] + SUBTITLE_MODES_EXTRA + SUBTITLE_MODES[1:],
        "quality": QUALITY_LEVELS,
        "originals": ORIGINAL_ACTIONS,
        "naming": NAMING_SCHEMES,
        "retry_steps": RETRY_STEPS,
        "quality_scale": QUALITY_SCALE,
        "unhealthy_video": UNHEALTHY_VIDEO_ACTIONS,
        "arr_kinds": ARR_KINDS,
    }


def warnings_for(profile):
    """Plain-language warnings about combinations that will bite.

    Shown in the wizard's review step rather than failing later at encode time.
    """
    out = []
    container = profile.get("container")
    audio = profile.get("audio_codec")
    subs = profile.get("subtitle_mode")
    video = profile.get("video_codec")

    if container == "mp4" and audio == "opus":
        out.append("MP4 can't hold Opus audio. Pick AAC, or switch to MKV.")
    if container == "mp4" and audio == "flac":
        out.append("MP4 can't hold FLAC audio. Pick AAC, or switch to MKV.")
    if container == "mp4" and subs == "keep":
        out.append("MP4 can't hold picture-based subtitles from Blu-rays. "
                   "Text subtitles will convert; the rest will be dropped.")
    if video == "copy" and profile.get("quality_level"):
        out.append("Quality has no effect when the video is left alone.")
    if video == "av1":
        out.append("AV1 encodes slowly. A two-hour film can take several hours "
                   "on a CPU-only node.")
    if profile.get("hdr_mode") == "sdr":
        out.append("Converting HDR to SDR runs on the processor, not the "
                   "graphics chip. Expect these files to take considerably "
                   "longer than the rest.")
    if profile.get("bit_depth") == "10" and video == "h264":
        out.append("10-bit H.264 plays on very little hardware. 10-bit is a "
                   "much safer choice with H.265.")
    if (profile.get("audio_language_mode") == "preferred_only"
            and not (profile.get("audio_languages_list") or [])):
        out.append("No preferred languages listed, so no audio would be "
                   "removed. Add at least one, such as eng.")
    if profile.get("normalise_loudness") and profile.get("audio_codec") == "copy":
        out.append("Evening out the volume means re-encoding the audio, so "
                   "'Leave audio alone' can't be used with it.")
    if profile.get("downmix") == "stereo" and profile.get("add_stereo_track"):
        out.append("Downmixing to stereo removes the surround track, so there "
                   "is nothing left to add a stereo companion to. Pick one.")
    if profile.get("downmix") == "stereo":
        out.append("Downmixing to stereo removes surround sound permanently. "
                   "Good for phones and laptops, not for a home theatre.")
    if profile.get("subtitle_mode") == "strip" and not profile.get("keep_forced_subs"):
        out.append("Forced subtitles will be removed too. Films with alien or "
                   "foreign dialogue will lose those translations.")

    # ---- settings that pull against each other -----------------------
    # These are the ones where a setting is fine on its own but works
    # against something else you've chosen. Each says what to change
    # rather than only what's wrong.
    skip_ext = [str(e).lower().lstrip(".")
                for e in ((profile.get("filters") or {}).get("skip_extensions") or [])]

    if container and container.lower() in skip_ext:
        out.append(
            f"'{container.upper()}' is in Skip these file types, which is the "
            f"container you're converting *to*. Anything already in "
            f"{container.upper()} will never be looked at, even when its codec "
            "is wrong. Remove it from the skip list and let the codec check "
            "decide instead.")

    for ext in skip_ext:
        if ext in ("mkv", "mp4", "m4v", "avi", "ts", "mov") and ext != (container or "").lower():
            out.append(
                f"Skipping '{ext}' files skips them entirely — Forge won't even "
                "check what codec they use. If the intention is to leave "
                "already-converted files alone, 'Leave existing files in the "
                "container they're in' on the Video step does that while still "
                "catching ones with the wrong codec.")
            break

    if profile.get("keep_existing_container"):
        out.append(
            "With 'Leave existing files in the container they're in' on, every "
            f"file keeps its own container, so the {container.upper() if container else 'chosen'} "
            "setting above only applies to files that have no container of their "
            "own — in practice, none. Files in odd containers like AVI stay AVI. "
            "Turn it off if you'd rather everything ended up the same type.")

    if profile.get("add_stereo_track") and profile.get("downmix") != "stereo":
        out.append(
            "Adding a stereo track makes files a little larger — roughly 100–150 "
            "MB per film. Worth it if people watch on phones or in browsers; "
            "switch it off if everything you play on handles surround.")

    if profile.get("auto_level_loudness") and not profile.get("auto_measure_loudness"):
        out.append(
            "Levelling automatically needs measuring automatically — nothing "
            "will be levelled until a measurement exists. Turn on 'Measure "
            "loudness on its own' as well, or level by hand from Library Health.")

    if profile.get("auto_measure_loudness") and profile.get("original_action") == "delete":
        out.append(
            "Originals are deleted, so a levelled file can't be reverted if you "
            "don't like the result. Consider 'Move it to an Originals folder' "
            "while you're still deciding what settings you want.")

    if profile.get("original_action") == "delete" and profile.get("auto_retry"):
        out.append(
            "Trying again at a smaller setting needs the original, which is "
            "deleted here. Files that come out bigger will be left as they are "
            "instead of retried.")

    audio_bitrate = str(profile.get("audio_bitrate") or "")
    if audio == "aac" and audio_bitrate.rstrip("k").isdigit() \
            and int(audio_bitrate.rstrip("k")) > 256:
        out.append(
            f"{audio_bitrate} is a lot for AAC — 160k is transparent for stereo "
            "and 256k is generous even for surround. Higher just makes files "
            "bigger.")

    if profile.get("downmix") == "stereo" and (profile.get("audio_languages_list") or []):
        out.append(
            "Downmixing applies to every audio track that's kept, including "
            "each language. Removing languages you don't want first keeps the "
            "file smaller.")

    # The wizard sends its own flat draft here, while a saved library keeps
    # these three under "arr" — read whichever shape turned up.
    arr = profile.get("arr") or {}
    field = lambda name, alt: profile.get(alt, arr.get(name))
    wants_replacing = (field("on_unhealthy_video", "on_unhealthy_video")
                       == "delete_and_research"
                       or field("auto_replace_missing_audio",
                                "auto_replace_missing_audio"))
    if wants_replacing and field("kind", "arr_kind") not in ("radarr", "sonarr"):
        out.append(
            "Replacing a broken file needs something to replace it with, and "
            "this library isn't managed by Radarr or Sonarr. Pick one under "
            "\"Managed by\" on the Basics step, or those files will just be "
            "listed for you instead.")

    return out


def resolve(profile):
    """Expand a wizard profile into the job spec workers understand."""
    crf = 22
    for level in QUALITY_LEVELS:
        if level["id"] == profile.get("quality_level"):
            crf = level["crf"]
    return {
        "codec": profile.get("video_codec") or "hevc",
        "remove_images": profile.get("remove_images", True),
        "hdr_mode": profile.get("hdr_mode", "preserve"),
        "bit_depth": profile.get("bit_depth", "match"),
        "tag_colours": profile.get("tag_colours", True),
        "audio_languages": profile.get("audio_languages_list") or ["eng"],
        "remove_other_audio":
            profile.get("audio_language_mode") == "preferred_only",
        "keep_forced_subs": profile.get("keep_forced_subs", True),
        "keep_chapters": profile.get("keep_chapters", True),
        "tidy_track_names": profile.get("tidy_track_names", True),
        "min_saving_percent": float(profile.get("min_saving_percent") or 0),
        "salvage_when_stuck": profile.get("salvage_when_stuck", True),
        "keep_existing_container": profile.get("keep_existing_container", False),
        "loudness_target_i": float(profile.get("loudness_target_i") or -16),
        "loudness_target_tp": float(profile.get("loudness_target_tp") or -1.5),
        "loudness_target_lra": float(profile.get("loudness_target_lra") or 11),
        "clean_metadata": profile.get("clean_metadata", False),
        "downmix": profile.get("downmix"),
        "add_stereo_track": profile.get("add_stereo_track", False),
        "stereo_track_codec": profile.get("stereo_track_codec") or "aac",
        "stereo_track_bitrate": profile.get("stereo_track_bitrate") or "160k",
        "normalise_loudness": profile.get("normalise_loudness", False),
        "default_first_subtitle": profile.get("default_first_subtitle", False),
        "quality": profile.get("crf_override") or crf,
        "container": profile.get("container") or "mkv",
        "audio": profile.get("audio_codec") or "aac",
        # `or` rather than a .get default: a key present-but-empty is the
        # common case here, and a default only fires when the key is absent.
        "audio_bitrate": profile.get("audio_bitrate") or "160k",
        "subtitle_mode": profile.get("subtitle_mode") or "keep",
        "subtitle_languages": profile.get("subtitle_languages", []),
    }


def base_quality(profile):
    """The CRF this library normally converts at."""
    if profile.get("crf_override"):
        return int(profile["crf_override"])
    for level in QUALITY_LEVELS:
        if level["id"] == profile.get("quality_level"):
            return level["crf"]
    return 22


def retry_ladder(profile):
    """Quality values to try, in order, when a file comes out too big.

    Returns absolute CRF values so the worker needs no extra context. An
    empty list means the library isn't set up to retry automatically.
    """
    if not profile.get("auto_retry"):
        return []
    base = base_quality(profile)
    chosen = profile.get("auto_retry_steps") or ["small", "smaller"]
    ladder = []
    for step in RETRY_STEPS:
        if step["id"] in chosen and step["offset"] is not None:
            ladder.append(min(40, base + step["offset"]))
    custom = profile.get("auto_retry_custom")
    if "custom" in chosen and custom:
        ladder.append(max(1, min(51, int(custom))))
    return sorted(set(ladder))


def manual_steps(profile):
    """Options offered on the review list, as (id, name, quality, detail)."""
    base = base_quality(profile)
    out = []
    for step in RETRY_STEPS:
        if step["offset"] is None:
            out.append({**step, "quality": None})
        else:
            out.append({**step, "quality": min(40, base + step["offset"])})
    return out


# Repackaging into a different container changes muxing overhead by at
# most a few hundred KB, regardless of how big the file is — a percentage
# tolerance would let a huge file grow by megabytes unnoticed while still
# being too tight for a small one, so this is a flat byte count instead.
# Without it, a remux landing on exactly the size it started at — the
# expected, successful outcome for a plain repack — got reported as
# "came out 0% larger" and run through the same handling as a genuinely
# bloated re-encode.
GROWTH_NOISE_FLOOR = 2 * 1024 * 1024  # bytes


def savings_verdict(size_before, size_after, min_percent=0.0):
    """Was this conversion worth keeping?

    Returns (ok, percent_saved, reason). A negative percentage means the
    file grew.
    """
    if not size_before or not size_after:
        return True, 0.0, None
    percent = (size_before - size_after) / size_before * 100
    if size_after - size_before > GROWTH_NOISE_FLOOR:
        return False, percent, f"came out {abs(percent):.0f}% larger"
    if min_percent and percent < min_percent:
        return False, percent, (f"only saved {percent:.0f}%, below the "
                                f"{min_percent:.0f}% you asked for")
    return True, percent, None
