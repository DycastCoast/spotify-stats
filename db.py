"""
Database helpers: one SQLite file (stats.db) holding users and their plays.

Run directly to create the tables:  python db.py
"""
import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from spotipy.cache_handler import CacheHandler

# Where the database lives. Normally next to the code; in the cluster DATA_DIR points at the
# mounted storage (/data), because the container's own files are read-only there.
DATA_DIR = Path(os.environ.get("DATA_DIR") or Path(__file__).parent)
DB_PATH = DATA_DIR / "stats.db"

SCHEMA = """
-- One row per person who has logged in.
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,          -- Spotify user ID
    display_name  TEXT,
    token_info    TEXT,                      -- JSON with access + refresh token (never sent to the browser)
    created_at    TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now'))
);

-- One row per play, from either source.
CREATE TABLE IF NOT EXISTS plays (
    id           INTEGER PRIMARY KEY,
    user_id      TEXT NOT NULL REFERENCES users(id),
    source       TEXT NOT NULL CHECK (source IN ('api', 'export')),
    played_at    TEXT NOT NULL,              -- UTC, ISO 8601, e.g. 2026-11-13T21:40:12.345Z
    track_id     TEXT,                       -- Spotify track ID (NULL for local files)
    track_name   TEXT,
    artist_id    TEXT,                       -- only the API gives this; the export doesn't
    artist_name  TEXT,
    album_name   TEXT,
    duration_ms  INTEGER,                    -- full track length (API only)
    ms_played    INTEGER,                    -- how long you actually listened (export only)

    -- Export-only details (NULL for poller rows). Keep this list in sync with NEW_COLUMNS below.
    reason_start TEXT,                       -- e.g. clickrow (chosen), trackdone (autoplay), fwdbtn
    reason_end   TEXT,                       -- e.g. trackdone (finished), fwdbtn (skipped)
    shuffle      INTEGER,                    -- 1 = shuffle on
    skipped      INTEGER,                    -- 1 = Spotify marked it as skipped
    offline      INTEGER,                    -- 1 = played offline
    platform     TEXT,                       -- device, e.g. android / windows
    conn_country TEXT,                       -- country code, e.g. GB
    incognito    INTEGER,                    -- 1 = played in a private session

    -- The duplicate guard. Source is included on purpose: if the export and the
    -- poller both saw the same play, we keep BOTH rows for the accuracy check.
    UNIQUE (user_id, source, track_id, played_at)
);

CREATE INDEX IF NOT EXISTS idx_plays_user_time ON plays (user_id, played_at);

-- Cover art and artist photos, fetched from Spotify once and kept, plus the page
-- colour picked from each. kind = 'track' (album cover) or 'artist' (photo).
-- Albums' real tracklists, looked up on Spotify once (when an album page is first
-- opened) and kept. Plays only say an album's NAME, not its track order or lengths.
-- Keyed the way plays name it: lower-case album name + the play's artist.
CREATE TABLE IF NOT EXISTS albums (
    name_key     TEXT NOT NULL,              -- lower-case album name
    artist       TEXT NOT NULL,
    name         TEXT,                       -- as Spotify spells it
    album_id     TEXT,
    total_tracks INTEGER,
    release_date TEXT,
    image_url    TEXT,
    tracks       TEXT,                       -- JSON list, NULL until the tracklist has been looked up
    fetched_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (name_key, artist)
);

CREATE TABLE IF NOT EXISTS artwork (
    kind         TEXT NOT NULL,
    track_id     TEXT NOT NULL,              -- the track it was looked up through
    image_url    TEXT,                       -- NULL = Spotify had no image (so don't ask again)
    colour_hex   TEXT,                       -- NULL = no usable colour (greyscale cover)
    colour_rgb   TEXT,
    colour_name  TEXT,
    fetched_at   TEXT DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    PRIMARY KEY (kind, track_id)
);

"""

# Views are saved queries: they hold no data of their own, so they can be dropped
# and recreated freely. init_db() does that every time, so changing a view here
# updates existing databases automatically.
VIEWS = """
DROP VIEW IF EXISTS plays_best;
DROP VIEW IF EXISTS plays_listened;

-- Listening time for every play, worked out at query time rather than stored.
-- Export rows already know ms_played. Poller (api) rows are estimated:
--   time since the previous play, capped at the track's length.
-- (Verified with real data: the API's played_at is when a track ENDED.)
-- The two halves are glued with UNION ALL so the slow estimating part only
-- runs on poller rows, not on hundreds of thousands of export rows.
CREATE VIEW plays_listened AS
SELECT p.*, p.ms_played AS listened_ms
FROM plays p
WHERE p.source = 'export'
UNION ALL
SELECT p.*,
    COALESCE(
        MIN(
            p.duration_ms,
            CAST(ROUND((julianday(p.played_at) - julianday(
                LAG(p.played_at) OVER (PARTITION BY p.user_id ORDER BY p.played_at)
            )) * 86400000) AS INTEGER)
        ),
        p.duration_ms   -- very first play we know of: no previous play, assume full length
    ) AS listened_ms
FROM plays p
WHERE p.source = 'api';

-- The plays to use for stats, with no double counting. Where someone's export and
-- the poller overlap, the export wins (it's exact); poller rows only count after
-- the export's last play. The accuracy check uses the full plays table instead.
CREATE VIEW plays_best AS
WITH export_end AS (                       -- each person's last exported play, worked out once
    SELECT user_id, MAX(played_at) AS last_export
    FROM plays WHERE source = 'export' GROUP BY user_id
)
SELECT pl.*
FROM plays_listened pl
LEFT JOIN export_end x ON x.user_id = pl.user_id
WHERE pl.source = 'export'
   OR pl.played_at > COALESCE(x.last_export, '');   -- '' = no export: keep all poller rows
"""



@contextmanager
def connect():
    """Open the database for a `with` block.

    Saves (commits) changes if the block finishes cleanly, and always closes the
    connection afterwards, even if something crashes halfway through.
    Rows come back like dicts: row["track_name"].
    """
    # timeout: if another thread is saving (the poller, an import), wait up to 30 s for it
    # instead of failing straight away with "database is locked".
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Columns added after the first version of the plays table. CREATE TABLE IF NOT EXISTS
# skips tables that already exist, so an existing stats.db needs these added separately.
NEW_COLUMNS = {
    "reason_start": "TEXT",
    "reason_end": "TEXT",
    "shuffle": "INTEGER",
    "skipped": "INTEGER",
    "offline": "INTEGER",
    "platform": "TEXT",
    "conn_country": "TEXT",
    "incognito": "INTEGER",
}


def migrate(conn):
    """Bring an older database up to date without losing any data.

    This is a tiny "migration": compare the columns the table has with the
    columns the code expects, and add whatever is missing.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(plays)")}
    for column, col_type in NEW_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE plays ADD COLUMN {column} {col_type}")
    # Trips together is opt-in: 0 = off (the default for everyone), 1 = this person agreed.
    user_columns = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "share_trips" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN share_trips INTEGER NOT NULL DEFAULT 0")
    # "What I'm playing right now" is opt-in too: 0 = only you see it, 1 = friends can.
    if "share_playing" not in user_columns:
        conn.execute("ALTER TABLE users ADD COLUMN share_playing INTEGER NOT NULL DEFAULT 0")


def shares_trips(user_id):
    with connect() as conn:
        row = conn.execute("SELECT share_trips FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["share_trips"])


def set_share_trips(user_id, on):
    with connect() as conn:
        conn.execute("UPDATE users SET share_trips = ? WHERE id = ?", (1 if on else 0, user_id))


def shares_playing(user_id):
    with connect() as conn:
        row = conn.execute("SELECT share_playing FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["share_playing"])


def set_share_playing(user_id, on):
    with connect() as conn:
        conn.execute("UPDATE users SET share_playing = ? WHERE id = ?", (1 if on else 0, user_id))


def init_db():
    with connect() as conn:
        conn.executescript(SCHEMA)
        migrate(conn)
        conn.executescript(VIEWS)


def save_user(user_id, display_name, token_info):
    """Insert a new user, or update their name/token if they already exist (an "upsert")."""
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO users (id, display_name, token_info) VALUES (?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                display_name = excluded.display_name,
                token_info   = excluded.token_info
            """,
            (user_id, display_name, json.dumps(token_info)),
        )


def add_user_if_missing(user_id, display_name=None):
    """Create a user with no login yet (e.g. a friend whose export you import first).

    If they log in on the website later, save_user() fills in their token on this
    same row, so their imported history and new plays end up together.
    """
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO users (id, display_name) VALUES (?, ?)",
            (user_id, display_name or user_id),
        )


def all_user_ids():
    """Users the poller can fetch for: only people who have logged in (have a token)."""
    with connect() as conn:
        return [row["id"] for row in conn.execute(
            "SELECT id FROM users WHERE token_info IS NOT NULL"
        )]


class DatabaseCacheHandler(CacheHandler):
    """Tells Spotipy to keep a user's token in our database instead of a file or cookie.

    Spotipy calls these two methods itself: get_cached_token() before each request,
    and save_token_to_cache() whenever it refreshes an expired token.
    """

    def __init__(self, user_id):
        self.user_id = user_id

    def get_cached_token(self):
        with connect() as conn:
            row = conn.execute(
                "SELECT token_info FROM users WHERE id = ?", (self.user_id,)
            ).fetchone()
        return json.loads(row["token_info"]) if row and row["token_info"] else None

    def save_token_to_cache(self, token_info):
        with connect() as conn:
            conn.execute(
                "UPDATE users SET token_info = ? WHERE id = ?",
                (json.dumps(token_info), self.user_id),
            )


if __name__ == "__main__":
    # python db.py  ->  creates the tables if needed, then prints a quick summary.
    init_db()
    print(f"Database: {DB_PATH}")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT u.display_name, p.source, COUNT(*) AS plays,
                   MIN(p.played_at) AS first, MAX(p.played_at) AS last
            FROM plays p JOIN users u ON u.id = p.user_id
            GROUP BY u.id, p.source
            """
        ).fetchall()
    if not rows:
        print("No plays saved yet.")
    for r in rows:
        print(f"  {r['display_name']} ({r['source']}): {r['plays']} plays, {r['first']} -> {r['last']}")
