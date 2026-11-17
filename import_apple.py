"""
Import an Apple Music listening history (from privacy.apple.com) into the database.

How to get it: privacy.apple.com -> "Request a copy of your data" -> tick
"Apple Media Services information" -> wait for Apple's email (up to about a week) ->
download the zip(s). Don't unzip them.

Usage:
    python import_apple.py "Apple Media Services information.zip" --user <their Spotify ID> --name Sam
    python import_apple.py export.zip --inspect        # look inside first, change nothing

--user   Who it belongs to. The site logs people in through Spotify, so an Apple Music
         person still needs a (free) Spotify account to log in with; use that ID.
         Leave them out of the Spotify app's User Management until they want to log in.
--name   Display name, if they haven't logged in on the site yet.

How Apple's data fits the site:
- Apple's file has song, artist (in most versions), album, when, how long, and why it
  ended (skipped, finished...). No Spotify song IDs, so each song gets a made-up ID
  ("am-" + a fingerprint of its name and artist). Pages group songs by name + artist,
  so Overview, Journey, Right now, Skips and Compare all work, including comparing
  with Spotify users. No covers or album tracklists (those come from Spotify).
- Apple has changed this file's columns over the years and doesn't document them, so
  columns are found by name with fallbacks. If a real file doesn't fit, run --inspect
  and the fix is usually one name added to the lists below.
- Never stored: IP address, device ID, anything not in INSERT_SQL.

Safe to run again: plays already imported are skipped.
"""
import argparse
import csv
import hashlib
import io
import json
import sys
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import PurePosixPath

from db import add_user_if_missing, connect, init_db
from import_export import INSERT_SQL

# Possible column names for each thing we need (first one found wins; case doesn't matter).
COLUMNS = {
    "song": ["Song Name", "Track Name", "Content Name", "Title"],
    "artist": ["Artist Name", "Container Artist Name", "Artist"],
    "album": ["Album Name", "Container Album Name", "Album"],
    "end": ["Event End Timestamp", "Event Timestamp", "Play End Timestamp"],
    "start": ["Event Start Timestamp", "Play Start Timestamp"],
    "ms": ["Play Duration Milliseconds", "Play Duration In Milliseconds", "Played Duration Milliseconds"],
    "event": ["Event Type"],
    "media": ["Media Type"],
    "end_reason": ["End Reason Type", "End Reason"],
    "shuffle": ["Shuffle Play", "Shuffle"],
    "offline": ["Offline"],
    "country": ["IP Country Code", "Ip Country Code", "Country Code"],
}
# Apple's "why it ended" -> the Spotify words the rest of the site understands.
END_REASONS = {
    "TRACK_SKIPPED_FORWARDS": "fwdbtn",
    "TRACK_SKIPPED_BACKWARDS": "backbtn",
    "NATURAL_END_OF_TRACK": "trackdone",
    "PLAYBACK_MANUALLY_PAUSED": "endplay",
    "MANUALLY_SELECTED_PLAYBACK_OF_A_DIFF_ITEM": "clickrow",
}
MAX_NESTED_MB = 2000           # Apple wraps zips inside zips; refuse anything absurd


def song_id(name, artist):
    """A made-up song ID: the same song + artist always gets the same one."""
    return "am-" + hashlib.sha1(f"{name.lower()}|{artist}".encode()).hexdigest()[:20]


def walk(zf, prefix=""):
    """Every (path, opener) inside a zip, looking inside zips within it too."""
    for info in zf.infolist():
        name = info.filename
        if name.lower().endswith(".zip"):
            if info.file_size > MAX_NESTED_MB * 1_000_000:
                continue
            with zipfile.ZipFile(io.BytesIO(zf.read(info))) as inner:
                yield from walk(inner, prefix + name + "/")
        else:
            yield prefix + name, (lambda zf=zf, info=info: zf.open(info))


def find_files(path):
    """The files we can use: Play Activity CSVs (the plays), and the library list
    (only used to fill in artists if the plays file doesn't have them)."""
    found = {"plays": [], "library": [], "daily": []}
    with zipfile.ZipFile(path) as z:
        for name, opener in walk(z):
            base = PurePosixPath(name).name.lower()
            if base.endswith(".csv") and "play activity" in base:
                found["plays"].append((name, opener().read()))
            elif base.endswith(".json") and "library tracks" in base:
                found["library"].append((name, opener().read()))
            elif base.endswith(".csv") and "play history daily tracks" in base:
                found["daily"].append((name, opener().read()))
    return found


def rows_of(raw):
    return csv.DictReader(io.TextIOWrapper(io.BytesIO(raw), encoding="utf-8-sig", newline=""))


def pick(header):
    """Map each thing we need to the column Apple used for it (or None)."""
    lower = {h.lower().strip(): h for h in header or []}
    return {key: next((lower[c.lower()] for c in options if c.lower() in lower), None)
            for key, options in COLUMNS.items()}


def to_iso(value):
    """Apple's timestamps ('2021-03-04T18:22:01.123Z' or similar) -> '2021-03-04T18:22:01Z' (UTC)."""
    if not value:
        return None
    try:
        t = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def artist_lookup(found):
    """Fallback for files without an artist column: song (+ album) -> artist, from the
    library list and the daily history ('Artist - Song')."""
    by_song_album, by_song = {}, {}
    for _, raw in found["library"]:
        try:
            for t in json.loads(raw):
                title, artist = t.get("Title"), t.get("Artist") or t.get("Album Artist")
                if title and artist:
                    by_song_album[(title.lower(), (t.get("Album") or "").lower())] = artist
                    by_song.setdefault(title.lower(), Counter())[artist] += 1
        except (ValueError, AttributeError):
            pass
    for _, raw in found["daily"]:
        for r in rows_of(raw):
            desc = r.get("Track Description") or ""
            if " - " in desc:
                artist, title = desc.split(" - ", 1)
                by_song.setdefault(title.lower(), Counter())[artist] += 1
    return by_song_album, {k: c.most_common(1)[0][0] for k, c in by_song.items()}


def convert(found, user_id, counts):
    """Yield database rows (tuples matching INSERT_SQL) from the play activity files."""
    by_song_album, by_song = artist_lookup(found)
    for _, raw in found["plays"]:
        reader = rows_of(raw)
        col = pick(reader.fieldnames)
        get = lambda r, key: (r.get(col[key]) or "").strip() if col[key] else ""
        for r in reader:
            counts["rows"] += 1
            if col["event"] and get(r, "event") not in ("PLAY_END", ""):
                counts["not a play"] += 1                 # play starts, lyrics views, etc.
                continue
            if col["media"] and get(r, "media").upper() not in ("AUDIO", ""):
                counts["video"] += 1
                continue
            name = get(r, "song")
            try:
                ms = int(float(get(r, "ms") or 0))
            except ValueError:
                ms = 0
            if not name or ms <= 0:
                counts["no song or time"] += 1
                continue
            album = get(r, "album")
            artist = (get(r, "artist") or by_song_album.get((name.lower(), album.lower()))
                      or by_song.get(name.lower()))
            if not artist:
                counts["no artist"] += 1
                continue
            end = to_iso(get(r, "end"))
            if not end and get(r, "start"):
                start = to_iso(get(r, "start"))
                end = start and datetime.fromtimestamp(
                    datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp() + ms / 1000, timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
            if not end:
                counts["no time"] += 1
                continue
            flag = lambda v: None if v == "" else int(v.lower() in ("true", "1", "yes"))
            counts["music"] += 1
            yield (user_id, end, song_id(name, artist), name, artist, album or None, ms,
                   None, END_REASONS.get(get(r, "end_reason").upper()) if col["end_reason"] else None,
                   flag(get(r, "shuffle")), None, flag(get(r, "offline")),
                   "apple music", (get(r, "country") or None) and get(r, "country").upper()[:2], 0)


def import_apple(path, user_id, name=None):
    init_db()
    found = find_files(path)
    if not found["plays"]:
        raise ValueError("No 'Play Activity' CSV in that zip. It needs to be the 'Apple Media Services "
                         "information' export from privacy.apple.com.")
    add_user_if_missing(user_id, name)
    counts = Counter()
    started = time.time()
    with connect() as conn:
        before = conn.total_changes
        rows = list(convert(found, user_id, counts))
        conn.executemany(INSERT_SQL, rows)
        counts["new"] = conn.total_changes - before
    counts["seconds"] = round(time.time() - started, 1)
    if rows:
        times = sorted(r[1] for r in rows)
        counts["first"], counts["last"] = times[0][:10], times[-1][:10]
    return counts


def inspect(path):
    """Print what's inside (files, columns, one example row with the IP address hidden). Changes nothing."""
    found = find_files(path)
    for kind, files in found.items():
        for name, raw in files:
            print(f"\n[{kind}] {name}  ({len(raw) / 1e6:.1f} MB)")
            if name.lower().endswith(".csv"):
                reader = rows_of(raw)
                example = next(reader, {})
                print("  columns:", ", ".join(reader.fieldnames or []))
                print("  understood as:", {k: v for k, v in pick(reader.fieldnames).items() if v})
                print("  example:", {k: ("(hidden)" if "ip" in k.lower().split() or "identifier" in k.lower() else v)
                                     for k, v in example.items() if v})
    if not any(found.values()):
        print("Nothing recognised. Files in the zip:")
        with zipfile.ZipFile(path) as z:
            for name, _ in walk(z):
                print("  ", name)


def main():
    parser = argparse.ArgumentParser(description="Import an Apple Music listening history export.")
    parser.add_argument("path", help="the zip from privacy.apple.com")
    parser.add_argument("--user", help="their Spotify user ID (what they log in to the site with)")
    parser.add_argument("--name", help="display name, if they haven't logged in on the site yet")
    parser.add_argument("--inspect", action="store_true", help="show what's in the zip and stop")
    args = parser.parse_args()
    try:
        if args.inspect:
            inspect(args.path)
            return
        if not args.user:
            sys.exit("Say who it belongs to: --user <their Spotify user ID>")
        c = import_apple(args.path, args.user, args.name)
    except (ValueError, zipfile.BadZipFile, FileNotFoundError) as err:
        sys.exit(f"Not imported: {err}")
    print(f"Imported Apple Music history for: {args.user}")
    print(f"  rows read:              {c['rows']:,}")
    for label in ("not a play", "video", "no song or time", "no artist", "no time"):
        if c[label]:
            print(f"  skipped ({label}): {c[label]:>10,}")
    print(f"  new plays saved:        {c['new']:,}   ({c['music'] - c['new']:,} already in the database)")
    if c["first"]:
        print(f"  covers:                 {c['first']} -> {c['last']}")
    print(f"  took {c['seconds']}s")


if __name__ == "__main__":
    main()
