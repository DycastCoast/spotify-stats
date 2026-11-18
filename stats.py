"""
Stats calculations for the website. Kept separate from app.py so that app.py only
deals with web pages, and this file only deals with numbers ("separation of concerns").
"""
import math
import statistics
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache

from diskcache import disk_cached
from zoneinfo import ZoneInfo

from db import connect

# Spotify stores times in UTC. People think in local time, so convert before
# working out "what hour" or "what day" a play happened. Using a real time zone
# (not "+0") means British Summer Time is handled correctly.
LOCAL_TZ = ZoneInfo("Europe/London")

MIN_PLAY_MS = 30_000          # plays shorter than this don't count as a "listen"
SLOT_MINUTES = 15             # the average-day chart's resolution
SLOTS = 24 * 60 // SLOT_MINUTES

# How a song started -> did you choose it, or did it come on by itself?
PICKED = {"clickrow", "playbtn", "backbtn"}          # you chose it (or went back to it)
PLAYED_ON = {"trackdone", "fwdbtn"}                  # next in queue / playlist / autoplay

# Diverging colours for the average-day chart: blue = quieter than your average,
# grey = about average, orange/red = busier than average.
LOW, MID, HIGH, PEAK = "#3987e5", "#8f8d86", "#d95926", "#e66767"

SKIP_BUCKETS = [  # (upper limit in seconds, label)
    (5, "Under 5 s"), (10, "5–10 s"), (20, "10–20 s"), (30, "20–30 s"),
    (60, "30 s–1 min"), (120, "1–2 min"), (math.inf, "Over 2 min"),
]


# ------------------------------------------------------------------ choices

def people():
    """Everyone who has at least one play, for the person picker."""
    with connect() as conn:
        return conn.execute(
            """
            SELECT u.id, u.display_name, COUNT(*) AS plays
            FROM users u JOIN plays p ON p.user_id = u.id
            GROUP BY u.id ORDER BY u.display_name
            """
        ).fetchall()


def years(user_id):
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT substr(played_at, 1, 4) AS y FROM plays WHERE user_id = ? ORDER BY y DESC",
            (user_id,),
        ).fetchall()
    return [r["y"] for r in rows]


# ------------------------------------------------------------------ helpers

@lru_cache(maxsize=None)
def device_type(platform):
    """Spotify's platform strings are messy ("Android OS 9 API 28 (Samsung...)").
    Boil them down to a few readable groups.

    @lru_cache remembers the answer for each string, so the same platform text
    seen 100,000 times is only worked out once."""
    p = (platform or "").lower()
    if "ipad" in p:
        return "Tablet"
    if "android" in p or "ios" in p or "iphone" in p:
        return "Phone"
    if "windows" in p or "osx" in p or "mac" in p or "linux" in p:
        return "Computer"
    if "web_player" in p or "web player" in p:
        return "Web player"
    if any(word in p for word in ("cast", "sonos", "alexa", "echo", "speaker", "tv", "partner", "google")):
        return "Speaker / TV"
    if "playstation" in p or "xbox" in p or "ps4" in p or "ps5" in p:
        return "Console"
    return "Other"


def level(value, cap):
    """Turn a number into a colour level 0-5 (0 means nothing at all)."""
    if value <= 0 or cap <= 0:
        return 0
    return min(5, max(1, math.ceil(5 * value / cap)))


def percentile(values, pct):
    """Value below which pct% of the values fall."""
    if not values:
        return 0
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))]


def fmt_hours(ms):
    hours = ms / 3_600_000
    return f"{hours:,.0f} h" if hours >= 10 else f"{hours:,.1f} h"


def fmt_minutes(ms):
    return f"{ms / 60_000:,.0f} min"


def slot_label(i):
    minutes = i * SLOT_MINUTES
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def minutes_into_day(t):
    """10:30:15.5 -> 630.258... (exact, fractions of a second included)."""
    return t.hour * 60 + t.minute + (t.second + t.microsecond / 1_000_000) / 60


def add_to_slots(per_day, start, end):
    """Spread one play across the 15-minute slots it actually covered.

    A song from 21:58 to 22:02 puts 2 minutes in the 21:45 slot and 2 in the
    22:00 slot, instead of all 4 landing wherever it happened to end.
    """
    if start.date() == end.date():
        pieces = [(end.date(), minutes_into_day(start), minutes_into_day(end))]
    else:
        # The play crossed midnight: split it into the two days.
        pieces = [(start.date(), minutes_into_day(start), 24 * 60),
                  (end.date(), 0, minutes_into_day(end))]
    for day, s, e in pieces:
        slots = per_day[day]
        for k in range(int(s // SLOT_MINUTES), min(SLOTS - 1, int(e // SLOT_MINUTES)) + 1):
            piece = min(e, (k + 1) * SLOT_MINUTES) - max(s, k * SLOT_MINUTES)
            if piece > 0:
                slots[k] += piece * 60_000


def smooth(values, width=3):
    """Average each point with its neighbours (wrapping round midnight) so the
    line shows the pattern rather than random 15-minute jitter."""
    n, half = len(values), width // 2
    return [sum(values[(i + k) % n] for k in range(-half, half + 1)) / width for i in range(n)]


def ranked(counter, label, n=10, unit="", art=None, link=None):
    """Top n items of a Counter as dicts ready for the template.
    label(key) -> (name, subtitle). The bar is relative to the top item.
    art(key) -> {"kind", "track"}: which picture to show, looked up through which track."""
    top = counter.most_common(n)
    biggest = top[0][1] if top else 1
    return [
        {"name": label(key)[0], "artist": label(key)[1], "count": count,
         "fig": f"{count:,}{unit}", "bar": round(100 * count / biggest),
         "art": art(key) if art else None, "link": link(key) if link else None}
        for key, count in top
    ]


def ranked_time(counter, label, n=10, art=None, link=None):
    """Like ranked(), but the counts are milliseconds shown as hours."""
    top = counter.most_common(n)
    biggest = top[0][1] if top else 1
    return [
        {"name": label(key)[0], "artist": label(key)[1], "count": ms,
         "fig": fmt_hours(ms), "bar": round(100 * ms / biggest),
         "art": art(key) if art else None, "link": link(key) if link else None}
        for key, ms in top
    ]


def devices(user_id):
    """Device groups this person has used (export data only), for the filter."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT DISTINCT platform FROM plays WHERE user_id = ? AND platform IS NOT NULL", (user_id,)
        ).fetchall()
    return sorted({device_type(r["platform"]) for r in rows})


def longest_streak(days):
    """Most consecutive days with listening, from a set of dates."""
    best = run = 0
    previous = None
    for d in sorted(days):
        run = run + 1 if previous and (d - previous).days == 1 else 1
        best = max(best, run)
        previous = d
    return best


# ------------------------------------------------------------------ the page

def latest_play_id(user_id):
    """A cheap "has anything changed?" key for the caches: the newest play's row ID (changes
    when plays are added) plus the person's data_version (goes up when plays are deleted).
    None if they have no plays at all."""
    with connect() as conn:
        newest, version = conn.execute(
            "SELECT (SELECT MAX(id) FROM plays WHERE user_id = ?), (SELECT data_version FROM users WHERE id = ?)",
            (user_id, user_id)).fetchone()
    return None if newest is None else f"{newest}.{version or 0}"


def overview(user_id, year=None, device=None):
    """Everything the overview page needs. Cached until the person gets new plays."""
    return _overview(user_id, year, device, latest_play_id(user_id))


def year_total_hours(user_id, year):
    """Hours listened (30 s+ plays) in one year, for the hero's comparison."""
    return _year_total_hours(user_id, str(year), latest_play_id(user_id))


@lru_cache(maxsize=64)
def _year_total_hours(user_id, year, latest_play_id):
    with connect() as conn:
        ms = conn.execute(
            """SELECT SUM(listened_ms) FROM plays_best
               WHERE user_id = ? AND played_at >= ? AND played_at < ? AND listened_ms >= ?""",
            (user_id, f"{year}-01-01", f"{int(year) + 1}-01-01", MIN_PLAY_MS),
        ).fetchone()[0]
    return (ms or 0) / 3_600_000


@lru_cache(maxsize=32)
@disk_cached(versions=1)
def _overview(user_id, year, device, latest_play_id):
    """The actual calculation, in one pass over the person's plays.

    lru_cache keeps the last 32 results keyed by the arguments. latest_play_id is
    only here so new plays give a new key, and therefore a fresh calculation
    (this is "cache invalidation", famously one of the hard problems).
    """
    sql = """
        SELECT played_at, listened_ms, track_id, track_name, artist_name, album_name,
               reason_start, reason_end, shuffle, platform
        FROM plays_best
        WHERE user_id = ?
    """
    params = [user_id]
    if year:
        # ISO dates sort as text, so a plain text comparison picks out the year.
        sql += " AND played_at >= ? AND played_at < ?"
        params += [f"{year}-01-01", f"{int(year) + 1}-01-01"]

    with connect() as conn:
        rows = conn.execute(sql, params).fetchall()

    total_ms, plays = 0, 0
    names = {}                                     # track_id -> (track, artist)
    daily = defaultdict(int)                       # local date -> ms
    per_day = defaultdict(lambda: [0.0] * SLOTS)   # local date -> ms in each 15-min slot
    artist_ms, track_ms, album_ms = Counter(), Counter(), Counter()
    artist_plays, track_plays, album_plays = Counter(), Counter(), Counter()
    track_album = {}                               # track_id -> (album, artist)

    start_ms = Counter()                           # picked / played_on / other -> ms
    shuffle_ms = 0
    picked_tracks, played_on_tracks = Counter(), Counter()

    behaviour_rows = 0
    skip_times = Counter()                         # bucket label -> skips
    skip_seconds = []
    track_seen, track_skips = Counter(), Counter()
    artist_seen, artist_skips = Counter(), Counter()
    device_ms, device_plays, device_skips = Counter(), Counter(), Counter()

    for r in rows:
        # Device filter: only exports record the device, so poller rows drop out.
        if device and (r["platform"] is None or device_type(r["platform"]) != device):
            continue

        # Local time, without the time zone attached, so the slot maths is simple.
        end = datetime.fromisoformat(r["played_at"].replace("Z", "+00:00")).astimezone(LOCAL_TZ).replace(tzinfo=None)
        ms = r["listened_ms"] or 0
        track, artist = r["track_id"], r["artist_name"]
        names.setdefault(track, (r["track_name"], artist))
        listened = ms >= MIN_PLAY_MS

        # --- listening totals: only plays of 30 s or more count
        if listened:
            total_ms += ms
            plays += 1
            daily[end.date()] += ms
            add_to_slots(per_day, end - timedelta(milliseconds=ms), end)
            album = (r["album_name"], artist)
            track_album[track] = album
            artist_ms[artist] += ms; artist_plays[artist] += 1
            track_ms[track] += ms; track_plays[track] += 1
            album_ms[album] += ms; album_plays[album] += 1

        # --- behaviour: only exports record how songs started and ended
        if r["reason_start"] is None:
            continue
        behaviour_rows += 1

        kind = ("picked" if r["reason_start"] in PICKED
                else "played_on" if r["reason_start"] in PLAYED_ON else "other")
        start_ms[kind] += ms                       # time-weighted, so quick skips barely count
        if kind == "played_on" and r["shuffle"]:
            shuffle_ms += ms
        if listened and kind == "picked":
            picked_tracks[track] += 1
        elif listened and kind == "played_on":
            played_on_tracks[track] += 1

        skipped = r["reason_end"] == "fwdbtn"      # you pressed "next"
        track_seen[track] += 1
        artist_seen[artist] += 1
        if skipped:
            track_skips[track] += 1
            artist_skips[artist] += 1
            seconds = ms / 1000
            skip_seconds.append(seconds)
            skip_times[next(label for limit, label in SKIP_BUCKETS if seconds < limit)] += 1

        d = device_type(r["platform"])
        device_ms[d] += ms
        device_plays[d] += 1
        device_skips[d] += skipped

    if not plays:
        return {"has_plays": False}

    track_label = lambda t: names[t]
    active_days = len(daily)

    # Pictures are looked up through a track: an album cover through the song, an
    # artist photo through their most-played song, an album through its most-played song.
    best_for_artist, best_for_album = {}, {}
    for t, ms in track_ms.most_common():                 # most played first, so the first one wins
        best_for_artist.setdefault(names[t][1], t)
        best_for_album.setdefault(track_album[t], t)
    song_art = lambda t: {"kind": "track", "track": t}
    artist_art = lambda a: {"kind": "artist", "track": best_for_artist[a]}
    album_art = lambda k: {"kind": "track", "track": best_for_album[k]}
    # Where a name in a list links to: its song page or artist page.
    song_link = lambda t: {"type": "song", "track": t}
    artist_link = lambda a: {"type": "artist", "name": a}
    album_link = lambda k: {"type": "album", "name": k[0], "artist": k[1]}
    result = {
        "has_plays": True,
        "total_hours": total_ms / 3_600_000,
        "first_day": min(daily),
        "last_day": max(daily),
        "tiles": [
            ("Plays", f"{plays:,}"),
            ("Artists", f"{len(artist_ms):,}"),
            ("Tracks", f"{len(track_ms):,}"),
            ("Per active day", fmt_minutes(total_ms / active_days)),
        ],
        "day_chart": build_day_chart(per_day),
        "calendar": build_calendar(daily, int(year) if year else None),
        # "What" tab: top lists, each by listening time and by plays (toggle).
        "top": {
            "artists": {"time": ranked_time(artist_ms, lambda a: (a, ""), art=artist_art, link=artist_link),
                        "plays": ranked(artist_plays, lambda a: (a, ""), unit=" plays", art=artist_art, link=artist_link)},
            "songs": {"time": ranked_time(track_ms, track_label, art=song_art, link=song_link),
                      "plays": ranked(track_plays, track_label, unit=" plays", art=song_art, link=song_link)},
            "albums": {"time": ranked_time(album_ms, lambda k: k, art=album_art, link=album_link),
                       "plays": ranked(album_plays, lambda k: k, unit=" plays", art=album_art, link=album_link)},
        },
        "starts": None,
        "skips": None,
        "device_split": None,
    }

    # ----- highlights: each one needs a comparison, or it doesn't appear
    hl_when, hl_what, hl_how = [], [], []
    dc = result["day_chart"]
    hl_when.append({"label": "Your peak time", "value": dc["peak_label"],
                    "detail": f"{dc['peak_ratio']:.1f}× your average for the day"})
    best_day = max(daily, key=daily.get)
    typical = statistics.median(daily.values())
    hl_when.append({"label": "Biggest day", "value": best_day.strftime("%d %b %Y"),
                    "detail": f"{fmt_hours(daily[best_day])}, {daily[best_day] / typical:.0f}× a typical day"})
    period_days = (max(daily) - min(daily)).days + 1
    hl_when.append({"label": "Longest streak", "value": f"{longest_streak(daily.keys())} days in a row", "count": True,
                    "detail": f"you listened on {active_days} of {period_days} days ({100 * active_days / period_days:.0f}%)"})

    top_artist, top_artist_ms = artist_ms.most_common(1)[0]
    hl_what.append({"label": "Top artist", "value": top_artist,
                    "detail": f"{100 * top_artist_ms / total_ms:.0f}% of all your listening"})
    top_track, top_track_plays = track_plays.most_common(1)[0]
    hl_what.append({"label": "Most played song", "value": names[top_track][0], "art": song_art(top_track),
                    "link": song_link(top_track),
                    "detail": f"{top_track_plays} plays, about once every {max(1, round(period_days / top_track_plays))} days"})

    if behaviour_rows:
        whole = sum(start_ms.values()) or 1
        total_skips = sum(track_skips.values())
        skip_rate = total_skips / behaviour_rows
        rate = lambda t: track_skips[t] / track_seen[t]

        # Hidden favourites: come on often, never skipped, but you rarely put them on yourself.
        hidden = sorted((t for t, n in played_on_tracks.items()
                         if n >= 10 and picked_tracks[t] <= 1 and rate(t) <= 0.05),
                        key=lambda t: -played_on_tracks[t])
        result["starts"] = {
            "segments": [
                {"label": label, "pct": round(100 * start_ms[key] / whole), "colour": colour,
                 "hours": fmt_hours(start_ms[key])}
                for label, key, colour in [
                    ("You put it on", "picked", "#3987e5"),
                    ("Came on by itself", "played_on", "#d95926"),
                    ("Other", "other", "#6b6a66"),
                ]
            ],
            "shuffle_pct": round(100 * shuffle_ms / (start_ms["played_on"] or 1)),
            "lists": {
                "picked": ranked(picked_tracks, track_label, unit="×", art=song_art, link=song_link),
                "played_on": ranked(played_on_tracks, track_label, unit="×", art=song_art, link=song_link),
                "hidden": ranked(Counter({t: played_on_tracks[t] for t in hidden}), track_label, unit="×",
                                 art=song_art, link=song_link),
            },
        }
        if hidden:
            h = hidden[0]
            yourself = "you've never put it on yourself" if picked_tracks[h] == 0 else "you've only put it on yourself once"
            hl_how.append({"label": "Hidden favourite", "value": names[h][0], "art": song_art(h), "link": song_link(h),
                           "detail": f"came on by itself {played_on_tracks[h]} times and you never skipped it, "
                                     f"but {yourself}"})

        most_skipped = sorted((t for t in track_seen if track_seen[t] >= 10),
                              key=lambda t: (-rate(t), -track_seen[t]))[:10]
        never_skipped = sorted((t for t in track_seen if track_seen[t] >= 15 and rate(t) <= 0.05),
                               key=lambda t: -(track_seen[t] - track_skips[t]))[:10]
        most_through = max((track_seen[t] - track_skips[t] for t in never_skipped), default=1)
        result["skips"] = {
            "rate": round(100 * skip_rate),
            "median_seconds": round(statistics.median(skip_seconds)) if skip_seconds else None,
            "timing": [
                {"label": label, "pct": round(100 * skip_times[label] / (total_skips or 1))}
                for _, label in SKIP_BUCKETS
            ],
            "lists": {
                "most": [{"name": names[t][0], "artist": names[t][1], "fig": f"{round(100 * rate(t))}%",
                          "link": {"type": "song", "track": t}, "art": song_art(t),
                          "bar": round(100 * rate(t)), "tip": f"skipped {track_skips[t]} of {track_seen[t]} times"}
                         for t in most_skipped],
                "never": [{"name": names[t][0], "artist": names[t][1],
                           "fig": f"{track_seen[t] - track_skips[t]}×",
                           "link": {"type": "song", "track": t}, "art": song_art(t), "art": song_art(t),
                           "bar": round(100 * (track_seen[t] - track_skips[t]) / most_through),
                           "tip": f"played through {track_seen[t] - track_skips[t]} of {track_seen[t]} times"}
                          for t in never_skipped],
            },
        }
        top_bucket = max(result["skips"]["timing"], key=lambda b: b["pct"])["pct"] or 1
        for b in result["skips"]["timing"]:
            b["bar"] = round(100 * b["pct"] / top_bucket)

        skip_artists = [a for a in artist_seen if artist_seen[a] >= 50]
        if skip_artists:
            worst = max(skip_artists, key=lambda a: artist_skips[a] / artist_seen[a])
            worst_rate = artist_skips[worst] / artist_seen[worst]
            if worst_rate > skip_rate + 0.15:          # only if it really stands out
                hl_how.append({"label": "Most skipped artist", "value": worst,
                               "detail": f"{100 * worst_rate:.0f}% of their songs skipped (you average {100 * skip_rate:.0f}%)"})

        if not device and len(device_ms) > 1:
            result["device_split"] = [
                {"name": name, "share": round(100 * device_ms[name] / whole),
                 "skip_rate": round(100 * device_skips[name] / device_plays[name])}
                for name, _ in device_ms.most_common()
                if device_ms[name] / whole >= 0.005      # hide devices under 0.5% of listening
            ]

    result["highlights"] = {"when": hl_when, "what": hl_what, "how": hl_how}
    return result


@lru_cache(maxsize=32)
@disk_cached(versions=1)
def discovery(user_id, year, latest_play_id):
    """Did each artist find you, or did you find them?

    For every artist, look at your first real listen (30 s+) to them. If that song
    came on by itself, they found you; if you put it on, you found them.
    "Regular" artists = 10+ listens, so one-off random plays don't count.
    Uses the whole history (a first listen is a first listen), then, for a single
    year, only artists whose first listen was in that year.
    """
    with connect() as conn:
        rows = conn.execute(
            """
            WITH firsts AS (
                SELECT artist_name, MIN(played_at) AS first, COUNT(*) AS listens
                FROM plays
                WHERE user_id = ? AND source = 'export' AND ms_played >= ?
                GROUP BY artist_name
            )
            SELECT f.artist_name, f.first, f.listens, p.reason_start
            FROM firsts f
            JOIN plays p ON p.user_id = ? AND p.source = 'export'
                        AND p.artist_name = f.artist_name AND p.played_at = f.first
            WHERE f.listens >= 10
            """,
            (user_id, MIN_PLAY_MS, user_id),
        ).fetchall()
    seen = {}
    for r in rows:                                 # one row per artist (ignore rare ties)
        if year and not r["first"].startswith(str(year)):
            continue
        seen.setdefault(r["artist_name"], r["reason_start"] in PLAYED_ON)
    if not seen:
        return None
    found_you = sum(seen.values())
    return {"total": len(seen), "found_you": found_you, "pct": round(100 * found_you / len(seen))}


# ------------------------------------------------------------ average day chart

# Chart geometry (SVG units; the SVG scales to fit its box).
W, H = 960, 280
LEFT, RIGHT, TOP, BOTTOM = 52, 12, 14, 30
PLOT_W, PLOT_H = W - LEFT - RIGHT, H - TOP - BOTTOM
# Percentile bands, paired 10/90, 15/85 ... 45/55. Drawn as faint stacked layers,
# so the haze is darkest where most weeks fall and fades towards the unusual ones.
BAND_PERCENTILES = [10, 15, 20, 25, 30, 35, 40, 45, 55, 60, 65, 70, 75, 80, 85, 90]


def build_day_chart(per_day):
    """The average day, three ways (all days, weekdays, weekends).

    For each 15-minute slot: the average listening across days (every day counts,
    including days with no listening, so it's a true "typical day"), and how much
    that varies from WEEK to week. Single days are too on/off to be useful (at
    22:00 you're either listening or not), but weekly averages show whether a
    habit is steady or comes and goes.

    The chart shows each slot as a RATIO to the person's overall average
    (x1 = normal for you, x2 = twice as much) on a doubling ("log") scale, where
    x0.5 sits as far below x1 as x2 sits above it. Minutes appear on hover.
    """
    first, last = min(per_day), max(per_day)
    all_days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    groups = {
        "all": all_days,
        "weekdays": [d for d in all_days if d.weekday() < 5],
        "weekends": [d for d in all_days if d.weekday() >= 5],
    }
    empty = [0.0] * SLOTS
    to_rate = 4 / 60_000      # ms in a 15-minute slot -> minutes per hour

    series = {}
    for key, days in groups.items():
        if not days:
            continue
        columns = list(zip(*(per_day.get(d, empty) for d in days)))   # one tuple per slot
        n = len(days)
        means = smooth([sum(c) / n * to_rate for c in columns])

        # Average each week separately (ISO week number), then look at the spread
        # of those weekly averages for every slot.
        weeks = defaultdict(list)
        for d in days:
            weeks[d.isocalendar()[:2]].append(per_day.get(d, empty))
        weekly = [[sum(col) / len(w) * to_rate for col in zip(*w)] for w in weeks.values()]
        slot_values = [sorted(week[i] for week in weekly) for i in range(SLOTS)]
        bands = {p: smooth([percentile(vals, p) for vals in slot_values], width=5) for p in BAND_PERCENTILES}

        series[key] = {"mean": means, "bands": bands, "days": n, "weeks": len(weekly)}

    # Everything is compared with the ALL-days average, so "weekends at x3"
    # really means three times your normal amount.
    baseline = statistics.fmean(series["all"]["mean"]) or 1e-9

    # Scale: powers of two that fit the lines (not the bands), so the line fills the chart.
    ratios = [m / baseline for v in series.values() for m in v["mean"]]
    lo_exp = max(-3, math.floor(math.log2(max(min(ratios), 1e-9))))   # never below x1/8
    hi_exp = max(1, math.ceil(math.log2(max(ratios))))
    lo_exp = min(lo_exp, -1)
    rmin, rmax = 2.0 ** lo_exp, 2.0 ** hi_exp
    span = hi_exp - lo_exp

    def pos(r):
        """0 at the bottom of the chart, 1 at the top (log scale, clipped)."""
        r = min(max(r, rmin), rmax)
        return (math.log2(r) - lo_exp) / span

    x = lambda i: LEFT + (i + 0.5) * PLOT_W / SLOTS
    y = lambda r: TOP + PLOT_H * (1 - pos(r))

    def band(low, high):
        upper = [f"{x(i):.1f},{y(v / baseline):.1f}" for i, v in enumerate(high)]
        lower = [f"{x(i):.1f},{y(v / baseline):.1f}" for i, v in reversed(list(enumerate(low)))]
        return " ".join(upper + lower)

    def ratio_text(r):
        return f"{r:.1f}×" if r >= 0.95 else f"{r:.2f}×"

    views = {}
    for key, v in series.items():
        mean, b = v["mean"], v["bands"]
        peak = max(mean) / baseline
        views[key] = {
            "line": " ".join(f"{x(i):.1f},{y(m / baseline):.1f}" for i, m in enumerate(mean)),
            # Outermost (10th-90th percentile) first, innermost (40th-60th) last.
            "bands": [band(b[lo], b[100 - lo]) for lo in BAND_PERCENTILES if lo < 50],
            # Colour: blue at the bottom, grey exactly at x1, red at the line's peak.
            "stops": [(0, LOW), (round(pos(1), 3), MID),
                      (round((pos(1) + pos(peak)) / 2, 3), HIGH), (round(max(pos(peak), pos(1) + 0.01), 3), PEAK), (1, PEAK)],
            "tips": [
                f"{slot_label(i)}–{slot_label((i + 1) % SLOTS)} · {ratio_text(m / baseline)} your average · "
                f"{m:.0f} min/h · most weeks {b[25][i]:.0f}–{b[75][i]:.0f}"
                for i, m in enumerate(mean)
            ],
            "days": v["days"],
        }

    tick_label = lambda e: f"×{2 ** e:g}" if e >= 0 else f"×1/{2 ** -e}"
    # Busiest hour of the average day (a full hour reads better than 15 minutes).
    hourly = [sum(series["all"]["mean"][h * 4:(h + 1) * 4]) / 4 for h in range(24)]
    peak_hour = max(range(24), key=lambda h: hourly[h])
    return {
        "peak_label": f"{peak_hour:02d}:00–{(peak_hour + 1) % 24:02d}:00",
        "peak_ratio": hourly[peak_hour] / baseline,
        "views": views,
        "baseline_label": f"×1 = your average, {baseline:.0f} min/h",
        "one_y": round(y(1), 1),
        "w": W, "h": H, "left": LEFT, "right": W - RIGHT, "top": TOP, "bottom": TOP + PLOT_H,
        "zero_y": round(y(rmin), 1), "max_y": round(y(rmax), 1),
        "slot_w": round(PLOT_W / SLOTS, 2),
        "slot_x": [round(LEFT + i * PLOT_W / SLOTS, 2) for i in range(SLOTS)],
        "y_ticks": [(tick_label(e), round(y(2.0 ** e), 1)) for e in range(lo_exp, hi_exp + 1)],
        "x_ticks": [(f"{h:02d}:00", round(LEFT + h * PLOT_W / 24, 1)) for h in range(0, 24, 3)],
    }


# ------------------------------------------------------------------ calendar

def build_calendar(daily, year):
    """A GitHub-style grid: one column per week, one row per weekday."""
    if not daily:
        return None
    if year is None:
        year = max(daily).year                     # "all time" shows the latest year
    first, last = date(year, 1, 1), date(year, 12, 31)
    start = first - timedelta(days=first.weekday())  # back to the Monday

    in_year = [v for d, v in daily.items() if d.year == year and v > 0]
    cap = percentile(in_year, 95)                  # one huge day shouldn't wash out the rest

    weeks, months = [], []
    day = start
    while day <= last:
        week = []
        for _ in range(7):
            if first <= day <= last:
                ms = daily.get(day, 0)
                week.append({"level": level(ms, cap),
                             "tip": f"{day.strftime('%a %d %b %Y')} · {fmt_minutes(ms)}"})
            else:
                week.append(None)                  # padding outside the year
            day += timedelta(days=1)
        firsts = [d for d in (day - timedelta(days=7 - i) for i in range(7)) if d.day == 1 and d.year == year]
        months.append(firsts[0].strftime("%b") if firsts else "")
        weeks.append(week)

    return {
        "year": year,
        "weeks": weeks,
        "months": months,
        "total": fmt_hours(sum(in_year)),
        "days": len(in_year),
    }
