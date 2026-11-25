"""
MusicBrainz: the open music encyclopedia. Crate Dive uses it for facts Spotify and Last.fm
don't have: where an artist is from, when they started, and whether they're a person or a group.

Rules MusicBrainz asks for (https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting):
  - an identifying User-Agent with a way to contact us
  - about one request a second per IP; too fast and they answer 503
Same pattern as lastfm.py: one shared gate, and a quiet period if we're told off.
"""
import re
import threading
import time

import requests

API = "https://musicbrainz.org/ws/2/"
USER_AGENT = "dycast-stats/1.0 ( https://dycast-stats.montlab.cc )"
MIN_GAP = 1.5
BACKOFF = 5 * 60            # first pause after a 503; doubles each time in a row, up to MAX_BACKOFF
MAX_BACKOFF = 60 * 60
MBID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')

_lock = threading.Lock()
_last_call = 0.0
_blocked_until = 0.0
_strikes = 0                # 503s in a row (reset by any good answer)
last_problem = None         # what MusicBrainz said last time it refused, for the status page


class RateLimited(Exception):
    pass


def blocked():
    """True while we're in a quiet period, so callers can skip MusicBrainz instead of waiting."""
    return time.monotonic() < _blocked_until


def _get(path, timeout=10, max_wait=None, **params):
    global _last_call, _blocked_until, _strikes, last_problem
    with _lock:
        now = time.monotonic()
        if now < _blocked_until:
            raise RateLimited()
        wait = _last_call + MIN_GAP - now
        if wait > 0:
            if max_wait is not None and wait > max_wait:
                raise RateLimited()
            time.sleep(wait)
        _last_call = time.monotonic()
    resp = requests.get(API + path, params={**params, "fmt": "json"},
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"}, timeout=timeout)
    if resp.status_code in (503, 429):
        _strikes += 1
        pause = min(MAX_BACKOFF, BACKOFF * 2 ** (_strikes - 1))
        _blocked_until = time.monotonic() + pause
        last_problem = f"HTTP {resp.status_code}, pausing {pause // 60} min: {resp.text[:160]!r}"
        raise RateLimited()
    _strikes = 0
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()


def _year(date):
    return int(date[:4]) if date and len(date) >= 4 and date[:4].isdigit() else None


def _facts(a):
    area = a.get("area") or {}
    begin_area = a.get("begin-area") or {}
    life = a.get("life-span") or {}
    return {
        "mbid": a.get("id"),
        "name": a.get("name"),
        "type": a.get("type"),                        # Person / Group / Orchestra / Choir / Character / Other
        "country": a.get("country"),                  # ISO code, e.g. GB (the area's country)
        "area": area.get("name"),                     # e.g. United Kingdom, or a city
        "begin_area": begin_area.get("name"),         # where they started, e.g. Manchester
        "begin_year": _year(life.get("begin")),
        "gender": a.get("gender"),
    }


def artist(mbid, **kw):
    """Facts for an artist by MusicBrainz ID, or None if it isn't valid / doesn't exist."""
    if not mbid or not MBID.match(mbid):              # only ever put a real ID into the URL
        return None
    data = _get("artist/" + mbid, **kw)
    return _facts(data) if data else None


def search_artist(name, min_score=95, **kw):
    """Best match for an artist name when we have no ID. Only accepts a very confident match
    (MusicBrainz scores 0-100), because "close enough" could be a different artist."""
    query = 'artist:"' + LUCENE_SPECIAL.sub(r"\\\1", name) + '"'
    data = _get("artist", query=query, limit=3, **kw)
    for a in (data or {}).get("artists") or []:
        if int(a.get("score", 0)) >= min_score:
            return _facts(a)
    return None
