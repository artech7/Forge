"""Turning release names into something a media server understands.

A downloaded file is a pile of tokens: title, year, resolution, source,
codec, audio layout, subtitle info, release group. Only the first two
matter for matching. Everything else is noise that actively confuses the
scanner, so the job is to find where the title ends and the noise begins.
"""
import re
from pathlib import Path

# Tokens that mark the end of a title. Once one of these appears, nothing
# after it belongs to the name of the film.
JUNK = {
    # resolution and scan
    "480p", "576p", "720p", "1080p", "1080i", "2160p", "4k", "8k", "uhd", "hd", "sd",
    # source
    "bluray", "blu-ray", "brrip", "bdrip", "bdremux", "remux", "webrip", "web-dl",
    "webdl", "web", "hdtv", "pdtv", "dvdrip", "dvdscr", "dvd", "hdrip", "cam",
    "ts", "telesync", "r5", "vodrip", "sdtv", "amzn", "nf", "dsnp", "hmax",
    "atvp", "hulu", "pcok", "stan", "itunes", "ma",
    # video codec
    "x264", "x265", "h264", "h265", "h", "avc", "hevc", "xvid", "divx", "av1",
    "vp9", "mpeg2", "10bit", "8bit", "10-bit", "hi10p", "hi10",
    # dynamic range
    "hdr", "hdr10", "hdr10+", "dv", "dovi", "dolbyvision", "sdr", "hlg",
    # audio
    "aac", "ac3", "eac3", "dd", "ddp", "dd5", "ddp5", "dts", "dts-hd", "dtshd",
    "dts-x", "truehd", "atmos", "flac", "mp3", "opus", "pcm", "lpcm",
    "2 0", "5 1", "7 1", "2ch", "6ch", "8ch", "dual", "dualaudio", "dual-audio",
    "multi", "multiaudio", "commentary",
    # subtitles
    "subrip", "srt", "ass", "pgs", "vobsub", "subbed", "sub", "subs", "multisub",
    "hardsub", "hardsubbed", "softsub", "esub", "esubs", "msubs",
    # container
    "mkv", "mp4", "avi", "m4v", "mov", "ts", "m2ts",
    # release flags
    "proper", "repack", "internal", "limited", "festival", "readnfo", "nfofix",
    "rerip", "dirfix", "sample", "complete", "extended", "unrated", "uncut",
    "remastered", "criterion", "imax", "theatrical", "directors", "cut",
    "final", "special", "edition", "anniversary", "3d", "half-sbs", "sbs", "hsbu",
    # language markers that follow a title
    "english", "eng", "japanese", "jpn", "spanish", "french", "german", "italian",
    "korean", "hindi", "dubbed", "dub",
}

# Words that stay lowercase inside a title, but never at the start.
SMALL_WORDS = {"a", "an", "and", "as", "at", "but", "by", "for", "from", "in",
               "into", "nor", "of", "on", "or", "over", "the", "to", "up", "vs",
               "with"}

EDITIONS = {
    "directors cut": "Director's Cut",
    "director's cut": "Director's Cut",
    "extended": "Extended",
    "extended cut": "Extended",
    "theatrical": "Theatrical",
    "unrated": "Unrated",
    "uncut": "Uncut",
    "remastered": "Remastered",
    "imax": "IMAX",
    "criterion": "Criterion",
    "final cut": "Final Cut",
}

YEAR = re.compile(r"\b(19[0-9]{2}|20[0-9]{2})\b")
EPISODE_PATTERNS = [
    re.compile(r"\bs(\d{1,2})[\s._-]*e(\d{1,3})\b", re.I),
    re.compile(r"\b(\d{1,2})x(\d{1,3})\b", re.I),
    re.compile(r"\bseason[\s._-]*(\d{1,2})[\s._-]*episode[\s._-]*(\d{1,3})\b", re.I),
]
BRACKETS = re.compile(r"[\[\(\{][^\])\}]*[\]\)\}]")
TMDB = re.compile(r"tmdb[\s._-]*(?:id)?[\s._-]*(\d+)", re.I)
TVDB = re.compile(r"tvdb[\s._-]*(?:id)?[\s._-]*(\d+)", re.I)
IMDB = re.compile(r"\b(tt\d{7,9})\b", re.I)


def _cap_segment(segment):
    """Capitalise one hyphen-free chunk, preserving short acronyms."""
    if not segment:
        return segment
    if segment.isupper() and len(segment) <= 5:
        return segment                  # WALL, MI, TRON, JFK
    return segment[:1].upper() + segment[1:].lower()


def _titlecase(text):
    words = text.split()
    out = []
    for index, word in enumerate(words):
        low = word.lower()
        if index and low in SMALL_WORDS:
            out.append(low)
        else:
            # Hyphenated names are capitalised per part: WALL-E, Spider-Man.
            out.append("-".join(_cap_segment(part) for part in word.split("-")))
    return " ".join(out)


def parse(filename):
    """Pull structure out of a release name.

    Returns title, year, season/episode when present, edition, and any
    provider IDs found. Confidence is low when the title had to be guessed
    without a year or episode marker to anchor it.
    """
    stem = Path(filename).stem
    raw = stem

    ids = {}
    for pattern, key in ((TMDB, "tmdb"), (TVDB, "tvdb")):
        found = pattern.search(raw)
        if found:
            ids[key] = found.group(1)
    found = IMDB.search(raw)
    if found:
        ids["imdb"] = found.group(1).lower()

    edition = None
    lowered = raw.lower().replace(".", " ").replace("_", " ")
    for marker, label in EDITIONS.items():
        if re.search(rf"\b{re.escape(marker)}\b", lowered):
            edition = label
            break

    # Pull the year out before stripping brackets — "Dune (2024) [1080p]"
    # keeps its year only if it's read first.
    bracket_year = re.search(r"[\[\(\{]\s*(19[0-9]{2}|20[0-9]{2})\s*[\]\)\}]", raw)
    working = BRACKETS.sub(" ", raw)
    if bracket_year:
        working += f" {bracket_year.group(1)}"
    working = re.sub(r"[._]+", " ", working)
    working = re.sub(r"\s*-\s*[A-Za-z0-9]+$", " ", working)   # trailing -GROUP
    working = re.sub(r"\s+", " ", working).strip()

    # --- TV first: an episode marker is the strongest anchor there is ---
    season = episode = None
    episode_title = None
    for pattern in EPISODE_PATTERNS:
        match = pattern.search(working)
        if match:
            season, episode = int(match.group(1)), int(match.group(2))
            title_part = working[:match.start()]
            tail = working[match.end():]
            # Episode titles sit between the marker and the first junk token.
            tail_words = []
            for word in tail.split():
                if word.lower().strip("-") in JUNK or YEAR.fullmatch(word):
                    break
                tail_words.append(word)
            episode_title = " ".join(tail_words).strip(" -") or None
            working = title_part
            break

    # --- year ---
    year = None
    years = YEAR.findall(working)
    if years:
        year = int(years[-1])
        cut = working.rfind(str(year))
        if cut > 0:
            working = working[:cut]

    # --- trim at the first junk token ---
    words, confident = [], bool(season or year)
    for word in working.split():
        clean = word.lower().strip("-()[]")
        if clean in JUNK:
            break
        if re.fullmatch(r"\d{3,4}p", clean):
            break
        words.append(word)

    title = _titlecase(" ".join(words).strip(" -–_")) or _titlecase(stem)
    if not words:
        confident = False

    return {
        "title": title,
        "year": year,
        "season": season,
        "episode": episode,
        "episode_title": _titlecase(episode_title) if episode_title else None,
        "edition": edition,
        "ids": ids,
        "is_tv": season is not None,
        "confident": confident,
        "original": stem,
    }


def _id_tag(parsed, scheme):
    """Provider IDs, in the syntax each server actually reads."""
    ids = parsed.get("ids") or {}
    if not ids:
        return ""
    key = "tvdb" if parsed["is_tv"] and "tvdb" in ids else \
          ("tmdb" if "tmdb" in ids else next(iter(ids)))
    value = ids[key]
    if scheme == "plex":
        return f" {{{key}-{value}}}"        # Plex reads curly braces
    return f" [{key}id-{value}]"            # Jellyfin and Emby read brackets


def safe(text):
    """Strip characters that break media servers or filesystems."""
    text = re.sub(r'[<>:"/\\|?*]', "", text or "")
    text = re.sub(r"\s+", " ", text).strip().rstrip(".")
    return text


def format_path(parsed, scheme="jellyfin", extension="mkv", folders=True):
    """Build the relative path this file should live at.

    All three servers agree on the shape. Jellyfin additionally requires the
    file name to start with its folder name exactly, which this satisfies by
    construction.
    """
    title = safe(parsed["title"]) or "Unknown"
    year = parsed.get("year")
    tag = _id_tag(parsed, scheme)

    if parsed["is_tv"]:
        show = f"{title} ({year})" if year else title
        show_dir = safe(show) + tag
        season = parsed["season"] or 1
        base = f"{safe(show)} - S{season:02d}E{parsed['episode']:02d}"
        if parsed.get("episode_title"):
            base += f" - {safe(parsed['episode_title'])}"
        name = f"{base}.{extension}"
        if not folders:
            return Path(name)
        return Path(show_dir) / f"Season {season:02d}" / name

    stem = f"{title} ({year})" if year else title
    folder = safe(stem) + tag

    # Jellyfin checks that the file name begins with its folder name, character
    # for character, so the ID tag goes before the edition rather than after.
    name = safe(stem) + (tag if scheme != "plex" else "")
    if parsed.get("edition"):
        if scheme == "plex":
            name += f" {{edition-{safe(parsed['edition'])}}}"
        else:
            name += f" - {safe(parsed['edition'])}"
    name = f"{name}.{extension}"
    if not folders:
        return Path(name)
    return Path(folder) / name


def resolve(filename, credential=None):
    """Parse a filename, then confirm it against TMDB when a key is set."""
    parsed = parse(filename)
    if credential:
        import lookup
        parsed = lookup.enrich(parsed, credential)
    return parsed


def preview(filename, scheme="jellyfin", extension="mkv", folders=True,
            credential=None):
    parsed = resolve(filename, credential)
    return {**parsed, "path": str(format_path(parsed, scheme, extension, folders))}
