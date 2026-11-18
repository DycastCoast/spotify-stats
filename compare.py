"""
You vs a friend: how much your listening overlaps, who found shared artists first,
what only one of you plays, and the times you were listening together (Jams).

Everything works from each person's timeline (see entity.py), so nothing new is
read from the database. Results are cached until either person has new plays.

Fairness rule used throughout: compare like with like. By default only the years
you BOTH have history for are used, otherwise whoever has the longer history
looks broader just because they have more of it.
"""
import bisect
import math
from collections import Counter, defaultdict
from datetime import datetime, timezone
from functools import lru_cache

from diskcache import disk_cached

import entity
from db import connect
from entity import fmt_date, local_ts
from stats import LOCAL_TZ, MIN_PLAY_MS, latest_play_id

FOUND_MIN_PLAYS = 10          # an artist only counts as "found" once you've played them 10+ times
JAM_WINDOW_S = 5 * 60         # same song, finishing within 5 minutes of each other = listening together
JAM_GAP_S = 30 * 60           # matches more than 30 minutes apart are separate sessions
JAM_MIN_SONGS = 2             # one lone match can be chance; two or more in a row is a session


def song_key(tl, t):
    """Songs are matched between people by name + artist: the same song often has
    a different Spotify ID in each person's history (single vs album version)."""
    name, artist, _ = tl.track_info[t]
    return ((name or "").lower(), artist)


def year_bounds(year):
    """The UTC timestamps where a UK calendar year starts and ends."""
    lo = datetime(year, 1, 1, tzinfo=LOCAL_TZ).timestamp()
    hi = datetime(year + 1, 1, 1, tzinfo=LOCAL_TZ).timestamp()
    return lo, hi


def span(tl):
    """First and last play, as timestamps."""
    return (tl.end[0], tl.end[-1]) if len(tl.end) else (0, 0)


def periods(me, other):
    """The choices for the page-wide period filter: the shared years, then each year in them."""
    a, b = entity.timeline(me), entity.timeline(other)
    lo, hi = max(span(a)[0], span(b)[0]), min(span(a)[1], span(b)[1])
    if lo >= hi:
        return lo, hi, []
    first = datetime.fromtimestamp(lo, LOCAL_TZ).year
    last = datetime.fromtimestamp(hi, LOCAL_TZ).year
    return lo, hi, [str(y) for y in range(last, first - 1, -1)]


# ------------------------------------------------------------------ totals in a window

def totals(tl, lo, hi):
    """Listening time per artist and per song (by key) between two timestamps."""
    artists, songs, song_id = Counter(), Counter(), {}
    start = bisect.bisect_left(tl.end, lo)          # plays are in time order, so jump straight to lo
    stop = bisect.bisect_left(tl.end, hi)
    info = tl.track_info
    for i in range(start, stop):
        ms = tl.ms[i]
        if ms < MIN_PLAY_MS:
            continue
        t = tl.track[i]
        artists[info[t][1]] += ms
        key = song_key(tl, t)
        songs[key] += ms
        song_id.setdefault(key, tl.track_ids[t])
    return artists, songs, song_id


def overlap(a, b):
    """How alike two people's listening is, from 0 to 100.

    Each person's listening is turned into shares (Kanye = 5% of Ethan's time).
    For every artist, take the SMALLER of the two shares and add them up. That is
    the part of your listening you have in common: if you both gave 5% to Kanye,
    that's 5 points; if you gave him 1% and Ethan 5%, only 1 point is shared.
    Two identical people score 100, two people with nothing in common score 0.
    """
    ta, tb = sum(a.values()) or 1, sum(b.values()) or 1
    return 100 * sum(min(a[k] / ta, b[k] / tb) for k in a.keys() & b.keys())


def share_on(a, keys):
    """What % of one person's time goes on a set of artists."""
    return 100 * sum(a[k] for k in keys) / (sum(a.values()) or 1)


# ------------------------------------------------------------------ the Venn diagram

def lens_area(r1, r2, d):
    """Area where two circles of radius r1 and r2, d apart, overlap."""
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return math.pi * min(r1, r2) ** 2
    a1 = r1 * r1 * math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
    a2 = r2 * r2 * math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2))
    k = 0.5 * math.sqrt((-d + r1 + r2) * (d + r1 - r2) * (d - r1 + r2) * (d + r1 + r2))
    return a1 + a2 - k


def venn(n_a, n_b, n_shared):
    """Circle sizes and positions so every AREA is to scale: each circle's area is
    one person's number of artists, and the overlap's area is the shared ones.

    There's no formula for the distance that gives a particular overlap, so it's
    found by bisection: try the midpoint, see if the overlap is too big or too
    small, halve the range, repeat. 60 rounds is far more precise than a pixel.
    """
    W, H, PAD = 560, 300, 20
    scale = (H / 2 - PAD) / math.sqrt(max(n_a, n_b) / math.pi)   # the bigger circle fills the height
    r1, r2 = math.sqrt(n_a / math.pi) * scale, math.sqrt(n_b / math.pi) * scale
    target = n_shared * scale * scale
    lo, hi = abs(r1 - r2), r1 + r2
    for _ in range(60):
        mid = (lo + hi) / 2
        lo, hi = (mid, hi) if lens_area(r1, r2, mid) > target else (lo, mid)
    d = (lo + hi) / 2
    width = r1 + d + r2
    cx1 = (W - width) / 2 + r1
    cx2 = cx1 + d
    # Where the two edges cross the centre line: the shared count goes between them.
    lens_mid = ((cx2 - r2) + (cx1 + r1)) / 2
    return {
        "w": W, "h": H, "cy": H / 2,
        "a": {"cx": round(cx1, 1), "r": round(r1, 1), "label_x": round(((cx1 - r1) + (cx2 - r2)) / 2, 1)},
        "b": {"cx": round(cx2, 1), "r": round(r2, 1), "label_x": round(((cx1 + r1) + (cx2 + r2)) / 2, 1)},
        "mid_x": round(lens_mid, 1),
    }


# ------------------------------------------------------------------ who found them first

def first_listens(tl, min_plays=FOUND_MIN_PLAYS):
    """Each artist you've played min_plays+ times -> the time of your first listen."""
    first, plays = {}, Counter()
    for i in range(len(tl.track)):
        if tl.ms[i] >= MIN_PLAY_MS:
            artist = tl.track_info[tl.track[i]][1]
            first.setdefault(artist, tl.end[i])
            plays[artist] += 1
    return {a: t for a, t in first.items() if plays[a] >= min_plays}


def who_first(ta, tb, weight_a, weight_b):
    """For artists you've BOTH played 10+ times: who got there first, and by how long.

    If the earlier first listen is from before the other person's history even
    starts, we can't know: they might have been listening for years before their
    records begin. Those are counted as "can't tell" instead of being given away.
    """
    fa, fb = first_listens(ta), first_listens(tb)
    start_a, start_b = span(ta)[0], span(tb)[0]
    rows, counts = [], Counter()
    for artist in fa.keys() & fb.keys():
        a, b = fa[artist], fb[artist]
        if a <= b:
            winner, lead, fair = "a", b - a, a >= start_b
        else:
            winner, lead, fair = "b", a - b, b >= start_a
        if not fair:
            winner = "unknown"
        elif lead < 7 * 86400:            # within a week: call it a draw (found together?)
            winner = "same"
        counts[winner] += 1
        rows.append({"artist": artist, "winner": winner, "lead_days": lead / 86400,
                     "a_date": fmt_date(datetime.fromtimestamp(a, LOCAL_TZ)),
                     "b_date": fmt_date(datetime.fromtimestamp(b, LOCAL_TZ)),
                     "weight": min(weight_a.get(artist, 0), weight_b.get(artist, 0))})
    rows.sort(key=lambda r: -r["weight"])          # the artists you both care about most come first
    return counts, rows


def lead_text(days):
    if days >= 365:
        years = days / 365.25
        return f"{years:.1f} years" if years < 10 else f"{years:.0f} years"
    if days >= 60:
        return f"{days / 30.44:.0f} months"
    return f"{days:.0f} days"


# ------------------------------------------------------------------ listening together

def jams(ta, tb):
    """Times you were both playing the same song at the same moment.

    A Jam (or one of you playing on the other's console) shows up in BOTH
    histories: the same song, finishing within a few minutes of each other.
    Matches are grouped into sessions; a single match on its own is ignored,
    because two people who like the same songs will occasionally line up by chance.
    """
    # Every listen of each song in B's history, as a sorted list of end times.
    b_times = defaultdict(list)
    for i in range(len(tb.track)):
        if tb.ms[i] >= MIN_PLAY_MS:
            b_times[song_key(tb, tb.track[i])].append(i)
    matches = []
    for i in range(len(ta.track)):
        if ta.ms[i] < MIN_PLAY_MS:
            continue
        key = song_key(ta, ta.track[i])
        cands = b_times.get(key)
        if not cands:
            continue
        t = ta.end[i]
        j = bisect.bisect_left(cands, t - JAM_WINDOW_S, key=lambda p: tb.end[p])
        if j < len(cands) and tb.end[cands[j]] <= t + JAM_WINDOW_S:
            matches.append((i, cands[j]))

    sessions = []
    for i, j in matches:
        if sessions and ta.end[i] - ta.end[sessions[-1][-1][0]] <= JAM_GAP_S:
            sessions[-1].append((i, j))
        else:
            sessions.append([(i, j)])
    return [s for s in sessions if len(s) >= JAM_MIN_SONGS]


def plays_before(tl, t, when):
    """How many times this person had played song t before a moment."""
    positions = tl.by_track.get(t, [])
    n = bisect.bisect_left(positions, when, key=lambda p: tl.end[p])
    return sum(1 for p in positions[:n] if tl.ms[p] >= MIN_PLAY_MS)


COUNTRIES = {"ES": "Spain", "FR": "France", "IT": "Italy", "PT": "Portugal", "DE": "Germany", "NL": "the Netherlands",
             "IE": "Ireland", "US": "the US", "GR": "Greece", "TR": "Turkey", "CN": "China", "HK": "Hong Kong",
             "MO": "Macao", "JP": "Japan"}
DEVICE_NAMES = (("playstation", "PlayStation"), ("xbox", "Xbox"), ("windows", "computer"), ("os x", "computer"),
                ("android", "phone"), ("ios", "phone"), ("cast", "speaker"), ("tv", "TV"))


def jam_summary(me, other, ta, tb):
    sessions = jams(ta, tb)
    if not sessions:
        return None
    # The device and country of each matched play (not kept in the timeline), one small query per session.
    extra = {me: {}, other: {}}
    with connect() as conn:
        for s in sessions:
            for uid, tl, idx in ((me, ta, 0), (other, tb, 1)):
                extra[uid].update(device_and_country(conn, uid, [tl.end[p[idx]] for p in s]))

    out, song_count, ms_total = [], Counter(), 0
    owner = Counter()                                # whose music the Jam songs were
    for s in sessions:
        first_i, last_i = s[0][0], s[-1][0]
        start = ta.end[first_i] - ta.ms[first_i] / 1000
        songs, minutes = [], 0
        for i, j in s:
            t = ta.track[i]
            mine = plays_before(ta, t, start)
            theirs = plays_before(tb, tb.track[j], start)
            whose = "new" if mine == 0 and theirs == 0 else "a" if mine > theirs else "b" if theirs > mine else "both"
            owner[whose] += 1
            song_count[song_key(ta, t)] += 1
            minutes += min(ta.ms[i], tb.ms[j]) / 60000
            songs.append({"name": ta.track_info[t][0], "artist": ta.track_info[t][1],
                          "track": ta.track_ids[t], "whose": whose})
        ms_total += minutes * 60000
        when = datetime.fromtimestamp(start, LOCAL_TZ)
        mine_extra = Counter(extra[me].get(ta.end[i]) for i, _ in s).most_common(1)[0][0] or (None, None)
        theirs_extra = Counter(extra[other].get(tb.end[j]) for _, j in s).most_common(1)[0][0] or (None, None)
        out.append({
            "date": f"{when:%a} {when.day} {when:%b %Y}",
            "time": when.strftime("%H:%M"),
            "year": when.year,
            "late": when.hour < 5,
            "length_min": round((ta.end[last_i] - start) / 60),
            "minutes": round(minutes),
            "songs": songs,
            "my_device": mine_extra[0], "their_device": theirs_extra[0],
            # Privacy: only YOUR country is ever shown, never the other person's.
            "my_country": (COUNTRIES.get(mine_extra[1], mine_extra[1])
                           if mine_extra[1] and mine_extra[1] != "GB" else None),
            "my_country_code": mine_extra[1] if mine_extra[1] and mine_extra[1] != "GB" else None,
        })

    biggest = max(out, key=lambda x: x["minutes"])
    by_year = Counter(x["year"] for x in out)
    # The song key is lower-cased, so take the real spelling (and an ID for the cover) from a session.
    spelling = {(x["name"].lower(), x["artist"]): (x["name"], x["track"]) for sess in out for x in sess["songs"]}
    top_songs = [{"name": spelling[key][0], "track": spelling[key][1], "artist": key[1], "n": n}
                 for key, n in song_count.most_common(8)]
    total_songs = sum(owner.values())
    return {
        "sessions": list(reversed(out)),             # newest first
        "count": len(out),
        "hours": ms_total / 3_600_000,
        "songs": total_songs,
        "biggest": biggest,
        "late_share": round(100 * sum(1 for x in out if x["late"]) / len(out)),
        "years": sorted(by_year.items()),
        "top_songs": top_songs,
        "owner": {k: round(100 * owner[k] / total_songs) for k in ("a", "b", "both", "new")},
        "owner_n": dict(owner),
    }


def device_and_country(conn, user_id, stamps):
    """end timestamp -> (device type, country) for the plays of one short session."""
    rows = conn.execute(
        """SELECT (julianday(played_at) - 2440587.5) * 86400.0, platform, conn_country
           FROM plays WHERE user_id = ? AND played_at BETWEEN ? AND ?""",
        (user_id, iso(min(stamps) - 2), iso(max(stamps) + 2))).fetchall()
    # Match on the rounded second: the stored and rebuilt numbers can differ by a hair.
    found = {}
    for ts, platform, country in rows:
        low = (platform or "").lower()
        found[round(ts)] = (next((name for key, name in DEVICE_NAMES if key in low), None), country)
    return {s: found.get(round(s)) for s in stamps}


def iso(ts):
    """A timestamp in the same text form the database stores dates in, for a fast range lookup."""
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")


# ------------------------------------------------------------------ overlap by year chart

def year_chart(rows):
    """Points for a line chart of the overlap score per year (hand-drawn SVG)."""
    if len(rows) < 2:
        return None
    W, H, LEFT, RIGHT, TOP, BOTTOM = 960, 260, 76, 40, 24, 40   # wide margins: phone labels are big
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM
    peak = max(r["score"] for r in rows)
    nice = 10 * math.ceil(peak / 10)                 # round the axis up to the next 10%
    step = 10 if nice <= 50 else 20
    pts = []
    for n, r in enumerate(rows):
        x = LEFT + pw * n / (len(rows) - 1)
        y = TOP + ph * (1 - r["score"] / nice)
        pts.append({**r, "x": round(x, 1), "y": round(y, 1), "peak": r["score"] == peak,
                    "tip": f"{r['year']}{' (part year)' if r['partial'] else ''} · {r['score']:.0f}% overlap"
                           + (f" · most shared: {r['top']}" if r["top"] else "")})
    best = max(rows, key=lambda r: r["score"])
    return {"w": W, "h": H, "left": LEFT, "right": W - RIGHT, "bottom": TOP + ph,
            "slot": round(pw / (len(rows) - 1), 1),
            "points": pts, "path": "M" + " L".join(f"{p['x']},{p['y']}" for p in pts),
            "y_ticks": [(v, round(TOP + ph * (1 - v / nice), 1)) for v in range(0, nice + 1, step)],
            "best": best, "first": rows[0], "last": rows[-1]}


# ------------------------------------------------------------------ the whole page

def compare(me, other, period=None):
    if period not in periods(me, other)[2]:       # anything unexpected -> the shared years (and one cache entry)
        period = None
    return _compare(me, other, period, latest_play_id(me), latest_play_id(other))


@lru_cache(maxsize=16)
@disk_cached(versions=2)
def _compare(me, other, period, _latest_me, _latest_other):
    ta, tb = entity.timeline(me), entity.timeline(other)
    shared_lo, shared_hi, years = periods(me, other)
    if not years:
        return None
    if period in years:
        lo, hi = year_bounds(int(period))
        lo, hi = max(lo, shared_lo), min(hi, shared_hi + 1)
    else:
        period, lo, hi = None, shared_lo, shared_hi + 1

    art_a, songs_a, ids_a = totals(ta, lo, hi)
    art_b, songs_b, ids_b = totals(tb, lo, hi)
    shared = art_a.keys() & art_b.keys()
    tot_a, tot_b = sum(art_a.values()) or 1, sum(art_b.values()) or 1
    share_a = {k: v / tot_a for k, v in art_a.items()}
    share_b = {k: v / tot_b for k, v in art_b.items()}

    def side_by_side(a, b, ids, label_key, n=20):
        ta_, tb_ = sum(a.values()) or 1, sum(b.values()) or 1
        both = a.keys() & b.keys()
        # Ranked by the SMALLER share: to be high on this list, it has to matter to both of you.
        ranked = sorted(both, key=lambda k: -min(a[k] / ta_, b[k] / tb_))[:n]
        top = max((max(a[k] / ta_, b[k] / tb_) for k in ranked), default=1) or 1
        rows = []
        for k in ranked:
            pa, pb = 100 * a[k] / ta_, 100 * b[k] / tb_
            rows.append({**label_key(k), "pa": pa, "pb": pb, "ha": a[k] / 3_600_000, "hb": b[k] / 3_600_000,
                         "wa": round(100 * pa / 100 / top, 1), "wb": round(100 * pb / 100 / top, 1),
                         "track": ids.get(k)})
        return rows

    artist_rows = side_by_side(art_a, art_b, {}, lambda k: {"name": k})
    for row in artist_rows:                          # a song of theirs to fetch the artist's photo through
        best = ta.artist_best.get(row["name"])
        row["track"] = ta.track_ids[best] if best is not None else None
    song_rows = side_by_side(songs_a, songs_b, ids_a,
                             lambda k: {"name": next(ta.track_info[t][0] for t in [ta.index_of[ids_a[k]]]),
                                        "artist": k[1]})

    # Only one of you: the other has NEVER played them, in their whole history (not just this period).
    def only(mine, my_tl, their_tl, n=10):
        rows = [(a, ms) for a, ms in mine.most_common() if a not in their_tl.artist_ms][:n]
        top = rows[0][1] if rows else 1
        out = []
        for a, ms in rows:
            best = my_tl.artist_best.get(a)
            out.append({"name": a, "fig": f"{ms / 3_600_000:,.0f} h" if ms >= 3_600_000 else f"{ms / 60000:.0f} min",
                        "bar": round(100 * ms / top), "track": my_tl.track_ids[best] if best is not None else None})
        return out

    only_a_count = sum(1 for k in art_a if k not in art_b)
    only_b_count = sum(1 for k in art_b if k not in art_a)
    time_shared_a, time_shared_b = share_on(art_a, shared), share_on(art_b, shared)

    # Overlap per year, over the shared years only.
    by_year = []
    for y in sorted(years):
        ylo, yhi = year_bounds(int(y))
        ylo, yhi = max(ylo, shared_lo), min(yhi, shared_hi + 1)
        ya, _, _ = totals(ta, ylo, yhi)
        yb, _, _ = totals(tb, ylo, yhi)
        if not ya or not yb:
            continue
        both = ya.keys() & yb.keys()
        ta_, tb_ = sum(ya.values()), sum(yb.values())
        top_shared = max(both, key=lambda k: min(ya[k] / ta_, yb[k] / tb_)) if both else None
        partial = ylo > year_bounds(int(y))[0] + 86400 or yhi < year_bounds(int(y))[1] - 86400
        by_year.append({"year": int(y), "score": overlap(ya, yb), "top": top_shared, "partial": partial})

    found_counts, found_rows = who_first(ta, tb, share_a, share_b)
    for row in found_rows:
        best = ta.artist_best.get(row["artist"])
        row["track"] = ta.track_ids[best] if best is not None else None
        row["lead"] = lead_text(row["lead_days"])

    # The shared artist each of you cares about far more than the other does. Both
    # must have given them 2+ hours, so it's a real shared artist, not a one-off play.
    lopsided = {}
    real = [k for k in shared if art_a[k] >= 2 * 3_600_000 and art_b[k] >= 2 * 3_600_000]
    for fan, mine, theirs in (("a", share_a, share_b), ("b", share_b, share_a)):
        picks = [k for k in real if mine[k] > theirs[k]]
        if picks:
            k = max(picks, key=lambda k: mine[k] / theirs[k])
            best = ta.artist_best.get(k)
            lopsided[fan] = {"name": k, "mine": 100 * mine[k], "theirs": 100 * theirs[k],
                             "ratio": mine[k] / theirs[k], "track": ta.track_ids[best] if best is not None else None}

    lo_d, hi_d = datetime.fromtimestamp(lo, LOCAL_TZ), datetime.fromtimestamp(min(hi - 1, shared_hi), LOCAL_TZ)
    return {
        "period": period,
        "years": years,
        "range": f"{lo_d:%b %Y} to {hi_d:%b %Y}",
        "score": overlap(art_a, art_b),
        "song_score": overlap(songs_a, songs_b),
        "shared_a": time_shared_a, "shared_b": time_shared_b,
        "hours_a": tot_a / 3_600_000, "hours_b": tot_b / 3_600_000,
        "counts": {"a": len(art_a), "b": len(art_b), "shared": len(shared),
                   "only_a": only_a_count, "only_b": only_b_count},
        "venn": venn(len(art_a), len(art_b), len(shared)) if shared else None,
        "artists": artist_rows,
        "songs": song_rows,
        "song_counts": {"a": len(songs_a), "b": len(songs_b), "shared": len(songs_a.keys() & songs_b.keys())},
        "only_a": only(art_a, ta, tb),
        "only_b": only(art_b, tb, ta),
        "by_year": year_chart(by_year),
        "found": {"counts": found_counts, "rows": found_rows[:20], "total": len(found_rows)},
        "lopsided": lopsided,
        "jams": jam_summary(me, other, ta, tb),
        "shared_end": fmt_date(datetime.fromtimestamp(shared_hi, LOCAL_TZ)),
        "history_a": (fmt_date(datetime.fromtimestamp(span(ta)[0], LOCAL_TZ)),
                      fmt_date(datetime.fromtimestamp(span(ta)[1], LOCAL_TZ))),
        "history_b": (fmt_date(datetime.fromtimestamp(span(tb)[0], LOCAL_TZ)),
                      fmt_date(datetime.fromtimestamp(span(tb)[1], LOCAL_TZ))),
    }


# ------------------------------------------------------------------ one song or artist, side by side

@lru_cache(maxsize=8)
@disk_cached(versions=1)
def _song_index(user_id, _latest):
    """(lower-case name, artist) -> song index in this person's timeline, to find 'the same song' across people."""
    tl = entity.timeline(user_id)
    return {song_key(tl, t): t for t in range(len(tl.track_info)) if tl.track_ms.get(t)}


def song_index(user_id):
    return _song_index(user_id, latest_play_id(user_id))


def first_play(tl, positions):
    return next((i for i in positions if tl.ms[i] >= MIN_PLAY_MS), None)


def who_got_there(ta, tb, ia, ib, name_a, name_b):
    """'You found them 2.1 years before Ethan', with the same fairness rule as the Compare page."""
    if ia is None or ib is None:
        return None
    a, b = ta.end[ia], tb.end[ib]
    if abs(a - b) < 7 * 86400:
        return "You both started within the same week." if name_a == "You" else f"{name_a} and {name_b} started within the same week."
    first, second, t_first, t_second, other_start = ((name_a, name_b, a, b, span(tb)[0]) if a < b
                                                     else (name_b, name_a, b, a, span(ta)[0]))
    whose = lambda n: "your" if n == "You" else f"{n}'s"
    if t_first < other_start:
        return (f"{first} got there first as far as the records go, but {whose(second)} history only starts "
                f"{fmt_date(datetime.fromtimestamp(other_start, LOCAL_TZ))}, so it can't be called.")
    verb = "were" if first == "You" else "was"
    return f"{first} {verb} there first, {lead_text((t_second - t_first) / 86400)} before {second.lower() if second == 'You' else second}."


def vs_chart(ta, tb, pa, pb):
    """Plays per month for both people on one chart (two lines, blue and orange)."""
    def months(tl, positions):
        c = Counter()
        for i in positions:
            if tl.ms[i] >= MIN_PLAY_MS:
                d = datetime.fromtimestamp(tl.end[i], LOCAL_TZ)
                c[d.year * 12 + d.month - 1] += 1
        return c
    ma, mb = months(ta, pa), months(tb, pb)
    if not ma and not mb:
        return None
    lo, hi = min(list(ma) + list(mb)), max(list(ma) + list(mb))
    if hi - lo < 2:
        return None
    W, H, LEFT, RIGHT, TOP, BOTTOM = 960, 200, 44, 16, 14, 28
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM
    peak = max(list(ma.values()) + list(mb.values()))
    rough = peak / 4
    mag = 10 ** math.floor(math.log10(rough)) if rough >= 1 else 1
    step = max(1, int(next(x * mag for x in (1, 2, 5, 10) if x * mag >= rough)))
    nice = step * math.ceil(peak / step)
    xs = lambda m: LEFT + pw * (m - lo) / (hi - lo)
    ys = lambda v: TOP + ph * (1 - v / nice)
    path = lambda c: "M" + " L".join(f"{xs(m):.1f},{ys(c.get(m, 0)):.1f}" for m in range(lo, hi + 1))
    years = sorted({m // 12 for m in range(lo, hi + 1)})
    every = max(1, math.ceil(len(years) / 9))
    return {
        "w": W, "h": H, "left": LEFT, "right": W - RIGHT, "bottom": TOP + ph,
        "a": path(ma), "b": path(mb),
        "y_ticks": [(v, round(ys(v), 1)) for v in range(0, nice + 1, step)],
        "x_ticks": [(str(y), round(xs(y * 12), 1)) for y in years[::every] if y * 12 >= lo],
        "cols": [{"x": round(xs(m) - pw / (hi - lo) / 2, 1), "w": round(pw / (hi - lo), 1),
                  "tip": f"{datetime(m // 12, m % 12 + 1, 1):%b %Y} · {ma.get(m, 0)} vs {mb.get(m, 0)} plays"}
                 for m in range(lo, hi + 1)],
    }


def pair(label, a, b, fmt, more_is="more"):
    """One row of the side-by-side table: both values, and bar widths relative to the bigger one."""
    top = max(a or 0, b or 0) or 1
    return {"label": label, "a": fmt(a) if a is not None else "–", "b": fmt(b) if b is not None else "–",
            "wa": round(100 * (a or 0) / top), "wb": round(100 * (b or 0) / top), "bars": more_is == "more"}


def peak_year(tl, positions):
    years = Counter()
    for i in positions:
        if tl.ms[i] >= MIN_PLAY_MS:
            years[datetime.fromtimestamp(tl.end[i], LOCAL_TZ).year] += tl.ms[i]
    return years.most_common(1)[0][0] if years else None


def vs_artist(user_a, user_b, artist, name_a, name_b):
    ta, tb = entity.timeline(user_a), entity.timeline(user_b)
    if not ta.artist_ms.get(artist):
        return None
    pa, pb = ta.by_artist[artist], tb.by_artist.get(artist, [])
    plays = lambda tl, ps: sum(1 for i in ps if tl.ms[i] >= MIN_PLAY_MS)
    hb = tb.artist_ms.get(artist, 0)
    result = {"kind": "artist", "a": name_a, "b": name_b, "never": not hb}
    if not hb:
        return result
    share = lambda tl: 100 * tl.artist_ms[artist] / (tl.total_ms or 1)
    result["rows"] = [
        pair("Listening time", ta.artist_ms[artist] / 3_600_000, hb / 3_600_000, lambda v: f"{v:,.1f} h" if v < 10 else f"{v:,.0f} h"),
        pair("Plays", plays(ta, pa), plays(tb, pb), lambda v: f"{v:,}"),
        pair("Share of listening", share(ta), share(tb), lambda v: f"{v:.2f}%" if v < 0.1 else f"{v:.1f}%"),
        pair("Rank", ta.artist_rank[artist], tb.artist_rank[artist], lambda v: f"#{v:,}", more_is="less"),
        pair("Peak year", peak_year(ta, pa), peak_year(tb, pb), str, more_is="none"),
    ]
    result["found"] = who_got_there(ta, tb, first_play(ta, pa), first_play(tb, pb), name_a, name_b)
    # Each person's top songs by this artist, marking the ones both have in their top lists.
    def top(tl, ps):
        c = Counter()
        for i in ps:
            if tl.ms[i] >= MIN_PLAY_MS:
                c[song_key(tl, tl.track[i])] += tl.ms[i]
        return c.most_common(5)
    top_a, top_b = top(ta, pa), top(tb, pb)
    both = {k for k, _ in top_a} & {k for k, _ in top_b}
    idx_a = song_index(user_a)
    def rows(items, tl, idx):
        out = []
        for k, ms in items:
            t = idx.get(k)
            out.append({"name": tl.track_info[t][0] if t is not None else k[0], "hours": ms / 3_600_000,
                        "track": tl.track_ids[t] if t is not None else None, "both": k in both})
        return out
    result["songs_a"] = rows(top_a, ta, idx_a)
    result["songs_b"] = rows(top_b, tb, song_index(user_b))
    # Links on B's list go to A's song page when A has the song, so they stay on the same person's pages.
    for s, (k, _) in zip(result["songs_b"], top_b):
        t = idx_a.get(k)
        s["track_a"] = ta.track_ids[t] if t is not None else None
    result["chart"] = vs_chart(ta, tb, pa, pb)
    return result


def vs_song(user_a, user_b, track_id, name_a, name_b):
    ta, tb = entity.timeline(user_a), entity.timeline(user_b)
    t = ta.index_of.get(track_id)
    if t is None:
        return None
    key = song_key(ta, t)
    tb_t = song_index(user_b).get(key)
    result = {"kind": "song", "a": name_a, "b": name_b, "never": tb_t is None}
    if tb_t is None:
        return result
    pa, pb = ta.by_track[t], tb.by_track[tb_t]
    plays = lambda tl, ps: sum(1 for i in ps if tl.ms[i] >= MIN_PLAY_MS)
    skips = lambda tl, ps: ([tl.skip[i] for i in ps if tl.skip[i] >= 0] or None)
    sa, sb = skips(ta, pa), skips(tb, pb)
    rate = lambda s: 100 * sum(s) / len(s) if s and len(s) >= 5 else None
    result["rows"] = [
        pair("Plays", plays(ta, pa), plays(tb, pb), lambda v: f"{v:,}"),
        pair("Listening time", ta.track_ms[t] / 3_600_000, tb.track_ms[tb_t] / 3_600_000,
             lambda v: f"{v:.1f} h" if v >= 1 else f"{v * 60:.0f} min"),
        pair("Rank", ta.track_rank[t], tb.track_rank[tb_t], lambda v: f"#{v:,}", more_is="less"),
        pair("Skipped", rate(sa), rate(sb), lambda v: f"{v:.0f}%", more_is="none"),
        pair("Peak year", peak_year(ta, pa), peak_year(tb, pb), str, more_is="none"),
    ]
    result["found"] = who_got_there(ta, tb, first_play(ta, pa), first_play(tb, pb), name_a, name_b)
    result["chart"] = vs_chart(ta, tb, pa, pb)
    return result


def vs_album(user_a, user_b, name, artist, name_a, name_b, saved=None):
    """One album, two people: totals, full listens, each person's favourite song on it,
    and every song side by side (share of each person's plays of the album)."""
    import albumpage
    a = albumpage.album(user_a, name, artist, saved)
    if a is None:
        return None
    b = albumpage.album(user_b, name, artist, saved)
    result = {"kind": "album", "a": name_a, "b": name_b, "never": b is None}
    if b is None:
        return result
    if not a["known_order"]:
        # No real tracklist: estimate ONE album for both (every song either has played from it),
        # otherwise a "full listen" would need a different number of songs for each person.
        both = [s["name"] for s in a["songs"]] + [s["name"] for s in b["songs"]]
        a = albumpage.album(user_a, name, artist, saved, also=both)
        b = albumpage.album(user_b, name, artist, saved, also=both)
    num = lambda tiles, label: tiles[[t[0] for t in tiles].index(label)][1]
    hours = lambda d: sum(s["hours"] for s in d["songs"])
    plays = lambda d: int(num(d["tiles"], "Plays").replace(",", ""))
    result["rows"] = [
        pair("Listening time", hours(a), hours(b), lambda v: f"{v:,.1f} h" if v < 10 else f"{v:,.0f} h"),
        pair("Plays", plays(a), plays(b), lambda v: f"{v:,}"),
        pair("Full listens", a["one_go"]["full"], b["one_go"]["full"], lambda v: f"{v:,}"),
        pair("Played as an album", a["one_go"]["here"], b["one_go"]["here"], lambda v: f"{v}%"),
        pair("Skipped", a["skips"]["rate"], b["skips"]["rate"], lambda v: f"{v}%", more_is="none"),
        pair("Rank", a["rank"], b["rank"], lambda v: f"#{v:,}", more_is="less"),
    ]
    fav = lambda d: max(d["songs"], key=lambda s: s["plays"], default=None)
    fa, fb = fav(a), fav(b)
    result["favourites"] = {"a": fa and fa["name"], "b": fb and fb["name"],
                            "same": bool(fa and fb and albumpage.clean(fa["name"]) == albumpage.clean(fb["name"]))}
    # Song by song, in tracklist order when known (else A's order). Each bar = share of that
    # person's plays of the album, so a heavy and a light listener compare fairly.
    b_by = {albumpage.clean(s["name"]): s for s in b["songs"]}
    tot_a = sum(s["plays"] for s in a["songs"]) or 1
    tot_b = sum(s["plays"] for s in b["songs"]) or 1
    rows = []
    for s in a["songs"]:
        o = b_by.get(albumpage.clean(s["name"]), {"plays": 0})
        rows.append({"n": s["n"], "name": s["name"], "track": s["track"],
                     "pa": 100 * s["plays"] / tot_a, "pb": 100 * o["plays"] / tot_b,
                     "ca": s["plays"], "cb": o["plays"]})
    top = max([max(r["pa"], r["pb"]) for r in rows] or [1]) or 1
    for r in rows:
        r["wa"], r["wb"] = round(100 * r["pa"] / top, 1), round(100 * r["pb"] / top, 1)
    result["tracks"] = rows
    result["known_order"] = a["known_order"]
    return result
