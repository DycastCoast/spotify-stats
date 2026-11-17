"""
Album covers and artist photos: looked up on Spotify once, then kept in the
database, so each image costs one API call ever (Spotify limits how many calls
a developer account can make).

Spotify removed its "fetch many at once" endpoints for apps like this one, so
it's one request per item: GET /tracks/{id}, and for a photo GET /artists/{id}.
"""
import albums
import theme
from db import connect


def get(kind, track_id):
    """The saved artwork for this item, or None if it hasn't been looked up yet."""
    with connect() as conn:
        row = conn.execute(
            "SELECT image_url, colour_hex, colour_rgb, colour_name FROM artwork WHERE kind = ? AND track_id = ?",
            (kind, track_id),
        ).fetchone()
    return dict(row) if row else None


def fetch(sp, kind, track_id):
    """Look the item up on Spotify, pick its colour, save it, and return it."""
    track = sp.track(track_id)
    albums.save_from_track(track)          # the album's ID and size come free with it
    if kind == "artist":
        images = sp.artist(track["artists"][0]["id"]).get("images") or []
    else:
        images = track["album"].get("images") or []

    # Spotify lists images largest first: show the middle size (~300 px), and
    # pick the colour from the smallest (64 px), which is quicker to download.
    image_url = images[len(images) // 2]["url"] if images else None
    colour = None
    if images:
        try:
            colour = theme.from_url(images[-1]["url"])
        except Exception:
            colour = None                      # a broken image shouldn't break the page

    saved = {
        "image_url": image_url,
        "colour_hex": colour and colour["hex"],
        "colour_rgb": colour and colour["rgb"],
        "colour_name": colour and colour["name"],
    }
    with connect() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO artwork (kind, track_id, image_url, colour_hex, colour_rgb, colour_name)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (kind, track_id, saved["image_url"], saved["colour_hex"], saved["colour_rgb"], saved["colour_name"]),
        )
    return saved


def save_from_track(track):
    """Keep the cover of a track object Spotify already sent us (e.g. to the poller),
    so it never has to be looked up separately. Costs no API call: only the small
    image is downloaded, from Spotify's image server, to pick its colour."""
    track_id = track.get("id")
    albums.save_from_track(track)          # free album info, whatever happens with the cover
    if not track_id or get("track", track_id) is not None:
        return False
    images = (track.get("album") or {}).get("images") or []
    if not images:
        return False
    try:
        colour = theme.from_url(images[-1]["url"])
    except Exception:
        colour = None
    with connect() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO artwork (kind, track_id, image_url, colour_hex, colour_rgb, colour_name)
               VALUES ('track', ?, ?, ?, ?, ?)""",
            (track_id, images[len(images) // 2]["url"],
             colour and colour["hex"], colour and colour["rgb"], colour and colour["name"]),
        )
    return True
