"""
Your listening right now: the last couple of weeks, compared with your usual.

"Now" is the newest play on record (so it still makes sense if tracking paused).
- RECENT = the last 14 days (on repeat, pace) or 30 days (rising/fading, finds, hours).
- USUAL = the 365 days before those 30 days, per day, so a quiet or busy month
  doesn't look like a change in taste.
Works with live tracking data too: none of it needs skip or device information.
"""
import math
import statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from functools import lru_cache

from diskcache import disk_cached

import entity
import journey
from entity import fmt_date, local_ts, timeline
from stats import LOCAL_TZ, latest_play_id

DAY = 86400
REPEAT_DAYS = 14
RECENT_DAYS = 30
USUAL_DAYS = 365


NEW_DAYS = 30             # a song is "new" for 30 days after you first heard it
CLASSIC_DAYS = 365        # ...and a "classic" once you've known it for over a year
WARMUP_DAYS = 180         # at the start of anyone's history EVERYTHING looks new: leave it out of "usual"


def discovery_mode(tl, listened, now):
    """Are you discovering or sitting on classics? Every listen is labelled by how long the person
    had known that song at the time: new (< 30 days), this year (< 1 year) or classic (1 year+).
    Last 30 days vs their usual month, plus a month-by-month line of the "new" share."""
    first_ever = tl.end[listened[0]]
    artist_first = {}
    for i in listened:
        artist_first.setdefault(tl.track_info[tl.track[i]][1], tl.end[i])
    months = defaultdict(lambda: [0, 0, 0])            # (year, month) -> ms new / this year / classic
    recent = [0, 0, 0]
    new_songs, classic_songs = Counter(), Counter()
    for i in listened:
        t, e, ms = tl.track[i], tl.end[i], tl.ms[i]
        age = e - tl.end[tl.first_listen[t]]
        kind = 0 if age <= NEW_DAYS * DAY else 1 if age <= CLASSIC_DAYS * DAY else 2
        if e - first_ever >= WARMUP_DAYS * DAY:
            d = datetime.fromtimestamp(e, LOCAL_TZ)
            months[(d.year, d.month)][kind] += ms
        if e >= now - RECENT_DAYS * DAY:
            recent[kind] += ms
            if kind == 0:
                new_songs[t] += ms
            elif kind == 2:
                classic_songs[t] += ms
    total = sum(recent)
    if total < 3 * 3_600_000:                          # under 3 hours in the last month: nothing to say
        return None
    share = [round(100 * x / total) for x in recent]
    full = sorted(k for k, v in months.items() if sum(v) >= 3_600_000)
    if len(full) < 4:
        return None
    usual = [statistics.median(100 * months[k][j] / sum(months[k]) for k in full) for j in range(3)]
    if share[0] >= max(usual[0] * 1.4, usual[0] + 8):
        mode, line = "exploring", "You're in discovery mode"
    elif share[2] >= usual[2] + 10 and share[0] <= usual[0] * 0.7:
        mode, line = "classics", "You're sitting on the classics"
    else:
        mode, line = "balanced", "A balanced month"
    ratio = share[0] / usual[0] if usual[0] else None

    def song(t, ms, kind):
        name, artist, _ = tl.track_info[t]
        first = datetime.fromtimestamp(tl.end[tl.first_listen[t]], LOCAL_TZ)
        if kind == "new":
            brand_new = now - artist_first.get(artist, 0) <= NEW_DAYS * DAY
            note = "new artist" if brand_new else f"artist first heard in {datetime.fromtimestamp(artist_first[artist], LOCAL_TZ).year}"
        else:
            note = f"first heard {first:%b %Y}"
        return {"name": name, "artist": artist, "track": tl.track_ids[t], "minutes": round(ms / 60000), "note": note}

    new_artist_ms = sum(ms for t, ms in new_songs.items()
                        if now - artist_first.get(tl.track_info[t][1], 0) <= NEW_DAYS * DAY)
    series = full[-24:]
    return {
        "mode": mode, "line": line, "share": share, "usual": [round(u) for u in usual],
        "ratio": ratio, "new_songs_count": len(new_songs),
        "new_artist_share": round(100 * new_artist_ms / recent[0]) if recent[0] else 0,
        "new": [song(t, ms, "new") for t, ms in new_songs.most_common(5)],
        "classics": [song(t, ms, "classic") for t, ms in classic_songs.most_common(5)],
        "trend": {"values": [round(100 * months[k][0] / sum(months[k]), 1) for k in series],
                  "labels": [datetime(y, m, 1).strftime("%b %Y") for y, m in series],
                  "usual": usual[0]},
    }


def overlap(a, b):
    ta, tb = sum(a.values()) or 1, sum(b.values()) or 1
    return 100 * sum(min(a[k] / ta, b[k] / tb) for k in a.keys() & b.keys())


def right_now(user_id):
    return _right_now(user_id, latest_play_id(user_id))


@lru_cache(maxsize=4)
@disk_cached(versions=1)
def _right_now(user_id, _latest):
    tl = timeline(user_id)
    listened = [i for i in range(len(tl.track)) if tl.listened(i)]
    if len(listened) < 100:
        return None
    now = tl.end[listened[-1]]
    t14, t30, t_usual = now - REPEAT_DAYS * DAY, now - RECENT_DAYS * DAY, now - (RECENT_DAYS + USUAL_DAYS) * DAY
    first_ever = tl.end[listened[0]]
    usual_days = max(1, min(USUAL_DAYS, (t30 - first_ever) / DAY))

    songs14, songs_usual = Counter(), Counter()
    artists30, artists_usual, artists14 = Counter(), Counter(), Counter()
    ms14, ms_usual = 0, 0
    hours30, hours_usual = Counter(), Counter()
    days14 = set()
    for i in listened:
        e = tl.end[i]
        if e < t_usual:
            continue
        t, a, ms = tl.track[i], tl.track_info[tl.track[i]][1], tl.ms[i]
        hour = int(local_ts(e) // 3600) % 24
        if e >= t30:
            artists30[a] += ms
            hours30[hour] += ms
            if e >= t14:
                songs14[t] += 1
                artists14[a] += ms
                ms14 += ms
                days14.add(int(local_ts(e) // DAY))
        else:
            songs_usual[t] += 1
            artists_usual[a] += ms
            ms_usual += ms
            hours_usual[hour] += ms

    # ---- On repeat: songs played far more in the last 14 days than their usual rate.
    def streak_start(t):
        """When this run of playing it began: walk back while plays are no more than 4 days apart."""
        ps = [i for i in tl.by_track[t] if tl.listened(i) and tl.end[i] <= now]
        k = len(ps) - 1
        while k > 0 and tl.end[ps[k]] - tl.end[ps[k - 1]] <= 4 * DAY:
            k -= 1
        return tl.end[ps[k]]
    repeat = []
    for t, n in songs14.most_common(40):
        if n < 5:
            break
        usual_rate = songs_usual[t] / usual_days * REPEAT_DAYS          # plays in a usual 14 days
        ratio = n / usual_rate if usual_rate else None
        if ratio is not None and ratio < 3:
            continue
        since = streak_start(t)
        repeat.append({"name": tl.track_info[t][0], "artist": tl.track_info[t][1], "track": tl.track_ids[t],
                       "plays": n, "usual": round(usual_rate, 1),
                       # A ratio only means something if it was played at least now and then before.
                       "ratio": ratio if usual_rate >= 1 else None,
                       "new": tl.end[tl.first_listen[t]] >= t30,
                       "since": fmt_date(datetime.fromtimestamp(since, LOCAL_TZ)), "days": max(1, round((now - since) / DAY))})
    repeat = repeat[:8]

    # ---- Rising and fading artists: share of the last 30 days vs share of the usual year.
    tot30, tot_usual = sum(artists30.values()) or 1, sum(artists_usual.values()) or 1
    rising, fading = [], []
    for a, ms in artists30.most_common(60):
        share, before = ms / tot30, artists_usual[a] / tot_usual
        if share >= 0.02 and (before == 0 or share / before >= 2):
            rising.append({"name": a, "now": 100 * share, "before": 100 * before, "new": before == 0,
                           "track": tl.track_ids[tl.artist_best[a]] if a in tl.artist_best else None})
    for a, ms in artists_usual.most_common(40):
        before, share = ms / tot_usual, artists30[a] / tot30
        if before >= 0.015 and share < before / 3:
            fading.append({"name": a, "now": 100 * share, "before": 100 * before,
                           "track": tl.track_ids[tl.artist_best[a]] if a in tl.artist_best else None})
    rising.sort(key=lambda r: -(r["now"] - r["before"]))
    fading.sort(key=lambda r: -(r["before"] - r["now"]))

    # ---- New era? How alike the last 30 days are to the 6 months before, vs how alike that usually is.
    def mix(lo, hi):
        c = Counter()
        for i in listened:
            if lo <= tl.end[i] < hi:
                c[tl.track_info[tl.track[i]][1]] += tl.ms[i]
        return c
    current = overlap(artists30, mix(t30 - 180 * DAY, t30))
    history = []
    for k in range(1, 13):                       # the same comparison at monthly steps over the past year
        end_ = now - k * 30 * DAY
        if end_ - 210 * DAY < first_ever:
            break
        history.append(overlap(mix(end_ - 30 * DAY, end_), mix(end_ - 210 * DAY, end_ - 30 * DAY)))
    usual_overlap = sum(history) / len(history) if history else None
    era = None
    try:
        j = journey.journey(user_id, "broad")
        if j:
            era = j["eras"][-1]["name"]
    except Exception:
        pass
    shift = None
    if usual_overlap:
        if current < usual_overlap - 12:
            shift = "shifting"
        elif current > usual_overlap + 8:
            shift = "settled"
        else:
            shift = "steady"

    # ---- Pace and hours.
    per_day14 = ms14 / REPEAT_DAYS
    per_day_usual = ms_usual / usual_days
    def share(c, hours):
        tot = sum(c.values()) or 1
        return 100 * sum(c[h] for h in hours) / tot
    late_now, late_usual = share(hours30, range(0, 5)), share(hours_usual, range(0, 5))
    def middle_hour(c):
        """The hour by which half the day's listening has happened, counting the day from 5am."""
        tot, run = sum(c.values()) or 1, 0
        for k in range(24):
            h = (5 + k) % 24
            run += c[h]
            if run >= tot / 2:
                return h
        return None
    mid_now, mid_usual = middle_hour(hours30), middle_hour(hours_usual)
    top_hours = max(1, max(hours30.values(), default=1) / (sum(hours30.values()) or 1),
                    max(hours_usual.values(), default=1) / (sum(hours_usual.values()) or 1))
    profile = []
    for h in range(24):
        a_ = hours30[h] / (sum(hours30.values()) or 1)
        b_ = hours_usual[h] / (sum(hours_usual.values()) or 1)
        profile.append({"hour": h, "now": 100 * a_, "usual": 100 * b_})
    peak = max(max(p["now"], p["usual"]) for p in profile) or 1
    for p in profile:
        p["h_now"], p["h_usual"] = round(100 * p["now"] / peak), round(100 * p["usual"] / peak)

    # ---- Fresh finds: artists first heard in the last 30 days.
    first_artist = {}
    for i in listened:
        first_artist.setdefault(tl.track_info[tl.track[i]][1], i)
    finds = []
    for a, i in first_artist.items():
        if tl.end[i] >= t30:
            ps = [p for p in tl.by_artist[a] if tl.listened(p)]
            days = len({int(local_ts(tl.end[p]) // DAY) for p in ps})
            finds.append({"name": a, "plays": len(ps), "days": days, "sticking": days >= 3 or len(ps) >= 15,
                          "first": fmt_date(datetime.fromtimestamp(tl.end[i], LOCAL_TZ)),
                          "first_song": tl.track_info[tl.track[i]][0],
                          "found": "found you" if tl.kind[i] == 2 else "you found" if tl.kind[i] == 1 else None,
                          "track": tl.track_ids[tl.artist_best[a]] if a in tl.artist_best else tl.track_ids[tl.track[i]]})
    finds.sort(key=lambda f: -f["plays"])
    new_songs = [t for t in songs14 if tl.end[tl.first_listen[t]] >= t30]

    last_play = datetime.fromtimestamp(now, LOCAL_TZ)
    hero_song = repeat[0] if repeat else None
    return {
        "as_of": fmt_date(last_play), "as_of_ts": now,
        "hero": hero_song,
        "repeat": repeat,
        "top_artists14": [{"name": a, "hours": ms / 3_600_000,
                           "track": tl.track_ids[tl.artist_best[a]] if a in tl.artist_best else None}
                          for a, ms in artists14.most_common(5)],
        "rising": rising[:6], "fading": fading[:6],
        "current_overlap": round(current), "usual_overlap": round(usual_overlap) if usual_overlap else None,
        "shift": shift, "era": era,
        "pace": {"now_min": per_day14 / 60000, "usual_min": per_day_usual / 60000,
                 "ratio": per_day14 / per_day_usual if per_day_usual else None,
                 "active_days": min(REPEAT_DAYS, len(days14))},
        "hours": {"late_now": round(late_now), "late_usual": round(late_usual),
                  "mid_now": mid_now, "mid_usual": mid_usual, "profile": profile},
        "finds": finds[:8], "finds_count": len(finds),
        "discovery": discovery_mode(tl, listened, now),
        "sticking": sum(1 for f in finds if f["sticking"]), "new_songs": len(new_songs),
    }
