"""TMDB lookups, so a title comes from the database rather than a guess.

Deliberately conservative: every failure path falls back to the offline
parser rather than blocking a transcode. A media server that never renames
is annoying; one that renames wrongly because an API hiccupped is worse.

Uses the standard library only — the server has no HTTP client dependency
and this isn't worth adding one for.
"""
import difflib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

import db

BASE = "https://api.themoviedb.org/3"
TIMEOUT = 8
CACHE_DAYS = 30

# TMDB asks that applications using its data say so.
ATTRIBUTION = ("This product uses the TMDB API but is not endorsed or "
               "certified by TMDB.")


def _cache_get(key):
    with db.connect() as conn:
        row = conn.execute(
            "SELECT value, at FROM lookup_cache WHERE key=?", (key,)).fetchone()
    if not row:
        return None
    if time.time() - (row["at"] or 0) > CACHE_DAYS * 86400:
        return None
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return None


def _cache_put(key, value):
    with db.connect() as conn:
        conn.execute(
            """INSERT INTO lookup_cache (key, value, at) VALUES (?,?,?)
               ON CONFLICT(key) DO UPDATE SET
                 value=excluded.value, at=excluded.at""",
            (key, json.dumps(value), time.time()))


class TMDB:
    """Minimal TMDB client. Accepts either credential type."""

    def __init__(self, credential):
        self.credential = (credential or "").strip()
        # v4 read tokens are JWTs; v3 keys are 32-character hex strings.
        self.is_bearer = self.credential.startswith("eyJ")

    @property
    def configured(self):
        return bool(self.credential)

    def _get(self, path, params=None, use_cache=True):
        if not self.configured:
            return None
        params = dict(params or {})
        if not self.is_bearer:
            params["api_key"] = self.credential
        url = f"{BASE}{path}?{urllib.parse.urlencode(params)}"

        cache_key = f"{path}?{urllib.parse.urlencode({k: v for k, v in params.items() if k != 'api_key'})}"
        if use_cache:
            hit = _cache_get(cache_key)
            if hit is not None:
                return hit

        request = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "User-Agent": "Forge/1.0",
            **({"Authorization": f"Bearer {self.credential}"} if self.is_bearer else {}),
        })
        try:
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError,
                json.JSONDecodeError, TimeoutError, OSError):
            return None
        if use_cache:
            _cache_put(cache_key, data)
        return data

    # ------------------------------------------------------------ checks

    def test(self):
        """Verify the credential. Returns (ok, message)."""
        if not self.configured:
            return False, "No API key set"
        data = self._get("/configuration", use_cache=False)
        if data is None:
            return False, "Could not reach TMDB, or the key was rejected"
        if "images" not in data:
            return False, "TMDB responded but the key looks wrong"
        kind = "read access token" if self.is_bearer else "API key"
        return True, f"Connected — {kind} accepted"

    # ------------------------------------------------------------ search

    def find_movie(self, title, year=None):
        params = {"query": title, "include_adult": "false"}
        if year:
            params["year"] = str(year)
        data = self._get("/search/movie", params)
        results = (data or {}).get("results") or []
        if not results and year:
            # The year in a release name is often the release year in another
            # territory, so a miss is worth retrying without it.
            data = self._get("/search/movie", {"query": title,
                                               "include_adult": "false"})
            results = (data or {}).get("results") or []
        return _best(results, title, year, "title", "release_date")

    def find_show(self, title, year=None):
        data = self._get("/search/tv", {"query": title, "include_adult": "false"})
        results = (data or {}).get("results") or []
        return _best(results, title, year, "name", "first_air_date")

    def episode_name(self, show_id, season, episode):
        data = self._get(f"/tv/{show_id}/season/{season}/episode/{episode}")
        return (data or {}).get("name")

    def external_ids(self, kind, tmdb_id):
        data = self._get(f"/{kind}/{tmdb_id}/external_ids")
        return data or {}


def _score(candidate_title, query, candidate_year, want_year):
    """How well a result matches, 0 to 1.

    Title similarity dominates; a matching year is a strong confirmation but
    can't rescue a title that doesn't look right.
    """
    ratio = difflib.SequenceMatcher(
        None, _norm(candidate_title), _norm(query)).ratio()
    if want_year and candidate_year:
        if candidate_year == want_year:
            ratio += 0.15
        elif abs(candidate_year - want_year) == 1:
            ratio += 0.05          # release years straddle new year often
        else:
            ratio -= 0.20
    return ratio


def _norm(text):
    text = re.sub(r"[^\w\s]", "", (text or "").lower())
    return re.sub(r"\s+", " ", text).strip()


def _best(results, query, want_year, title_key, date_key):
    """Pick the best candidate, or None if nothing is convincing enough."""
    best, best_score = None, 0.0
    for item in results[:10]:
        date = item.get(date_key) or ""
        year = int(date[:4]) if date[:4].isdigit() else None
        score = _score(item.get(title_key), query, year, want_year)
        # Popularity only breaks ties between similarly good title matches.
        score += min(item.get("popularity", 0) / 1000, 0.05)
        if score > best_score:
            best, best_score = item, score

    # Below this, a wrong rename is more likely than a right one.
    if not best or best_score < 0.62:
        return None
    date = best.get(date_key) or ""
    return {
        "id": best.get("id"),
        "title": best.get(title_key),
        "year": int(date[:4]) if date[:4].isdigit() else None,
        "confidence": round(min(best_score, 1.0), 2),
    }


# ---------------------------------------------------------------- enrich

def enrich(parsed, credential):
    """Replace a guessed title with TMDB's, when a confident match exists.

    Returns the parsed dict either way — untouched if there's no key, no
    network, or no convincing match, with a 'looked_up' flag saying which.
    """
    client = TMDB(credential)
    if not client.configured or not parsed.get("title"):
        return {**parsed, "looked_up": False}

    if parsed.get("is_tv"):
        match = client.find_show(parsed["title"], parsed.get("year"))
        if not match:
            return {**parsed, "looked_up": False}
        updated = {**parsed, "title": match["title"],
                   "year": match["year"] or parsed.get("year"),
                   "confident": True, "looked_up": True,
                   "match_confidence": match["confidence"]}
        ids = dict(parsed.get("ids") or {})
        ids["tmdb"] = str(match["id"])
        external = client.external_ids("tv", match["id"])
        if external.get("tvdb_id"):
            ids["tvdb"] = str(external["tvdb_id"])
        updated["ids"] = ids
        if parsed.get("season") and parsed.get("episode"):
            name = client.episode_name(match["id"], parsed["season"],
                                       parsed["episode"])
            if name:
                updated["episode_title"] = name
        return updated

    match = client.find_movie(parsed["title"], parsed.get("year"))
    if not match:
        return {**parsed, "looked_up": False}
    ids = dict(parsed.get("ids") or {})
    ids["tmdb"] = str(match["id"])
    return {**parsed, "title": match["title"],
            "year": match["year"] or parsed.get("year"),
            "ids": ids, "confident": True, "looked_up": True,
            "match_confidence": match["confidence"]}
