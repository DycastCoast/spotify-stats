"""Trivia: "Do you know you?" Rounds of 10 questions made fresh from your own history.

Each kind of question is a small function that picks things at random from your listening
and works out the right answer from the same data, so the questions never run out and
change as new plays arrive. The facts they draw on are worked out once per person (and
saved, like every other page), so making a round is instant.
"""
import json
import random
import time
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache

import entity
import stats
from db import connect
from diskcache import disk_cached
from entity import BANDS, band_of, local_ts
from playlists import short_title
from stats import MIN_PLAY_MS

ROUND = 10
SEEN_DAYS = 30                      # a question you've had won't come back for this long
SEASONS = {3: "spring", 4: "spring", 5: "spring", 6: "summer", 7: "summer", 8: "summer",
           9: "autumn", 10: "autumn", 11: "autumn"}   # winter straddles two years, so it's left out
BAND_NAMES = {"late night": "After midnight", "morning": "Mornings", "afternoon": "Afternoons", "evening": "Evenings"}

TRIVIA_SQL = """
CREATE TABLE IF NOT EXISTS trivia_seen (user_id TEXT, key TEXT, at REAL, PRIMARY KEY (user_id, key));
CREATE TABLE IF NOT EXISTS trivia_best (user_id TEXT PRIMARY KEY, best INTEGER, rounds INTEGER, last INTEGER);
"""


def setup():
    with connect() as conn:
        conn.executescript(TRIVIA_SQL)


# ------------------------------------------------------------------ the facts questions draw on

def facts(user_id):
    return _facts(user_id, stats.latest_play_id(user_id))


@lru_cache(maxsize=6)
@disk_cached(versions=1)
def _facts(user_id, _latest):
    tl = entity.timeline(user_id)
    plays, first, skipped, known = Counter(), {}, Counter(), Counter()
    year_track, year_artist, year_ms = defaultdict(Counter), defaultdict(Counter), Counter()
    season_track = defaultdict(Counter)
    band_year = defaultdict(Counter)
    for i in range(len(tl.track)):
        t = tl.track[i]
        if tl.skip[i] >= 0:
            known[t] += 1
            skipped[t] += tl.skip[i]
        ms = tl.ms[i]
        if ms < MIN_PLAY_MS:
            continue
        d = datetime.utcfromtimestamp(local_ts(tl.end[i]))
        plays[t] += 1
        first.setdefault(t, tl.end[i])
        year_track[d.year][t] += 1
        year_artist[d.year][tl.track_info[t][1]] += ms
        year_ms[d.year] += ms
        if d.month in SEASONS:
            season_track[(d.year, SEASONS[d.month])][t] += 1
        band_year[d.year][band_of(d.hour)] += ms
        band_year["all"][band_of(d.hour)] += ms

    top = [t for t, _ in plays.most_common(400)]
    songs = {t: {"name": tl.track_info[t][0], "short": short_title(tl.track_info[t][0], 40), "artist": tl.track_info[t][1], "id": tl.track_ids[t],
                 "plays": plays[t], "first": first[t],
                 "skip": skipped[t] / known[t] if known[t] >= 20 else None} for t in top}
    keep = set(top)
    year_songs, season_songs = {}, {}
    for y, c in year_track.items():
        year_songs[y] = [(t, n) for t, n in c.most_common(12) if t in keep]
    for k, c in season_track.items():
        if sum(c.values()) >= 150:
            season_songs[k] = [(t, n) for t, n in c.most_common(12) if t in keep]
    return {
        "songs": songs,
        "year_songs": year_songs,
        "season_songs": season_songs,
        "year_artists": {y: [(a, round(ms / 3_600_000, 1)) for a, ms in c.most_common(10)]
                         for y, c in year_artist.items() if year_ms[y] >= 50 * 3_600_000},
        "year_hours": {y: ms / 3_600_000 for y, ms in year_ms.items() if ms >= 20 * 3_600_000},
        "bands": {y: dict(c) for y, c in band_year.items() if sum(c.values()) >= 30 * 3_600_000},
        "artist_hours": {a: ms / 3_600_000 for a, ms in tl.artist_ms.most_common(300)},
        "last_year": max(year_ms) if year_ms else None,
    }


# ------------------------------------------------------------------ the kinds of question

def _song_option(s):
    return {"label": short_title(s["name"], 40), "sub": s["artist"], "track": s["id"]}


def this_or_that(f, rng, ctx):
    """Which of two songs did you play more? Close enough to be a real question."""
    pool = [s for s in f["songs"].values() if s["plays"] >= 15]
    for _ in range(40):
        a, b = rng.sample(pool, 2)
        hi, lo = max(a["plays"], b["plays"]), min(a["plays"], b["plays"])
        if a["artist"] != b["artist"] and 1.15 <= hi / lo <= 1.8:
            right = 0 if a["plays"] > b["plays"] else 1
            return {"key": f"tot:{min(a['id'], b['id'])}:{max(a['id'], b['id'])}",
                    "kind": "This or that", "prompt": "Which have you played more, all time?",
                    "options": [_song_option(a), _song_option(b)], "answer": right,
                    "figures": [f"{a['plays']} plays", f"{b['plays']} plays"],
                    "reveal": f"{a['short']}: {a['plays']} plays. {b['short']}: {b['plays']}."}


def top_artist_year(f, rng, ctx):
    years = [y for y, a in f["year_artists"].items() if len(a) >= 5]
    if not years:
        return None
    y = rng.choice(years)
    ranked = f["year_artists"][y]
    wrong = rng.sample(ranked[1:8], 3)
    opts = [ranked[0]] + wrong
    rng.shuffle(opts)
    return {"key": f"tay:{y}", "kind": "Your #1", "prompt": f"Who was your most-played artist of {y}?",
            "options": [{"label": a, "sub": None} for a, _ in opts],
            "answer": opts.index(ranked[0]), "figures": [f"{h:g} h" for _, h in opts],
            "reveal": f"{ranked[0][0]}, with {ranked[0][1]:g} hours. Next was {ranked[1][0]} ({ranked[1][1]:g} h)."}


def _nice(n):
    step = 50 if n >= 300 else 20 if n >= 100 else 5
    return max(step, int(round(n / step) * step))


def hours_year(f, rng, ctx):
    years = [y for y in f["year_hours"] if y != f["last_year"]] or list(f["year_hours"])
    if not years:
        return None
    y = rng.choice(years)
    real = f["year_hours"][y]
    line = _nice(real * rng.choice([0.7, 0.8, 1.25, 1.4]))
    more = real > line
    return {"key": f"hy:{y}:{line}", "kind": "Higher or lower",
            "prompt": f"In {y}, did you listen to more or less than {line:,} hours?",
            "options": [{"label": "More", "sub": None}, {"label": "Less", "sub": None}],
            "answer": 0 if more else 1, "figures": None,
            "reveal": f"{real:,.0f} hours in {y}, about {real / 365:.1f} a day."}


def peak_time(f, rng, ctx):
    choices = [k for k in f["bands"] if k == "all" or k != f["last_year"]]
    if not choices:
        return None
    k = rng.choice(choices)
    shares = f["bands"][k]
    total = sum(shares.values()) or 1
    order = [b for b, *_ in BANDS]
    best = max(order, key=lambda b: shares.get(b, 0))
    when = "overall" if k == "all" else f"in {k}"
    return {"key": f"pt:{k}", "kind": "When", "prompt": f"When did you listen most {when}?",
            "options": [{"label": BAND_NAMES[b], "sub": None} for b in order], "answer": order.index(best),
            "figures": [f"{shares.get(b, 0) / total:.0%}" for b in order],
            "reveal": f"{BAND_NAMES[best]}: {shares.get(best, 0) / total:.0%} of your listening {when}."}


def first_heard(f, rng, ctx):
    pool = sorted(f["songs"].values(), key=lambda s: -s["plays"])[:120]
    for _ in range(40):
        picks = rng.sample(pool, 4)
        times = sorted(s["first"] for s in picks)
        if all(b - a >= 60 * 86400 for a, b in zip(times, times[1:])) and len({s["artist"] for s in picks}) == 4:
            right = min(range(4), key=lambda i: picks[i]["first"])
            fmt = lambda s: datetime.utcfromtimestamp(s["first"]).strftime("%b %Y")
            return {"key": "fh:" + ":".join(sorted(s["id"] for s in picks)), "kind": "First heard",
                    "prompt": "Which of these did you discover first?",
                    "options": [_song_option(s) for s in picks], "answer": right,
                    "figures": [fmt(s) for s in picks],
                    "reveal": f"{picks[right]['short']}, back in {fmt(picks[right])}."}


def most_skipped(f, rng, ctx):
    pool = [s for s in f["songs"].values() if s["skip"] is not None]
    if len(pool) < 8:
        return None
    for _ in range(40):
        picks = rng.sample(pool, 4)
        rates = sorted(s["skip"] for s in picks)
        if all(b - a >= 0.08 for a, b in zip(rates, rates[1:])):
            right = max(range(4), key=lambda i: picks[i]["skip"])
            return {"key": "ms:" + ":".join(sorted(s["id"] for s in picks)), "kind": "Skips",
                    "prompt": "Which of these do you skip most often?",
                    "options": [_song_option(s) for s in picks], "answer": right,
                    "figures": [f"{s['skip']:.0%} skipped" for s in picks],
                    "reveal": f"{picks[right]['short']}: skipped {picks[right]['skip']:.0%} of the time."}


def season_song(f, rng, ctx):
    keys = [k for k, v in f["season_songs"].items() if len(v) >= 6]
    if not keys:
        return None
    y, season = rng.choice(keys)
    ranked = f["season_songs"][(y, season)]
    opts = [ranked[0]] + rng.sample(ranked[1:9], 3)
    rng.shuffle(opts)
    songs = f["songs"]
    return {"key": f"ss:{y}:{season}", "kind": "Eras", "prompt": f"What was your top song of {season} {y}?",
            "options": [_song_option(songs[t]) for t, _ in opts], "answer": opts.index(ranked[0]),
            "figures": [f"{n} plays" for _, n in opts],
            "reveal": f"{songs[ranked[0][0]]['short']}, {ranked[0][1]} plays that {season}."}


def friend_artist(f, rng, ctx):
    """Who has played more of an artist you both listen to: you or a friend?"""
    for friend_id, friend_name, friend_facts in rng.sample(ctx["friends"], len(ctx["friends"])):
        shared = [a for a in f["artist_hours"] if friend_facts["artist_hours"].get(a, 0) >= 2 and f["artist_hours"][a] >= 2]
        if not shared:
            continue
        a = rng.choice(shared[:40])
        mine, theirs = f["artist_hours"][a], friend_facts["artist_hours"][a]
        if max(mine, theirs) / min(mine, theirs) < 1.2:
            continue
        return {"key": f"fa:{friend_id}:{a}", "kind": "Friends", "prompt": f"Who has listened to more {a}?",
                "options": [{"label": "You", "sub": None}, {"label": friend_name, "sub": None}],
                "answer": 0 if mine > theirs else 1, "figures": [f"{mine:,.0f} h", f"{theirs:,.0f} h"],
                "reveal": f"You: {mine:,.0f} hours. {friend_name}: {theirs:,.0f}."}


KINDS = [this_or_that, top_artist_year, hours_year, peak_time, first_heard, most_skipped, season_song, friend_artist]


# ------------------------------------------------------------------ a round

def seen_keys(user_id):
    with connect() as conn:
        rows = conn.execute("SELECT key FROM trivia_seen WHERE user_id = ? AND at > ?",
                            (user_id, time.time() - SEEN_DAYS * 86400)).fetchall()
    return {r["key"] for r in rows}


def make_round(user_id, seed=None):
    """Ten questions, at most two of each kind, none you've had in the last month."""
    f = facts(user_id)
    if not f["songs"]:
        return []
    rng = random.Random(seed)
    friends = [(p["id"], p["display_name"], facts(p["id"])) for p in stats.people() if p["id"] != user_id]
    ctx = {"friends": friends}
    seen = seen_keys(user_id)
    out, used, per_kind = [], set(), Counter()
    order = KINDS[:] * 3
    rng.shuffle(order)
    for kind in order:
        if len(out) >= ROUND:
            break
        if per_kind[kind] >= (1 if kind is peak_time else 2) or (kind is friend_artist and not friends):
            continue
        for _ in range(6):
            q = kind(f, rng, ctx)
            if q and q["key"] not in seen and q["key"] not in used:
                used.add(q["key"])
                per_kind[kind] += 1
                out.append(q)
                break
    with connect() as conn:
        conn.executemany("INSERT OR REPLACE INTO trivia_seen (user_id, key, at) VALUES (?, ?, ?)",
                         [(user_id, q["key"], time.time()) for q in out])
    return out


def best(user_id):
    with connect() as conn:
        row = conn.execute("SELECT best, rounds, last FROM trivia_best WHERE user_id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def record(user_id, score):
    score = max(0, min(ROUND, int(score)))
    with connect() as conn:
        conn.execute("""INSERT INTO trivia_best (user_id, best, rounds, last) VALUES (?, ?, 1, ?)
                        ON CONFLICT(user_id) DO UPDATE SET best = MAX(best, excluded.best),
                        rounds = rounds + 1, last = excluded.last""", (user_id, score, score))
    return best(user_id)
