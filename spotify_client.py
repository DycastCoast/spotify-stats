"""
Spotify login settings shared by the website (app.py) and the poller (poller.py).
Keeping them in one place means the two can never drift apart.
"""
import os

import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth

load_dotenv()

# Least privilege: read-only access to top items, recently played, and the song playing
# right now, plus creating PRIVATE playlists (Playlists page: "Save to Spotify"). Nothing else.
# (Adding a permission here means everyone logs in once more.)
SCOPES = "user-top-read user-read-recently-played user-read-currently-playing playlist-modify-private"


def make_oauth(cache_handler, state=None):
    return SpotifyOAuth(
        client_id=os.environ["SPOTIFY_CLIENT_ID"],
        client_secret=os.environ["SPOTIFY_CLIENT_SECRET"],
        redirect_uri=os.environ["SPOTIFY_REDIRECT_URI"],
        scope=SCOPES,
        state=state,
        cache_handler=cache_handler,
        show_dialog=True,  # always show Spotify's consent screen (handy when testing accounts)
    )


def make_client(cache_handler):
    """A Spotify client that FAILS FAST when Spotify says "too many requests" (HTTP 429).

    By default spotipy obeys Spotify's "Retry-After" and quietly sleeps, which after a big
    burst of requests can be hours: the website, poller or snapshot would just hang.
    status_retries=0 turns that into an error we can catch and handle instead.
    """
    return spotipy.Spotify(auth_manager=make_oauth(cache_handler), status_retries=0, requests_timeout=15)


def retry_after(err):
    """How many seconds Spotify asked us to wait, from a 429 error (None if it didn't say)."""
    headers = getattr(err, "headers", None) or {}
    try:
        return int(headers.get("Retry-After") or headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


# When Spotify says "too many requests", EVERYTHING that would ask it something extra
# (covers, album tracklists) waits until this time. One shared clock, so different parts
# of the site can't each keep knocking.
_paused_until = 0.0


def paused():
    import time
    return time.time() < _paused_until


def pause_after(err):
    """Call with a SpotifyException: if it was a 429, stop extra requests for as long as Spotify asked."""
    global _paused_until
    import time
    if getattr(err, "http_status", None) == 429:
        _paused_until = time.time() + (retry_after(err) or 600)
