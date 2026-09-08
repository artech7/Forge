"""Radarr/Sonarr integration, for files a health check finds unrecoverable.

Same philosophy as lookup.py: standard library only, every failure path
returns a plain description rather than raising, and nothing here ever
touches a file directly — Radarr and Sonarr manage their own libraries,
so the safest thing Forge can do is ask them to do it, not reach around
them.
"""
import json
import urllib.error
import urllib.parse
import urllib.request

TIMEOUT = 15


def _request(base_url, api_key, method, path, params=None, body=None):
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers={
        "X-Api-Key": api_key,
        "Content-Type": "application/json",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else None


def test_connection(kind, base_url, api_key):
    """Used by the wizard's "Test connection" button — never raises."""
    try:
        status = _request(base_url, api_key, "GET", "/api/v3/system/status")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return False, "That API key was rejected."
        return False, f"Server responded with an error ({exc.code})."
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"Could not reach that address: {exc.reason if hasattr(exc, 'reason') else exc}"
    except (json.JSONDecodeError, ValueError):
        return False, "That address responded, but not like Radarr or Sonarr."

    name = (status or {}).get("appName", "")
    expected = "Radarr" if kind == "radarr" else "Sonarr"
    if name and expected.lower() not in name.lower():
        return False, f"That address is answering as {name or 'something else'}, not {expected}."
    return True, f"Connected to {name or expected} {status.get('version', '')}".strip()


def remap_path(path, path_from, path_to):
    """Translate Forge's view of a path into the *arr's view of it.

    The same problem the worker's MOUNTS setting solves for a different
    machine seeing a different drive letter — here it's Forge's container
    mount versus Radarr/Sonarr's own. A plain prefix swap covers every
    real case without needing per-library special-casing.
    """
    if not path_from:
        return path
    if path.startswith(path_from):
        return path_to.rstrip("/") + path[len(path_from):]
    return path


def find_and_research(kind, base_url, api_key, path, path_from="", path_to=""):
    """Delete the file via Radarr/Sonarr's own API, then ask it to re-fetch.

    Deleting through the *arr's API (rather than Forge unlinking the file
    itself) keeps its database consistent and is what actually triggers
    the "missing" state that makes a new search meaningful. Returns a
    plain-language result either way — this is reported straight into a
    job's outcome, not logged somewhere separate.
    """
    remapped = remap_path(path, path_from, path_to)
    try:
        parsed = _request(base_url, api_key, "GET", "/api/v3/parse",
                          params={"path": remapped})
    except urllib.error.HTTPError as exc:
        return False, f"Radarr/Sonarr rejected the lookup ({exc.code})."
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"Could not reach Radarr/Sonarr: {getattr(exc, 'reason', exc)}"
    except (json.JSONDecodeError, ValueError):
        return False, "Radarr/Sonarr sent back something unexpected."

    if not parsed:
        return False, f"Radarr/Sonarr didn't recognise this path: {remapped}"

    if kind == "radarr":
        movie = parsed.get("movie") or {}
        movie_file = parsed.get("movieFile") or (movie.get("movieFile")
                                                 if movie else None)
        if not movie or not movie_file:
            return False, "Radarr knows this file, but has no file record for it."
        try:
            _request(base_url, api_key, "DELETE",
                    f"/api/v3/moviefile/{movie_file['id']}")
            _request(base_url, api_key, "POST", "/api/v3/command", body={
                "name": "MoviesSearch", "movieIds": [movie["id"]],
            })
        except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
            return False, f"Deleted the record, but the new search failed: {exc}"
        return True, f"Removed and asked Radarr to search again for {movie.get('title', 'this movie')}."

    # sonarr
    episodes = parsed.get("episodes") or []
    episode_file = parsed.get("episodeFile")
    series = parsed.get("series") or {}
    if not episodes or not episode_file:
        return False, "Sonarr knows this file, but has no file record for it."
    try:
        _request(base_url, api_key, "DELETE",
                f"/api/v3/episodefile/{episode_file['id']}")
        _request(base_url, api_key, "POST", "/api/v3/command", body={
            "name": "EpisodeSearch",
            "episodeIds": [e["id"] for e in episodes],
        })
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        return False, f"Deleted the record, but the new search failed: {exc}"
    return True, f"Removed and asked Sonarr to search again for {series.get('title', 'this episode')}."


# --------------------------------------------------------------- Bazarr
# Same shape as the Radarr/Sonarr client above: standard library only,
# every failure returns a description rather than raising, and Forge
# never touches subtitle files itself — Bazarr owns that, so the most
# Forge should do is tell it which files need looking at.


def test_bazarr(base_url, api_key):
    """Used by the wizard's "Test connection" button — never raises."""
    try:
        status = _request(base_url, api_key, "GET", "/api/system/status")
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False, "That API key was rejected."
        return False, f"Bazarr responded with an error ({exc.code})."
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return False, f"Could not reach that address: {getattr(exc, 'reason', exc)}"
    except (json.JSONDecodeError, ValueError):
        return False, "That address responded, but not like Bazarr."
    version = ((status or {}).get("data") or {}).get("bazarr_version", "")
    return True, f"Connected to Bazarr {version}".strip()


def search_subtitles(base_url, api_key, path, path_from="", path_to=""):
    """Ask Bazarr to go looking for subtitles for one file.

    Bazarr indexes by movie and episode rather than by path, so the path
    has to be resolved to an id first. Both lookups are paged endpoints
    with no path filter, which is why this scans rather than queries —
    fine for the handful of files someone selects, and the reason this
    isn't offered as a whole-library button.
    """
    target = remap_path(path, path_from, path_to)

    for kind, listing, id_field, param in (
            ("movie", "/api/movies", "radarrId", "radarrid"),
            ("episode", "/api/episodes/wanted", "sonarrEpisodeId", "seriesid")):
        try:
            found = _find_in_bazarr(base_url, api_key, listing, target, id_field)
        except (urllib.error.HTTPError, urllib.error.URLError, OSError,
                json.JSONDecodeError, ValueError):
            continue
        if not found:
            continue
        endpoint = ("/api/movies/subtitles" if kind == "movie"
                    else "/api/episodes/subtitles")
        try:
            _request(base_url, api_key, "PATCH", endpoint,
                     params={param: found})
        except urllib.error.HTTPError as exc:
            return False, f"Bazarr refused the search ({exc.code})."
        except (urllib.error.URLError, OSError) as exc:
            return False, f"Could not reach Bazarr: {getattr(exc, 'reason', exc)}"
        return True, f"Asked Bazarr to search for this {kind}."

    return False, ("Bazarr doesn't recognise this path. If Bazarr sees your "
                   "library at a different path than Forge does, set the path "
                   "mapping.")


def _find_in_bazarr(base_url, api_key, listing, target, id_field):
    """The id Bazarr uses for whichever item owns this path, or None."""
    start, page = 0, 250
    while True:
        payload = _request(base_url, api_key, "GET", listing,
                           params={"start": start, "length": page}) or {}
        rows = payload.get("data") or []
        if not rows:
            return None
        for row in rows:
            row_path = row.get("path") or ""
            if row_path == target or row_path.endswith(target.split("/")[-1]):
                return row.get(id_field)
        if len(rows) < page:
            return None
        start += page
        if start > 20000:      # don't scan forever on a huge library
            return None
