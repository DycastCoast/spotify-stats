"""
Importing your own Spotify history from the website (the "Import" page).

Safety, in the order it happens:
1. The upload must be a real zip containing Spotify's streaming-history files, within
   size limits. Checked from the zip's table of contents BEFORE anything is unpacked,
   so a "zip bomb" (a tiny file that unpacks to gigabytes) is refused up front.
2. Whose is it? Spotify's export files don't say. So:
   - if it matches another person's history already on the site, it's refused
     (that's exactly the mix-up that happened with the very first import);
   - if the site already tracked some of your plays live over the same dates,
     the export must contain most of them, or it's refused;
   - with nothing to compare against, you must tick "this is my own data".
3. Space: refused if the database would grow past the limit (the free plan has 512 MB).
4. Only ever imported into the logged-in person's own history. The upload is deleted after.
"""
import os
import zipfile
from datetime import datetime
from pathlib import Path

from db import DB_PATH, connect
from import_export import import_zip, read_entries

MAX_UPLOAD_MB = 100           # the zip itself (Ethan's 8 years: 10 MB; a 10-year history ~25 MB)
MAX_UNPACKED_MB = 1500        # all its history files unpacked
MAX_FILE_MB = 200             # any one file unpacked
MAX_RATIO = 100               # unpacked / packed: JSON packs about 10x; far more is a zip bomb
MAX_DB_MB = int(os.environ.get("MAX_DB_MB", 400))   # leave room for the code and packages in 512 MB
BYTES_PER_PLAY = 300          # roughly what one play adds to the database, indexes included
UPLOAD_DIR = Path(os.environ.get("UPLOAD_DIR") or Path(__file__).parent / "uploads")   # /tmp/uploads in the cluster


class Refused(Exception):
    """A friendly reason the upload can't be imported (shown on the page)."""


def is_audio(name):
    name = Path(name).name
    return name.startswith("Streaming_History_Audio") and name.endswith(".json")


def check_zip(path):
    """Step 1: is this a sensible Spotify export? Only the zip's table of contents is read."""
    if not zipfile.is_zipfile(path):
        raise Refused("That isn't a zip file. Upload the my_spotify_data.zip Spotify emailed you, without unzipping it.")
    with zipfile.ZipFile(path) as z:
        infos = z.infolist()
        if len(infos) > 1000:
            raise Refused("That zip has far more files in it than a Spotify export.")
        audio = [i for i in infos if is_audio(i.filename)]
        if not audio:
            names = " ".join(i.filename for i in infos)
            hint = (" It looks like the basic 'Account data' export, which only has the last year: "
                    "request 'Extended streaming history' instead." if "StreamingHistory_music" in names else "")
            raise Refused("No Streaming_History_Audio files in that zip, so it isn't an Extended streaming history export." + hint)
        total = 0
        for i in audio:
            if i.file_size > MAX_FILE_MB * 1_000_000:
                raise Refused("One of the files in that zip is far bigger than a Spotify export file.")
            if i.compress_size and i.file_size / i.compress_size > MAX_RATIO:
                raise Refused("That zip unpacks to far more than it should. It isn't a normal Spotify export.")
            total += i.file_size
        if total > MAX_UNPACKED_MB * 1_000_000:
            raise Refused("That export is far bigger than any listening history should be.")
    return len(audio)


def to_epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()


def survey(path):
    """First read-through: just what's needed for the checks (no saving yet)."""
    times, keys, music = [], set(), 0
    for e in read_entries(path):
        uri = e.get("spotify_track_uri") or ""
        if not uri.startswith("spotify:track:") or e.get("incognito_mode"):
            continue
        music += 1
        times.append(e["ts"])
        keys.add((uri.rsplit(":", 1)[-1], int(to_epoch(e["ts"]) // 60)))   # (track, minute it ended)
    if not times:
        raise Refused("That export doesn't contain any music plays.")
    times.sort()
    return {"music": music, "first": times[0], "last": times[-1], "times": times, "keys": keys}


def check_owner(user_id, info, people):
    """Step 2. Returns "matched" (it's clearly theirs), or "unknown" (nothing to compare
    with: needs the tick box). Raises Refused if it looks like someone else's."""
    times = info["times"]
    sample = times[:: max(1, len(times) // 300)][:300]          # ~300 plays spread over the whole export
    with connect() as conn:
        for other_id, other_name in people.items():
            if other_id == user_id:
                continue
            marks = ",".join("?" * len(sample))
            found = conn.execute(
                f"SELECT COUNT(*) FROM plays WHERE user_id = ? AND source = 'export' AND played_at IN ({marks})",
                [other_id, *sample]).fetchone()[0]
            if found >= len(sample) / 2:
                raise Refused(f"This looks like {other_name}'s history, which is already on the site, not yours. "
                              "Every Spotify export is called my_spotify_data.zip, so they're easy to mix up.")
        live = conn.execute(
            "SELECT track_id, played_at FROM plays WHERE user_id = ? AND source = 'api' AND played_at BETWEEN ? AND ?",
            (user_id, info["first"], info["last"][:19] + "Z")).fetchall()
    if len(live) < 20:
        return "unknown"
    keys = info["keys"]
    matched = 0
    for row in live:
        minute = int(to_epoch(row["played_at"]) // 60)
        if any((row["track_id"], minute + d) in keys for d in (-2, -1, 0, 1, 2)):
            matched += 1
    if matched / len(live) < 0.5:
        raise Refused(f"This doesn't look like your account's history: of {len(live)} plays the site tracked for you "
                      f"over the same dates, only {matched} are in it. Check it's your own export.")
    return "matched"


def check_space(info):
    """Step 3: would the database grow past the limit?"""
    now_mb = os.path.getsize(DB_PATH) / 1_000_000 if os.path.exists(DB_PATH) else 0
    extra_mb = info["music"] * BYTES_PER_PLAY / 1_000_000
    if now_mb + extra_mb > MAX_DB_MB:
        raise Refused(f"There isn't room: the database is {now_mb:.0f} MB and this would add about {extra_mb:.0f} MB, "
                      f"past the {MAX_DB_MB} MB limit. Ask whoever runs the site.")


def handle_upload(file_storage, user_id, people, confirmed):
    """The whole upload, start to finish. Returns a summary, or raises Refused.
    Returns {"need_confirm": True} if there was nothing to check ownership against."""
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    path = UPLOAD_DIR / f"{user_id}-{os.urandom(6).hex()}.zip"     # random name: never the uploader's file name
    try:
        file_storage.save(path)
        check_zip(path)
        try:
            info = survey(path)
        except (ValueError, KeyError, UnicodeDecodeError) as err:
            raise Refused("Couldn't read the history files in that zip. Is it a complete Spotify export?") from err
        owner = check_owner(user_id, info, people)
        if owner == "unknown" and not confirmed:
            return {"need_confirm": True, "first": info["first"][:10], "last": info["last"][:10], "music": info["music"]}
        check_space(info)
        result = import_zip(path, user_id)
        result["owner_check"] = owner
        return result
    finally:
        path.unlink(missing_ok=True)       # the upload is never kept


def my_history(user_id):
    """What's stored for this person, for the Import page."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT source, COUNT(*) AS n, MIN(played_at) AS first, MAX(played_at) AS last
               FROM plays WHERE user_id = ? GROUP BY source""", (user_id,)).fetchall()
    return {r["source"]: {"n": r["n"], "first": r["first"][:10], "last": r["last"][:10]} for r in rows}


def delete_export(user_id):
    """Remove everything this person imported from exports (their live-tracked plays stay)."""
    with connect() as conn:
        n = conn.execute("DELETE FROM plays WHERE user_id = ? AND source = 'export'", (user_id,)).rowcount
    from db import bump_data_version
    bump_data_version(user_id)              # so every page drops what it worked out from the old plays
    return n
