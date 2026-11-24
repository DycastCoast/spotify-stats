"""
Import a Spotify Extended Streaming History export into the database.

Usage (the zip doesn't need extracting):
    python import_export.py data/my_spotify_data.zip --user <spotify-user-id>
    python import_export.py data/ethans_data.zip --user <ethans-id> --name Ethan

--user   The person's Spotify user ID. Find it on their profile: Share -> Copy link
         gives open.spotify.com/user/<THIS PART>. Leave it out if exactly one person
         has logged in on the site and the export is theirs.
--name   Display name, used if this person hasn't logged in on the site yet.
--include-private   Also import plays made in Spotify private sessions (skipped by default).

Safe to run again: plays already in the database are skipped, so re-importing
a newer export only adds what's new.
"""
import argparse
import json
import sys
import time
import zipfile
from pathlib import Path

from db import add_user_if_missing, all_user_ids, connect, init_db

# Data minimisation: these are the only fields we read. Everything else in the
# export (IP address, offline_timestamp, podcast and audiobook fields) is never stored.
INSERT_SQL = """
    INSERT OR IGNORE INTO plays (
        user_id, source, played_at, track_id, track_name, artist_name, album_name,
        ms_played, reason_start, reason_end, shuffle, skipped, offline,
        platform, conn_country, incognito
    ) VALUES (?, 'export', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def read_entries(path):
    """Yield every audio entry from a zip, a folder, or a single JSON file.

    This is a generator (it uses `yield`): it hands back one entry at a time
    instead of building one giant list, so memory stays low even for 10 years
    of history.
    """
    path = Path(path)

    def is_audio(name):
        name = Path(name).name
        return name.startswith("Streaming_History_Audio") and name.endswith(".json")

    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as z:
            for name in sorted(n for n in z.namelist() if is_audio(n)):
                with z.open(name) as f:
                    yield from json.load(f)
    elif path.is_dir():
        for file in sorted(p for p in path.rglob("*.json") if is_audio(p)):
            yield from json.loads(file.read_text(encoding="utf-8"))
    elif is_audio(path):
        yield from json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ValueError(f"Don't know how to read {path}. Give a .zip, a folder, or a Streaming_History_Audio .json file.")


def to_row(user_id, e):
    """Turn one export entry into a database row (a tuple matching INSERT_SQL)."""
    uri = e.get("spotify_track_uri") or ""
    return (
        user_id,
        e["ts"],                         # when the track ENDED, UTC, e.g. 2026-10-03T20:07:12Z
        uri.rsplit(":", 1)[-1],          # "spotify:track:abc123" -> "abc123", same as the API's IDs
        e.get("master_metadata_track_name"),
        e.get("master_metadata_album_artist_name"),
        e.get("master_metadata_album_album_name"),
        e.get("ms_played"),
        e.get("reason_start"),
        e.get("reason_end"),
        int(bool(e.get("shuffle"))),
        int(bool(e.get("skipped"))),
        int(bool(e.get("offline"))),
        e.get("platform"),
        e.get("conn_country"),
        int(bool(e.get("incognito_mode"))),
    )


def pick_user(arg_user):
    if arg_user:
        return arg_user
    users = all_user_ids()
    if len(users) == 1:
        return users[0]
    sys.exit("Please say whose export this is with --user <spotify-user-id>.")


def import_zip(path, user_id, include_private=False):
    """Import one export into one person's history. Returns counts for a summary.
    Used by the command line below and by the website's upload page (uploads.py)."""
    started = time.perf_counter()
    stats = {"entries": 0, "not_music": 0, "private": 0, "music": 0, "first": None, "last": None}

    def music_rows():
        """Filter and convert entries one at a time, keeping count as we go."""
        for e in read_entries(path):
            stats["entries"] += 1
            if not (e.get("spotify_track_uri") or "").startswith("spotify:track:"):
                stats["not_music"] += 1          # podcast, audiobook or local file
                continue
            if e.get("incognito_mode") and not include_private:
                stats["private"] += 1
                continue
            stats["music"] += 1
            stats["first"] = min(stats["first"] or e["ts"], e["ts"])
            stats["last"] = max(stats["last"] or e["ts"], e["ts"])
            yield to_row(user_id, e)

    # executemany pulls rows from the generator one by one, so the whole history
    # never sits in memory at once. It's all one transaction: much faster than
    # saving row by row, and if anything fails halfway, nothing is half-imported.
    with connect() as conn:
        before = conn.total_changes
        conn.executemany(INSERT_SQL, music_rows())
        new = conn.total_changes - before
    stats["new"] = new
    stats["seconds"] = time.perf_counter() - started
    return stats


def main():
    parser = argparse.ArgumentParser(description="Import a Spotify Extended Streaming History export.")
    parser.add_argument("path", help="the export .zip (or an extracted folder)")
    parser.add_argument("--user", help="Spotify user ID of the person this export belongs to")
    parser.add_argument("--name", help="display name, if they haven't logged in on the site")
    parser.add_argument("--include-private", action="store_true",
                        help="also import plays from Spotify private sessions")
    args = parser.parse_args()

    init_db()
    user_id = pick_user(args.user)
    add_user_if_missing(user_id, args.name)
    try:
        stats = import_zip(args.path, user_id, args.include_private)
    except ValueError as err:
        sys.exit(str(err))

    print(f"Imported for user: {user_id}")
    print(f"  entries read:         {stats['entries']:>9,}")
    print(f"  skipped (not music):  {stats['not_music']:>9,}")
    print(f"  skipped (private):    {stats['private']:>9,}")
    print(f"  new plays saved:      {stats['new']:>9,}   ({stats['music'] - stats['new']:,} already in the database)")
    if stats["first"]:
        print(f"  covers:               {stats['first'][:10]} -> {stats['last'][:10]}")
    print(f"  took {stats['seconds']:.1f}s")


if __name__ == "__main__":
    main()
