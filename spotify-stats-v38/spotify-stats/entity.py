"""
Stats for one song or one artist, plus search.

Many of these need the ORDER of plays (what came before and after a song, how
many times in a row), so each person's whole history is loaded once into a
compact "timeline" in memory, and every song or artist page works from that.

The timeline uses the `array` module rather than lists of objects: an array of
plain numbers takes a few bytes per play instead of ~100, so ten years of
listening (~300,000 plays) fits in about 5 MB.
"""
import math
import statistics
from array import array
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache

from diskcache import disk_cached

from db import connect
from stats import LOCAL_TZ, MIN_PLAY_MS, PICKED, PLAYED_ON, latest_play_id

SESSION_GAP_S = 10 * 60      # plays more than 10 minutes apart aren't "back to back"
BANDS = [  # time-of-day bands: (label, first hour, last hour, phrase)
    ("late night", 0, 5, "after midnight"),
    ("morning", 6, 11, "in the morning"),
    ("afternoon", 12, 17, "in the afternoon"),
    ("evening", 18, 23, "in the evening"),
]


# ------------------------------------------------------------------ the timeline

@lru_cache(maxsize=None)
def utc_offset(hour_key):
    """UK clock offset from UTC (0 or 3600 s) for one hour of history. Cached, so the
    slow time-zone lookup runs once per hour of history, not once per play."""
    moment = datetime.fromtimestamp(hour_key * 3600, timezone.utc)
    return int(moment.astimezone(LOCAL_TZ).utcoffset().total_seconds())


def local_ts(ts):
    """A UTC timestamp shifted to UK clock time (still a plain number)."""
    return ts + utc_offset(int(ts // 3600))


class Timeline:
    """One person's plays in time order, as parallel arrays (position i = the i-th play).

    Songs with the same name by the same artist count as ONE song: Spotify gives a
    single and its album version different IDs, but to you it's the same song.
    """

    def __init__(self, cursor):
        self.track_ids, self.track_info = [], []        # song index -> a Spotify ID / (name, artist, album)
        index_of, song_of = {}, {}                       # Spotify ID -> song index; (name, artist) -> song index
        self.track = array("I")         # which song
        self.end = array("d")           # when it ended, seconds since 1970 (UTC)
        self.ms = array("I")            # how long it was listened to
        self.kind = array("b")          # how it started: 1 you put it on, 2 came on by itself, 3 other, 0 unknown
        self.skip = array("b")          # 1 skipped (pressed next), 0 not, -1 unknown (poller rows)
        self.shuffle = array("b")       # 1 shuffle on, 0 off, -1 unknown (poller rows)
        # Which ALBUM each play came from. Kept per play, not per song: a single and the album
        # version are one song (above) but count towards different albums.
        self.album = array("I")
        self.album_info = []                             # album index -> (album name, artist)
        album_of, album_raw = {}, {}

        # Rows are read one at a time from the database (never all at once), and the
        # database has already turned the date into a number, which is much faster
        # than parsing ~300,000 date strings in Python.
        for track_id, name, artist, album, end, ms, rs, re_, shuffle in cursor:
            t = index_of.get(track_id)
            if t is None:
                key = ((name or "").lower(), artist)
                t = song_of.get(key)
                if t is None:
                    t = song_of[key] = len(self.track_ids)
                    self.track_ids.append(track_id)
                    self.track_info.append((name, artist, album))
                index_of[track_id] = t
            self.track.append(t)
            self.end.append(end)
            self.ms.append(ms or 0)
            self.kind.append(0 if rs is None else 1 if rs in PICKED else 2 if rs in PLAYED_ON else 3)
            self.skip.append(-1 if re_ is None else int(re_ == "fwdbtn"))
            self.shuffle.append(-1 if shuffle is None else int(bool(shuffle)))
            a = album_raw.get((album, artist))           # fast path: exactly as stored
            if a is None:
                akey = ((album or "").lower(), artist)  # "Igor" and "IGOR" are the same album
                a = album_of.get(akey)
                if a is None:
                    a = album_of[akey] = len(self.album_info)
                    self.album_info.append((album or "", artist))
                album_raw[(album, artist)] = a
            self.album.append(a)

        self.index_of = index_of
        self.song_of = song_of                           # (lower-case song name, artist) -> song index
        self.album_of = album_of                         # (lower-case album name, artist) -> album index
        # Positions of every play of each song / artist, and totals for ranking.
        self.by_track = defaultdict(list)
        self.by_artist = defaultdict(list)
        self.by_album = defaultdict(list)
        self.track_ms, self.artist_ms, self.album_ms = Counter(), Counter(), Counter()
        band_ms, weekend_ms, self.total_ms = Counter(), 0, 0
        known, skips = 0, 0
        info, track, end, msa, skip, albums = self.track_info, self.track, self.end, self.ms, self.skip, self.album
        album_track_ms = Counter()
        self.by_day = defaultdict(list)      # local day (days since 1970) -> real listens that day
        self.first_listen = {}               # song -> position of its first ever real listen
        for i in range(len(track)):
            t = track[i]
            artist = info[t][1]
            self.by_track[t].append(i)
            self.by_artist[artist].append(i)
            self.by_album[albums[i]].append(i)
            if skip[i] >= 0:
                known += 1
                skips += skip[i]
            ms = msa[i]
            if ms >= MIN_PLAY_MS:
                self.track_ms[t] += ms
                self.artist_ms[artist] += ms
                self.album_ms[albums[i]] += ms
                album_track_ms[(albums[i], t)] += ms
                self.total_ms += ms
                lt = local_ts(end[i])
                self.by_day[int(lt // 86400)].append(i)
                self.first_listen.setdefault(t, i)
                band_ms[band_of(int(lt // 3600) % 24)] += ms
                if (int(lt // 86400) + 3) % 7 >= 5:      # 1 Jan 1970 was a Thursday (weekday 3)
                    weekend_ms += ms
        # Your normal listening, to compare a song or artist against.
        self.band_share = {b: band_ms[b] / (self.total_ms or 1) for b, *_ in BANDS}
        self.weekend_share = weekend_ms / (self.total_ms or 1)
        self.skip_rate = skips / known if known else None
        # Each artist's most-played song: the track their photo is looked up through.
        self.artist_best = {}
        for t, ms in self.track_ms.most_common():
            self.artist_best.setdefault(info[t][1], t)
        self.track_rank = {t: n + 1 for n, (t, _) in enumerate(self.track_ms.most_common())}
        self.artist_rank = {a: n + 1 for n, (a, _) in enumerate(self.artist_ms.most_common())}
        self.album_rank = {a: n + 1 for n, (a, _) in enumerate(self.album_ms.most_common())}
        # Each album's most-played song: its cover is looked up through it.
        self.album_best = {}
        for (a, t), _ in album_track_ms.most_common():
            self.album_best.setdefault(a, t)

    def local(self, i):
        """When play i happened, in UK time (no time zone attached)."""
        return datetime(1970, 1, 1) + timedelta(seconds=local_ts(self.end[i]))

    def start(self, i):
        return self.end[i] - self.ms[i] / 1000

    def listened(self, i):
        return self.ms[i] >= MIN_PLAY_MS

    def neighbour(self, i, step):
        """The nearest real listen (30 s+) before (step=-1) or after (step=+1) play i,
        if it was part of the same listening session; skipped songs in between are passed over."""
        j = i + step
        while 0 <= j < len(self.track) and not self.listened(j):
            j += step
        if not 0 <= j < len(self.track):
            return None
        gap = self.start(i) - self.end[j] if step < 0 else self.start(j) - self.end[i]
        return j if gap <= SESSION_GAP_S else None


@lru_cache(maxsize=24)
def band_of(hour):
    return next(label for label, lo, hi, _ in BANDS if lo <= hour <= hi)


def timeline(user_id):
    return _timeline(user_id, latest_play_id(user_id))


@lru_cache(maxsize=4)
@disk_cached(versions=1)
def _timeline(user_id, latest_play_id):
    with connect() as conn:
        cursor = conn.execute(
            """SELECT track_id, track_name, artist_name, album_name,
                      (julianday(played_at) - 2440587.5) * 86400.0,   -- the date as seconds since 1970
                      listened_ms, reason_start, reason_end, shuffle
               FROM plays_best WHERE user_id = ? AND track_id IS NOT NULL
               ORDER BY played_at""",
            (user_id,),
        )
        return Timeline(cursor)


# ------------------------------------------------------------------ search

def search(user_id, query, songs=6, artists=4, albums=4):
    """Songs, artists and albums whose name contains the query, most played first."""
    q = query.strip().lower()
    if len(q) < 2:
        return {"songs": [], "artists": [], "albums": []}
    tl = timeline(user_id)
    song_hits = [t for t, (name, artist, _) in enumerate(tl.track_info)
                 if q in (name or "").lower() and tl.track_ms[t]]
    song_hits.sort(key=lambda t: -tl.track_ms[t])
    artist_hits = [a for a in tl.artist_ms if q in (a or "").lower()]
    artist_hits.sort(key=lambda a: -tl.artist_ms[a])
    album_hits = [a for a, (name, _) in enumerate(tl.album_info) if q in name.lower() and tl.album_ms[a]]
    album_hits.sort(key=lambda a: -tl.album_ms[a])
    return {
        "albums": [{"name": tl.album_info[a][0], "artist": tl.album_info[a][1],
                    "hours": round(tl.album_ms[a] / 3_600_000, 1),
                    "track": tl.track_ids[tl.album_best[a]]} for a in album_hits[:albums]],
        "songs": [{"track": tl.track_ids[t], "name": tl.track_info[t][0], "artist": tl.track_info[t][1],
                   "plays": sum(1 for i in tl.by_track[t] if tl.listened(i))} for t in song_hits[:songs]],
        "artists": [{"name": a, "hours": round(tl.artist_ms[a] / 3_600_000),
                     "track": tl.track_ids[tl.artist_best[a]]} for a in artist_hits[:artists]],
    }


# ------------------------------------------------------------------ shared pieces

def fmt_date(d):
    return f"{d.day} {d:%b %Y}"


def monthly_chart(tl, positions):
    """Plays per month from the first to the last month, as bars for an SVG chart."""
    months = Counter()
    for i in positions:
        if tl.listened(i):
            when = tl.local(i)
            months[(when.year, when.month)] += 1
    if not months:
        return None
    (y, m), last = min(months), max(months)
    keys = []
    while (y, m) <= last:
        keys.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    peak_key = max(months, key=months.get)
    peak = months[peak_key]

    W, H, LEFT, RIGHT, TOP, BOTTOM = 960, 220, 40, 12, 16, 28
    plot_w, plot_h = W - LEFT - RIGHT, H - TOP - BOTTOM
    slot = plot_w / len(keys)
    gap = 2 if slot > 6 else 0.5                       # the 2px surface gap, unless bars get very thin
    # Axis steps that read naturally: 1, 2, 5, 10, 20, 50 ... (not 77, 154, 231).
    rough = peak / 4
    magnitude = 10 ** math.floor(math.log10(rough)) if rough >= 1 else 1
    step = next(m * magnitude for m in (1, 2, 5, 10) if m * magnitude >= rough)
    step = max(1, int(step))
    nice = step * math.ceil(peak / step)
    bars = []
    for n, k in enumerate(keys):
        v = months.get(k, 0)
        # A month with any plays gets at least a sliver, so quiet months next to one
        # huge spike still show up (hover gives the exact number).
        h = max(plot_h * v / nice, 1.5) if v else 0
        label = datetime(k[0], k[1], 1).strftime("%b %Y")
        bars.append({"x": round(LEFT + n * slot + gap / 2, 2), "w": round(max(slot - gap, 0.6), 2),
                     "y": round(TOP + plot_h - h, 2), "h": round(h, 2), "peak": k == peak_key,
                     "tip": f"{label} · {v} play{'s' if v != 1 else ''}", "slot_x": round(LEFT + n * slot, 2)})
    years = sorted({k[0] for k in keys})
    year_step = max(1, math.ceil(len(years) / 8))       # at most ~8 year labels
    x_ticks = [(str(yr), round(LEFT + keys.index((yr, 1)) * slot, 1) if (yr, 1) in keys else LEFT)
               for yr in years[::year_step]]
    return {
        "w": W, "h": H, "left": LEFT, "right": W - RIGHT, "bottom": TOP + plot_h, "slot_w": round(slot, 2),
        "bars": bars,
        "y_ticks": [(v, round(TOP + plot_h * (1 - v / nice), 1)) for v in range(0, nice + 1, step)],
        "x_ticks": x_ticks,
        "peak_label": datetime(peak_key[0], peak_key[1], 1).strftime("%B %Y"),
        "peak_plays": peak,
    }


def when_you_play(tl, positions, min_plays=10):
    """Is this a late-night song? A weekend artist? Compared with your normal listening."""
    plays = [i for i in positions if tl.listened(i)]
    if len(plays) < min_plays:
        return None
    total = sum(tl.ms[i] for i in plays)
    band_ms, weekend = Counter(), 0
    for i in plays:
        when = tl.local(i)
        band_ms[band_of(when.hour)] += tl.ms[i]
        weekend += tl.ms[i] if when.weekday() >= 5 else 0
    best = max(BANDS, key=lambda b: (band_ms[b[0]] / total) / (tl.band_share[b[0]] or 1))
    ratio = (band_ms[best[0]] / total) / (tl.band_share[best[0]] or 1)
    weekend_ratio = (weekend / total) / (tl.weekend_share or 1)
    return {
        "band": best[0] if ratio >= 1.3 else None,
        "band_text": (f"{ratio:.1f}× your usual share of listening {best[3]}" if ratio >= 1.3
                      else "spread across the day like the rest of your listening"),
        "bands": [{"label": label.capitalize(), "pct": round(100 * band_ms[label] / total),
                   "usual": round(100 * tl.band_share[label])} for label, *_ in BANDS],
        "weekend_text": (f"{weekend_ratio:.1f}× your usual share at weekends" if weekend_ratio >= 1.3
                         else "mostly a weekday thing" if weekend_ratio <= 0.7
                         else "weekdays and weekends alike"),
    }


def treatment(tl, positions):
    """How you treat it: skip rate vs your average, how far in you skip, how often you finish."""
    known = [i for i in positions if tl.skip[i] >= 0]
    if len(known) < 5 or tl.skip_rate is None:
        return None
    skipped = [i for i in known if tl.skip[i] == 1]
    rate = len(skipped) / len(known)
    return {
        "rate": round(100 * rate),
        "usual": round(100 * tl.skip_rate),
        "verdict": ("you skip it far less than most songs" if rate < tl.skip_rate - 0.15
                    else "you skip it far more than most songs" if rate > tl.skip_rate + 0.15
                    else "about as often as everything else"),
        "median_s": round(statistics.median(tl.ms[i] / 1000 for i in skipped)) if skipped else None,
        "appearances": len(known),
    }


def biggest_day(tl, positions):
    days = Counter(tl.local(i).date() for i in positions if tl.listened(i))
    if not days:
        return None
    day, n = days.most_common(1)[0]
    return {"date": fmt_date(day), "plays": n}


def comeback(tl, positions):
    """The longest break between two plays, if you came back after at least 3 months."""
    plays = [i for i in positions if tl.listened(i)]
    best = None
    for a, b in zip(plays, plays[1:]):
        gap = tl.end[b] - tl.end[a]
        if gap >= 90 * 86400 and (best is None or gap > best[0]):
            best = (gap, a, b)
    if not best:
        return None
    gap, a, b = best
    months = round(gap / (30.44 * 86400))
    return {"months": months, "from": tl.local(a).strftime("%b %Y"), "to": tl.local(b).strftime("%b %Y")}


def song_links(tl, counter, who, n=10):
    """Neighbouring songs, ready for a list: how many times, and a bar relative to the top one."""
    top = counter.most_common(n)
    biggest = top[0][1] if top else 1
    return [{"track": tl.track_ids[t], "name": tl.track_info[t][0], "artist": tl.track_info[t][1],
             "fig": f"{c}×", "bar": round(100 * c / biggest)} for t, c in top]


# ------------------------------------------------------------------ one song

def song(user_id, track_id):
    tl = timeline(user_id)
    t = tl.index_of.get(track_id)
    if t is None or not tl.track_ms[t]:
        return None
    name, artist, album = tl.track_info[t]
    positions = tl.by_track[t]
    plays = [i for i in positions if tl.listened(i)]
    first, last = plays[0], plays[-1]

    # Back to back: the longest run of this song in a row, and its neighbours.
    best_run, run, run_end = 1, 1, plays[0]
    before, after = Counter(), Counter()
    for i in plays:
        prev = tl.neighbour(i, -1)
        nxt = tl.neighbour(i, +1)
        if prev is not None and tl.track[prev] == t:
            run += 1
            if run > best_run:
                best_run, run_end = run, i
        else:
            run = 1
        if prev is not None and tl.track[prev] != t:
            before[tl.track[prev]] += 1
        if nxt is not None and tl.track[nxt] != t:
            after[tl.track[nxt]] += 1

    first_kind = tl.kind[first]
    started = {1: "You put it on yourself", 2: "It came on by itself", 3: "It started another way"}.get(first_kind)
    artist_share = tl.track_ms[t] / (tl.artist_ms[artist] or 1)
    return {
        "track": track_id, "name": name, "artist": artist, "album": album,
        "rank": tl.track_rank[t], "of": len(tl.track_rank),
        "artist_share": round(100 * artist_share),
        "tiles": [
            ("Plays", f"{len(plays):,}"),
            ("Listening time", f"{tl.track_ms[t] / 3_600_000:,.1f} h"),
            ("First played", fmt_date(tl.local(first))),
            ("Last played", fmt_date(tl.local(last))),
        ],
        "first": {"date": fmt_date(tl.local(first)), "started": started,
                  "found": "found you" if first_kind == 2 else "you found it" if first_kind == 1 else None},
        "history": monthly_chart(tl, positions),
        "biggest_day": biggest_day(tl, positions),
        "loop": {"count": best_run, "date": fmt_date(tl.local(run_end))} if best_run >= 3 else None,
        "comeback": comeback(tl, positions),
        "when": when_you_play(tl, positions),
        "treatment": treatment(tl, positions),
        "before": song_links(tl, before, user_id),
        "after": song_links(tl, after, user_id),
    }


# ------------------------------------------------------------------ one artist

def artist(user_id, name):
    tl = timeline(user_id)
    if not tl.artist_ms.get(name):
        return None
    positions = tl.by_artist[name]
    plays = [i for i in positions if tl.listened(i)]
    first = plays[0]

    songs_ms, songs_plays = Counter(), Counter()
    albums_ms = Counter()
    for i in plays:
        songs_ms[tl.track[i]] += tl.ms[i]
        songs_plays[tl.track[i]] += 1
        albums_ms[tl.album_info[tl.album[i]][0]] += tl.ms[i]
    best_track = songs_ms.most_common(1)[0][0]

    # The first month after discovering them: did it take off straight away?
    month_after = sum(1 for i in plays if tl.end[i] - tl.end[first] <= 30 * 86400)

    # Their most skipped song (needs a few appearances to mean anything).
    seen, skipped = Counter(), Counter()
    for i in positions:
        if tl.skip[i] >= 0:
            seen[tl.track[i]] += 1
            skipped[tl.track[i]] += tl.skip[i]
    candidates = [s for s in seen if seen[s] >= 8]
    worst = max(candidates, key=lambda s: skipped[s] / seen[s]) if candidates else None

    years = Counter()
    for i in plays:
        years[tl.local(i).year] += tl.ms[i]
    peak_year, peak_ms = years.most_common(1)[0]

    top_songs = songs_ms.most_common(10)
    biggest = top_songs[0][1]
    first_kind = tl.kind[first]
    return {
        "name": name,
        "art_track": tl.track_ids[best_track],
        "rank": tl.artist_rank[name], "of": len(tl.artist_rank),
        "share": round(100 * tl.artist_ms[name] / (tl.total_ms or 1), 1),
        "tiles": [
            ("Listening time", f"{tl.artist_ms[name] / 3_600_000:,.0f} h"),
            ("Plays", f"{len(plays):,}"),
            ("Songs played", f"{len(songs_ms):,}"),
            ("First listen", fmt_date(tl.local(first))),
        ],
        "first": {
            "date": fmt_date(tl.local(first)),
            "song": tl.track_info[tl.track[first]][0],
            "song_track": tl.track_ids[tl.track[first]],
            "found": "found you" if first_kind == 2 else "you found them" if first_kind == 1 else None,
            "month_after": month_after,
        },
        "peak_year": {"year": peak_year, "hours": round(peak_ms / 3_600_000)},
        "history": monthly_chart(tl, positions),
        "biggest_day": biggest_day(tl, positions),
        "comeback": comeback(tl, positions),
        "when": when_you_play(tl, positions, min_plays=20),
        "treatment": treatment(tl, positions),
        "worst": ({"name": tl.track_info[worst][0], "track": tl.track_ids[worst],
                   "pct": round(100 * skipped[worst] / seen[worst])}
                  if worst is not None and skipped[worst] / seen[worst] >= 0.5 else None),
        "top_songs": [{"name": tl.track_info[s][0], "track": tl.track_ids[s], "fig": f"{ms / 3_600_000:,.1f} h",
                       "bar": round(100 * ms / biggest), "plays": songs_plays[s]} for s, ms in top_songs],
        "albums": [{"name": a or "Unknown album", "fig": f"{ms / 3_600_000:,.1f} h"} for a, ms in albums_ms.most_common(5)],
    }


# ------------------------------------------------------------------ on this day

EPOCH = datetime(1970, 1, 1).date()


def on_this_day(user_id, month, day, today):
    """What you were listening to on this date in each earlier year, newest first."""
    tl = timeline(user_id)
    years = []
    first_year = tl.local(0).year if len(tl.track) else today.year
    for year in range(today.year - (1 if (month, day) > (today.month, today.day) else 0), first_year - 1, -1):
        try:
            date = datetime(year, month, day).date()
        except ValueError:                         # 29 Feb in a non-leap year
            continue
        if date >= today:                          # only the past
            continue
        plays = tl.by_day.get((date - EPOCH).days, [])
        if not plays:
            continue
        songs, ms_total = Counter(), 0
        for i in plays:
            songs[tl.track[i]] += 1
            ms_total += tl.ms[i]
        # A typical day that year (the median of days with any listening), for comparison.
        year_days = [sum(tl.ms[i] for i in tl.by_day[d])
                     for d in range((datetime(year, 1, 1).date() - EPOCH).days, (datetime(year, 12, 31).date() - EPOCH).days + 1)
                     if d in tl.by_day]
        typical = statistics.median(year_days) if year_days else 0
        new_songs = [tl.track[i] for i in plays if tl.first_listen.get(tl.track[i]) == i]
        top = songs.most_common(5)
        biggest = top[0][1]
        years.append({
            "year": year,
            "ago": today.year - year,
            "date": f"{date:%A} {date.day} {date:%b %Y}",
            "minutes": round(ms_total / 60_000),
            "vs_typical": (ms_total / typical) if typical else None,
            "songs": [{"track": tl.track_ids[t], "name": tl.track_info[t][0], "artist": tl.track_info[t][1],
                       "fig": f"{n}×", "bar": round(100 * n / biggest)} for t, n in top],
            "new": [{"track": tl.track_ids[t], "name": tl.track_info[t][0], "artist": tl.track_info[t][1]}
                    for t in new_songs[:6]],
            "new_count": len(new_songs),
        })
    return years
