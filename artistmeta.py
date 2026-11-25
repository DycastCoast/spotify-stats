"""
The artist pool for Crate Dive: every artist anyone has played, plus Last.fm's chart and
genre top artists, each with:
  - Last.fm listeners worldwide (for rarity)
  - MusicBrainz facts: where from, start year, person or group (for prompts like
    "an artist from Manchester" or "a band formed in the 1990s")

Filled slowly by a background thread (see run_forever), one artist at a time, because both
services ask for polite request rates. Everything is saved, so each artist costs about two
calls ever (plus a refresh of the listener count every couple of months). If the site
restarts, it carries on where it left off.

Status for the site owner: /trivia/dive/status
"""
import json
import logging
import os
import threading
import time

import lastfm
import musicbrainz
from db import connect

log = logging.getLogger("artistmeta")

SQL = """
CREATE TABLE IF NOT EXISTS artist_meta (
    name          TEXT PRIMARY KEY,       -- as plays / Last.fm spell it
    source        TEXT,                   -- why it's in the pool: plays / chart / tag:<genre>
    lf_name       TEXT,                   -- Last.fm's spelling after autocorrect
    listeners     INTEGER,                -- Last.fm listeners worldwide
    lf_plays      INTEGER,
    tags          TEXT,                   -- JSON list of Last.fm's top tags, lower case
    mbid          TEXT,                   -- MusicBrainz ID
    lf_status     TEXT,                   -- NULL = not asked yet, ok, missing, error
    lf_at         REAL,
    mb_type       TEXT,                   -- Person / Group / ...
    mb_country    TEXT,                   -- ISO code, e.g. GB
    mb_area       TEXT,
    mb_begin_area TEXT,                   -- e.g. Manchester
    mb_begin_year INTEGER,
    mb_gender     TEXT,
    mb_status     TEXT,                   -- NULL = not asked yet, ok, missing, error
    mb_at         REAL,
    added_at      REAL
);
CREATE INDEX IF NOT EXISTS idx_artist_meta_lf ON artist_meta (lf_status);
CREATE INDEX IF NOT EXISTS idx_artist_meta_mb ON artist_meta (mb_status);
CREATE TABLE IF NOT EXISTS artist_meta_seeds (key TEXT PRIMARY KEY, done_at REAL);
"""

# Genre tags whose top artists widen the pool beyond what the group already plays.
SEED_TAGS = ["rock", "pop", "hip-hop", "rap", "indie", "electronic", "metal", "punk", "jazz", "soul",
             "rnb", "folk", "country", "k-pop", "house", "grime", "uk hip hop", "drum and bass",
             "britpop", "shoegaze", "emo", "reggae", "afrobeats", "latin", "techno", "trap",
             "singer-songwriter", "alternative", "classical", "disco"]
CHART_PAGES = 10                 # 10 x 100 = Last.fm's top 1,000 artists
TAG_PAGES = 2                    # 200 per genre
RETRY_ERRORS_AFTER = 24 * 3600   # a failed lookup is tried again a day later
REFRESH_AFTER = 60 * 24 * 3600   # listener counts are refreshed every ~2 months
SKIP = {"", "various artists", "unknown artist", "[unknown]"}


def setup():
    with connect() as conn:
        conn.executescript(SQL)


# ------------------------------------------------------------------ filling the pool
def add_from_plays():
    """Every artist anyone has played joins the pool (once). Cheap: no outside calls."""
    with connect() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO artist_meta (name, source, added_at)
               SELECT DISTINCT artist_name, 'plays', ? FROM plays
               WHERE artist_name IS NOT NULL AND TRIM(artist_name) != ''""", (time.time(),))
        return cur.rowcount


def _add_listed(artists, source):
    """Artists from a Last.fm list. Chart lists already include listener counts, so those
    are saved straight away (no extra call needed later)."""
    now = time.time()
    added = 0
    with connect() as conn:
        for a in artists:
            if a["name"].strip().lower() in SKIP:
                continue
            has_count = a.get("listeners") is not None
            cur = conn.execute(
                """INSERT OR IGNORE INTO artist_meta (name, source, lf_name, listeners, lf_plays, mbid, lf_status, lf_at, added_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (a["name"], source, a["name"] if has_count else None, a.get("listeners"), a.get("plays"),
                 a.get("mbid"), "ok" if has_count else None, now if has_count else None, now))
            added += cur.rowcount
            if not cur.rowcount and has_count:      # already in the pool (e.g. from plays) but not looked up yet
                conn.execute(
                    """UPDATE artist_meta SET lf_name = ?, listeners = ?, lf_plays = ?, mbid = COALESCE(mbid, ?),
                           lf_status = 'ok', lf_at = ? WHERE name = ? AND lf_status IS NULL""",
                    (a["name"], a["listeners"], a.get("plays"), a.get("mbid"), now, a["name"]))
    return added


def seed_step():
    """Do ONE chart/genre page that hasn't been fetched yet. Returns False when all are done."""
    with connect() as conn:
        done = {r["key"] for r in conn.execute("SELECT key FROM artist_meta_seeds")}
    todo = [("chart", p) for p in range(1, CHART_PAGES + 1)] + [(t, p) for t in SEED_TAGS for p in range(1, TAG_PAGES + 1)]
    for what, page in todo:
        key = f"{what}:{page}"
        if key in done:
            continue
        if what == "chart":
            added = _add_listed(lastfm.chart_top_artists(page=page), "chart")
        else:
            added = _add_listed(lastfm.tag_top_artists(what, page=page), "tag:" + what)
        with connect() as conn:
            conn.execute("INSERT OR REPLACE INTO artist_meta_seeds (key, done_at) VALUES (?, ?)", (key, time.time()))
        log.info("seed %s: %s new artists", key, added)
        return True
    return False


# ------------------------------------------------------------------ looking artists up
def _next(column_status, column_at, extra_where="", order="", params=()):
    now = time.time()
    with connect() as conn:
        return conn.execute(
            f"""SELECT * FROM artist_meta
                WHERE ({column_status} IS NULL
                       OR ({column_status} = 'error' AND {column_at} < ?)) {extra_where}
                {order} LIMIT 1""", (now - RETRY_ERRORS_AFTER, *params)).fetchone()


def _play_counts():
    """How often the group has played each artist, so the most-played get looked up first
    (they're the ones most likely to come up as answers). Worked out once per batch."""
    with connect() as conn:
        return {r["artist_name"]: r["n"] for r in conn.execute(
            "SELECT artist_name, COUNT(*) AS n FROM plays GROUP BY artist_name")}


def lastfm_step(priority):
    """Look up one artist's listener count. Returns True if there was work to do."""
    with connect() as conn:
        pending = [r["name"] for r in conn.execute(
            """SELECT name FROM artist_meta WHERE lf_status IS NULL
                  OR (lf_status = 'error' AND lf_at < ?)
                  OR (lf_status = 'ok' AND lf_at < ?)
               ORDER BY (source = 'plays') DESC LIMIT 400""",
            (time.time() - RETRY_ERRORS_AFTER, time.time() - REFRESH_AFTER))]
    if not pending:
        return False
    name = max(pending, key=lambda n: priority.get(n, 0))
    if name.strip().lower() in SKIP:
        status, info = "missing", None
    else:
        try:
            info = lastfm.artist_info(name)
            status = "ok" if info and info["listeners"] is not None else "missing"
        except (lastfm.RateLimited, lastfm.Unavailable):
            raise
        except Exception as e:                       # network blip, odd reply: try again tomorrow
            log.warning("last.fm %r: %s", name, e)
            info, status = None, "error"
    with connect() as conn:
        if info:
            conn.execute(
                """UPDATE artist_meta SET lf_name = ?, listeners = ?, lf_plays = ?, tags = ?,
                       mbid = COALESCE(?, mbid), lf_status = ?, lf_at = ? WHERE name = ?""",
                (info["name"], info["listeners"], info["plays"], json.dumps(info["tags"]),
                 info["mbid"], status, time.time(), name))
        else:
            conn.execute("UPDATE artist_meta SET lf_status = ?, lf_at = ? WHERE name = ?", (status, time.time(), name))
    return True


def musicbrainz_step():
    """Look up one artist's where-from / start year / type. Returns True if there was work."""
    row = _next("mb_status", "mb_at", "AND lf_status = 'ok'", "ORDER BY listeners DESC")
    if not row:
        return False
    try:
        facts = musicbrainz.artist(row["mbid"]) if row["mbid"] else None
        if not facts:
            facts = musicbrainz.search_artist(row["lf_name"] or row["name"])
        status = "ok" if facts else "missing"
    except musicbrainz.RateLimited:
        raise
    except Exception as e:
        log.warning("musicbrainz %r: %s", row["name"], e)
        facts, status = None, "error"
    with connect() as conn:
        if facts:
            conn.execute(
                """UPDATE artist_meta SET mbid = COALESCE(mbid, ?), mb_type = ?, mb_country = ?, mb_area = ?,
                       mb_begin_area = ?, mb_begin_year = ?, mb_gender = ?, mb_status = ?, mb_at = ? WHERE name = ?""",
                (facts["mbid"], facts["type"], facts["country"], facts["area"], facts["begin_area"],
                 facts["begin_year"], facts["gender"], status, time.time(), row["name"]))
        else:
            conn.execute("UPDATE artist_meta SET mb_status = ?, mb_at = ? WHERE name = ?", (status, time.time(), row["name"]))
    return True


# ------------------------------------------------------------------ the background thread
_state = {"running": False, "last_error": None, "paused_until": 0, "started_at": None}


def run_for(seconds, priority=None):
    """Work through the queue for up to `seconds`. Each call to Last.fm / MusicBrainz waits
    its turn at that service's gate, so this never goes faster than they allow."""
    priority = priority if priority is not None else _play_counts()
    end = time.monotonic() + seconds
    did = 0
    while time.monotonic() < end:
        worked = False
        try:
            worked = seed_step() or lastfm_step(priority)   # chart pages first: they bring listener counts for free
        except lastfm.RateLimited:
            _state["paused_until"] = time.time() + lastfm.BACKOFF
            _state["last_error"] = "Last.fm asked us to slow down; pausing"
            log.warning("last.fm rate limit: pausing")
            return did
        except lastfm.Unavailable:
            _state["last_error"] = "LASTFM_API_KEY isn't set"
            return did
        if not musicbrainz.blocked():                # in a quiet period: skip it, Last.fm carries on
            try:
                worked = musicbrainz_step() or worked
            except musicbrainz.RateLimited:
                _state["last_error"] = "MusicBrainz asked us to slow down: " + (musicbrainz.last_problem or "")
        if not worked:
            break                                    # nothing left to do for now
        did += 1
    return did


def run_forever():
    """The background thread's job. Gentle: a short rest between rounds, a long one when
    there's nothing to do or a service asked us to back off."""
    _state.update(running=True, started_at=time.time())
    time.sleep(90)                                   # let the site and the first poll settle
    rounds = 0
    while True:
        try:
            if rounds % 36 == 0:                     # every ~6 h: pick up newly played artists
                log.info("pool: %s new artists from plays", add_from_plays())
            rounds += 1
            wait = _state["paused_until"] - time.time()
            if wait > 0:
                time.sleep(wait)
            did = run_for(10 * 60)
            log.info("looked up %s artists", did)
            time.sleep(10 if did else 10 * 60)
        except Exception:
            log.exception("artist lookup round failed")
            time.sleep(10 * 60)


_started = False


def start():
    """Started by scheduler.start() on the cluster. Off when LASTFM_API_KEY is missing, or
    when RUN_ENRICH=0 (handy if Last.fm ever has a problem with us)."""
    global _started
    if _started or os.environ.get("RUN_ENRICH") == "0" or not lastfm.available():
        return
    _started = True
    setup()
    threading.Thread(target=run_forever, name="artistmeta", daemon=True).start()
    log.info("artist lookups started")


def status():
    with connect() as conn:
        c = conn.execute(
            """SELECT COUNT(*) AS total,
                      SUM(lf_status = 'ok') AS lf_ok, SUM(lf_status = 'missing') AS lf_missing,
                      SUM(lf_status = 'error') AS lf_error, SUM(lf_status IS NULL) AS lf_todo,
                      SUM(mb_status = 'ok') AS mb_ok, SUM(mb_status = 'missing') AS mb_missing,
                      SUM(mb_status IS NULL AND lf_status = 'ok') AS mb_todo,
                      SUM(source = 'plays') AS from_plays
               FROM artist_meta""").fetchone()
        seeds = conn.execute("SELECT COUNT(*) AS n FROM artist_meta_seeds").fetchone()["n"]
    out = {k: (c[k] or 0) for k in c.keys()}
    out["seed_pages_done"] = seeds
    out["seed_pages_total"] = CHART_PAGES + len(SEED_TAGS) * TAG_PAGES
    out["running"] = _state["running"]
    out["last_error"] = _state["last_error"]
    out["musicbrainz_paused"] = musicbrainz.blocked()
    # Rough time left: each artist needs ~one Last.fm call (2.5 s); MusicBrainz runs alongside.
    out["hours_left_estimate"] = round(out["lf_todo"] * lastfm.MIN_GAP / 3600, 1)
    return out
