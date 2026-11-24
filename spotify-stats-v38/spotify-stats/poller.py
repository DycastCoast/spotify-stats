"""
The poller: saves everyone's recently played tracks into the database.

Run it by hand:   python poller.py
Later, a timer (Task Scheduler, then the Raspberry Pi) runs it every 30 minutes.

Safe to run as often as you like: plays we've already saved are skipped
by the UNIQUE rule in the plays table (INSERT OR IGNORE).
"""
import logging
from pathlib import Path

import spotipy

import artwork
from db import DatabaseCacheHandler, all_user_ids, connect, init_db
from spotify_client import make_client

# When a timer runs this script there's no window to print to, so every message
# also goes to poller.log next to this file. Open it to check the poller is working.
log = logging.getLogger("poller")


def poll_user(user_id):
    """Fetch one user's last 50 plays and save the new ones. Returns how many were new."""
    # auth_manager lets Spotipy refresh expired tokens by itself; the
    # DatabaseCacheHandler saves the refreshed token back to our database.
    sp = make_client(DatabaseCacheHandler(user_id))
    items = sp.current_user_recently_played(limit=50)["items"]

    rows = []
    for item in items:
        track = item["track"]
        main_artist = track["artists"][0] if track["artists"] else {}
        rows.append((
            user_id,
            "api",
            item["played_at"],
            track.get("id"),
            track.get("name"),
            main_artist.get("id"),
            main_artist.get("name"),
            track.get("album", {}).get("name"),
            track.get("duration_ms"),
        ))

    # Spotify sent each song's cover along with the play: keep it (no extra API call).
    for item in items:
        try:
            artwork.save_from_track(item["track"])
        except Exception:
            pass                                    # a cover is a nice-to-have; never stop the poller

    with connect() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO plays
                (user_id, source, played_at, track_id, track_name,
                 artist_id, artist_name, album_name, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return conn.total_changes - before


def poll_all():
    """Poll everyone who has logged in. Returns {user: new plays, or "failed"}.
    Used by main() below (the timer on your PC) and by the live site's /internal/poll."""
    init_db()
    results = {}
    for user_id in all_user_ids():
        # One user failing (e.g. they revoked access) shouldn't stop the others.
        try:
            results[user_id] = poll_user(user_id)
            log.info("%s: %d new plays saved", user_id, results[user_id])
        except Exception:
            log.exception("%s: FAILED", user_id)  # also records the full error details
            results[user_id] = "failed"
    if not results:
        log.warning("No users yet. Log in on the website first.")
    return results


def main():
    poll_all()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(Path(__file__).parent / "poller.log", encoding="utf-8"),
            logging.StreamHandler(),  # still prints to the terminal when you run it by hand
        ],
    )
    main()
