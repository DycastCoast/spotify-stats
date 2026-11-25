"""
Last.fm: how many people listen to an artist, worldwide. Crate Dive uses this for rarity
(Spotify stopped giving small apps its popularity numbers in Feb 2026).

Last.fm asks apps not to make "several calls per second" continuously, so every call here
goes through one gate that keeps at least MIN_GAP seconds between requests, from every
thread in the site. If Last.fm says we're going too fast (error 29 / HTTP 429), the gate
closes for BACKOFF seconds and callers get RateLimited straight away instead of waiting.

The API key comes from the LASTFM_API_KEY environment variable (the sealed secret on the
cluster, .env on your PC). It's never put in a page or a log.
"""
import os
import threading
import time

import requests

API = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "dycast-stats/1.0 (+https://dycast-stats.montlab.cc)"
MIN_GAP = 2.5              # seconds between any two calls (well under Last.fm's guidance)
BACKOFF = 15 * 60          # after a rate-limit warning, stay quiet this long

_lock = threading.Lock()
_last_call = 0.0
_blocked_until = 0.0


class LastfmError(Exception):
    """Last.fm answered, but with an error (they often send errors inside a normal 200 reply)."""
    def __init__(self, code, message):
        super().__init__(f"Last.fm error {code}: {message}")
        self.code = code


class RateLimited(Exception):
    """We were told to slow down (or are still in the quiet period after that)."""


class Unavailable(Exception):
    """No API key set, so Last.fm can't be used at all."""


def available():
    return bool(os.environ.get("LASTFM_API_KEY"))


def _call(method, timeout=10, max_wait=None, **params):
    """One request to Last.fm through the shared gate. Returns the decoded JSON.

    max_wait: the longest this caller is willing to queue for its turn (the live answer
    check uses a short one, so a player never waits behind the background job for long).
    """
    global _last_call, _blocked_until
    key = os.environ.get("LASTFM_API_KEY")
    if not key:
        raise Unavailable()
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
    query = {**params, "method": method, "api_key": key, "format": "json"}
    resp = requests.get(API, params=query, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    if resp.status_code == 429:
        _blocked_until = time.monotonic() + BACKOFF
        raise RateLimited()
    try:
        data = resp.json()
    except ValueError:
        raise LastfmError(resp.status_code, "reply wasn't JSON")
    if isinstance(data, dict) and "error" in data:
        if data["error"] == 29:
            _blocked_until = time.monotonic() + BACKOFF
            raise RateLimited()
        raise LastfmError(data["error"], data.get("message", ""))
    return data


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def artist_info(name, **kw):
    """Listeners, plays, top tags and MusicBrainz ID for one artist, or None if Last.fm
    doesn't know them. autocorrect=1 lets Last.fm fix small spelling differences."""
    try:
        data = _call("artist.getInfo", artist=name, autocorrect=1, **kw)
    except LastfmError as e:
        if e.code == 6:                    # "The artist you supplied could not be found"
            return None
        raise
    a = data.get("artist") or {}
    stats = a.get("stats") or {}
    tags = (a.get("tags") or {}).get("tag") or []
    if isinstance(tags, dict):             # one tag comes back as an object, not a list
        tags = [tags]
    return {
        "name": a.get("name") or name,
        "mbid": a.get("mbid") or None,
        "listeners": _int(stats.get("listeners")),
        "plays": _int(stats.get("playcount")),
        "tags": [t.get("name", "").lower() for t in tags if t.get("name")][:6],
    }


def _artist_list(data, key):
    items = (data.get(key) or {}).get("artist") or []
    if isinstance(items, dict):
        items = [items]
    return [{"name": a.get("name"), "mbid": a.get("mbid") or None,
             "listeners": _int(a.get("listeners")), "plays": _int(a.get("playcount"))}
            for a in items if a.get("name")]


def chart_top_artists(page=1, limit=100):
    """Last.fm's global top artists (with listener counts included, so no extra calls)."""
    return _artist_list(_call("chart.getTopArtists", page=page, limit=limit), "artists")


def tag_top_artists(tag, page=1, limit=100):
    """Top artists for a genre tag, e.g. "grime". Listener counts aren't included here."""
    return _artist_list(_call("tag.getTopArtists", tag=tag, page=page, limit=limit), "topartists")
