"""
"Listening right now": the song someone is playing this second, straight from Spotify,
plus what their history says about it.

Unlike everything else on the site this isn't from the database: the browser asks
/api/playing every ~20 seconds while a page is open, and this asks Spotify. Answers are
kept for CACHE_S seconds, so several open tabs (or the top-bar pill and the Right now
card on the same page) share one Spotify request.

Privacy: you always see your own. Friends see it only if you switch sharing on
(users.share_playing), the same idea as trips together.
"""
import json
import time
from functools import lru_cache

import spotipy

import albums
import entity
import stats
from db import DatabaseCacheHandler, connect
from spotify_client import SCOPES, make_client, pause_after, paused

CACHE_S = 15
_cache = {}                     # user ID -> (when asked, answer)
NEEDED = "user-read-currently-playing"


def has_permission(user_id):
    """Did this person log in AFTER the 'currently playing' permission was added?
    Checked from their saved token, so people who haven't re-logged in cost no request."""
    with connect() as conn:
        row = conn.execute("SELECT token_info FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["token_info"]:
        return None                     # never logged in (history imported only)
    return NEEDED in (json.loads(row["token_info"]).get("scope") or "").split()


def playing(user_id):
    """What they're playing, as a small dict with a "state":
    playing / paused (with the song), idle (nothing on), other (a podcast or ad),
    relogin (needs to log in again), none (never logged in), unknown (Spotify didn't answer)."""
    hit = _cache.get(user_id)
    if hit and time.time() - hit[0] < CACHE_S:
        answer = dict(hit[1])
        if answer.get("state") == "playing":        # move the progress on by the time since we asked
            answer["progress_ms"] = min(answer["duration_ms"], answer["progress_ms"] + int((time.time() - hit[0]) * 1000))
        return answer
    answer = _ask(user_id)
    _cache[user_id] = (time.time(), answer)
    return answer


def _ask(user_id):
    allowed = has_permission(user_id)
    if allowed is None:
        return {"state": "none"}
    if not allowed:
        return {"state": "relogin"}
    if paused():                        # Spotify told us to wait (a 429): don't knock
        return {"state": "unknown"}
    try:
        current = make_client(DatabaseCacheHandler(user_id)).current_user_playing_track()
    except spotipy.SpotifyException as err:
        pause_after(err)
        return {"state": "relogin" if err.http_status in (401, 403) else "unknown"}
    except Exception:                   # e.g. their login expired and couldn't be refreshed, or a timeout
        return {"state": "unknown"}
    if not current:
        return {"state": "idle"}        # Spotify answers "204 No Content": nothing playing
    if current.get("currently_playing_type") not in (None, "track"):
        return {"state": "other", "kind": current.get("currently_playing_type")}    # podcast or ad (no song details)
    if not current.get("item"):
        return {"state": "idle"}
    item = current["item"]
    try:
        import artwork
        artwork.save_from_track(item)   # free cover + album info, no extra request
    except Exception:
        pass
    images = (item.get("album") or {}).get("images") or []
    artists = item.get("artists") or [{}]
    return {
        "state": "playing" if current.get("is_playing") else "paused",
        "track_id": item.get("id"),
        "song": item.get("name"),
        "artist": artists[0].get("name"),
        "artists": ", ".join(a.get("name", "") for a in artists),
        "album": (item.get("album") or {}).get("name"),
        "image": images[len(images) // 2]["url"] if images else None,
        "progress_ms": current.get("progress_ms") or 0,
        "duration_ms": item.get("duration_ms") or 0,
        "local": item.get("is_local", False),
    }


def find(tl, track_id, name, artist):
    """The song's place in someone's history: by Spotify ID, else by name + artist."""
    t = tl.index_of.get(track_id)
    if t is None:
        t = tl.song_of.get(((name or "").lower(), artist))
    return t


def about(user_id, track_id, name, artist, album):
    """What this person's history says about the song (worked out once per song + new plays)."""
    return _about(user_id, track_id, name, artist, album, stats.latest_play_id(user_id))


@lru_cache(maxsize=32)
def _about(user_id, track_id, name, artist, album, _latest):
    tl = entity.timeline(user_id)
    t = find(tl, track_id, name, artist)
    facts = {"known": False, "track": track_id}
    if t is not None and tl.track_ms[t]:
        plays = [i for i in tl.by_track[t] if tl.listened(i)]
        skips = [tl.skip[i] for i in tl.by_track[t] if tl.skip[i] >= 0]
        newest = tl.end[len(tl.end) - 1] if len(tl.end) else time.time()
        recent = sum(1 for i in plays if tl.end[i] > newest - 14 * 86400)
        facts.update({
            "known": True, "track": tl.track_ids[t],
            "plays": len(plays), "rank": tl.track_rank.get(t), "of": len(tl.track_rank),
            "hours": round(tl.track_ms[t] / 3_600_000, 1),
            "first": entity.fmt_date(tl.local(plays[0])) if plays else None,
            "recent": recent,
            "skip_rate": round(100 * sum(skips) / len(skips)) if len(skips) >= 5 else None,
        })
    # The artist and album, so even a brand-new song gets some context.
    if artist in tl.artist_rank:
        facts["artist"] = {"rank": tl.artist_rank[artist], "hours": round(tl.artist_ms[artist] / 3_600_000)}
    a = tl.album_of.get(((album or "").lower(), artist))
    if a is not None and a in tl.album_rank:
        facts["album"] = {"rank": tl.album_rank[a]}
    # Friends who've played it (play counts only: everyone's stats pages are visible on the site anyway).
    friends = []
    for p in stats.people():
        if p["id"] == user_id:
            continue
        ftl = entity.timeline(p["id"])
        ft = find(ftl, track_id, name, artist)
        if ft is not None and ftl.track_ms[ft]:
            friends.append({"id": p["id"], "name": p["display_name"] or p["id"],
                            "plays": sum(1 for i in ftl.by_track[ft] if ftl.listened(i)),
                            "track": ftl.track_ids[ft]})
    facts["friends"] = sorted(friends, key=lambda f: -f["plays"])
    return facts
