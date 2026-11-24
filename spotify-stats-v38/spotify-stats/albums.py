"""
Albums' real tracklists (order, song lengths, how many songs), which plays don't include.

Looked up on Spotify ONCE per album, the first time someone opens its page, and saved.
Album info Spotify sends anyway (with recent plays and cover lookups) is saved for free,
so many albums already know their Spotify ID and size before anyone opens them.

Requests per album, at most: 1 to find the album's ID through one of its songs (skipped if
already known), 1 for the album itself. Never in bursts; and if Spotify ever says "slow
down", nothing is asked until it says so (spotify_client.paused), and pages use estimates.
"""
import json

from db import connect
from spotify_client import pause_after, paused


def get(name, artist):
    """What's saved about this album (or None). 'tracks' is a list once looked up."""
    with connect() as conn:
        row = conn.execute("SELECT * FROM albums WHERE name_key = ? AND artist = ?",
                           ((name or "").lower(), artist)).fetchone()
    if not row:
        return None
    saved = dict(row)
    saved["tracks"] = json.loads(saved["tracks"]) if saved["tracks"] else None
    return saved


def save_from_track(track):
    """Keep the album info inside a track object Spotify already sent us. No API call."""
    album = (track or {}).get("album") or {}
    artists = track.get("artists") or []
    if not album.get("id") or not album.get("name") or not artists:
        return
    images = album.get("images") or []
    with connect() as conn:
        conn.execute(
            """INSERT INTO albums (name_key, artist, name, album_id, total_tracks, release_date, image_url)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (name_key, artist) DO UPDATE SET
                   album_id = COALESCE(albums.album_id, excluded.album_id),
                   total_tracks = COALESCE(albums.total_tracks, excluded.total_tracks),
                   release_date = COALESCE(albums.release_date, excluded.release_date),
                   image_url = COALESCE(albums.image_url, excluded.image_url)""",
            (album["name"].lower(), artists[0]["name"], album["name"], album["id"], album.get("total_tracks"),
             album.get("release_date"), images[len(images) // 2]["url"] if images else None))


def lookup(sp, name, artist):
    """Make sure the album's tracklist is saved; returns what's saved (tracks may still be None
    if Spotify couldn't be asked). Called from the album page, never in a loop."""
    saved = get(name, artist)
    if saved and saved["tracks"] is not None:
        return saved
    if paused() or sp is None:
        return saved
    try:
        album_id = saved and saved["album_id"]
        if not album_id:
            # Find the album's Spotify ID through one of its songs that someone played.
            with connect() as conn:
                row = conn.execute(
                    """SELECT track_id FROM plays WHERE lower(album_name) = ? AND artist_name = ?
                       AND track_id IS NOT NULL AND track_id NOT LIKE 'am-%' LIMIT 1""", ((name or "").lower(), artist)).fetchone()
            if not row:
                return saved
            track = sp.track(row["track_id"])
            save_from_track(track)
            album_id = track["album"]["id"]
        album = sp.album(album_id)
    except Exception as err:                 # rate limit, offline, removed album: use estimates
        pause_after(err)
        return get(name, artist)
    items = (album.get("tracks") or {}).get("items") or []      # the first 50: plenty for nearly every album
    tracks = [{"n": t["track_number"], "disc": t.get("disc_number", 1), "name": t["name"],
               "ms": t.get("duration_ms"), "id": t.get("id")} for t in items]
    tracks.sort(key=lambda t: (t["disc"], t["n"]))
    images = album.get("images") or []
    with connect() as conn:
        conn.execute(
            """INSERT INTO albums (name_key, artist, name, album_id, total_tracks, release_date, image_url, tracks)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (name_key, artist) DO UPDATE SET name = excluded.name, album_id = excluded.album_id,
                   total_tracks = excluded.total_tracks, release_date = excluded.release_date,
                   image_url = COALESCE(excluded.image_url, albums.image_url), tracks = excluded.tracks""",
            ((name or "").lower(), artist, album.get("name"), album_id, album.get("total_tracks"),
             album.get("release_date"), images[len(images) // 2]["url"] if images else None, json.dumps(tracks)))
    return get(name, artist)
