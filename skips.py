"""
Skipping, in depth. Export data only: the live tracking doesn't say why a song ended.

A SKIP = the song ended because next was pressed (reason_end "fwdbtn"), at any point.
- Instant skip: within 10 seconds (you knew straight away).
- Late skip: after a minute or more (you gave it a go, then got bored).
- Streak: 5+ skips in a row, without a 10-minute break (hunting for something).
- Burnout: a song you hardly ever skipped for a long time that you now mostly skip.
"""
import math
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache

import entity
from entity import SESSION_GAP_S, fmt_date, timeline
from stats import LOCAL_TZ, latest_play_id

INSTANT_S = 10
LATE_S = 60
STREAK = 5
BUCKETS = [(0, 2, "under 2 s"), (2, 5, "2–5 s"), (5, 10, "5–10 s"), (10, 30, "10–30 s"),
           (30, 60, "30 s–1 min"), (60, 120, "1–2 min"), (120, 10**9, "2 min+")]
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def rate(pair):
    seen, skipped = pair
    return 100 * skipped / seen if seen else None


def skips(user_id):
    return _skips(user_id, latest_play_id(user_id))


@lru_cache(maxsize=4)
def _skips(user_id, _latest):
    tl = timeline(user_id)
    n = len(tl.track)
    known = [i for i in range(n) if tl.skip[i] >= 0]
    if len(known) < 200:
        return None
    skipped = [i for i in known if tl.skip[i] == 1]
    overall = 100 * len(skipped) / len(known)

    # ---- Habits: by year, hour, weekday, shuffle, how the song started.
    by_year, by_hour, by_day = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    by_shuffle, by_start = defaultdict(lambda: [0, 0]), defaultdict(lambda: [0, 0])
    for i in known:
        when = tl.local(i)
        s = tl.skip[i]
        for d, k in ((by_year, when.year), (by_hour, when.hour), (by_day, when.weekday()),
                     (by_shuffle, tl.shuffle[i]), (by_start, tl.kind[i])):
            d[k][0] += 1
            d[k][1] += s
    years = [{"year": y, "rate": rate(v), "seen": v[0]} for y, v in sorted(by_year.items()) if v[0] >= 200]
    hours = [{"hour": h, "rate": rate(by_hour[h]), "seen": by_hour[h][0]} for h in range(24)]
    days = [{"day": WEEKDAYS[d], "rate": rate(by_day[d]), "seen": by_day[d][0]} for d in range(7)]
    start_labels = {1: "You put it on", 2: "Came on by itself", 3: "Other (remote, app opened…)"}
    starts = [{"label": start_labels[k], "rate": round(rate(by_start[k])), "seen": by_start[k][0]}
              for k in (1, 2, 3) if by_start[k][0] >= 100]
    shuffle = [{"label": label, "rate": round(rate(by_shuffle[k])), "seen": by_shuffle[k][0]}
               for k, label in ((1, "Shuffle on"), (0, "Shuffle off")) if by_shuffle[k][0] >= 100]

    # ---- Instant vs late: when in the song you press next.
    bucket_n = Counter()
    per_song = defaultdict(lambda: [0, 0, 0])            # appearances, instant skips, late skips
    for i in known:
        t = tl.track[i]
        per_song[t][0] += 1
        if tl.skip[i]:
            secs = tl.ms[i] / 1000
            for lo, hi, label in BUCKETS:
                if lo <= secs < hi:
                    bucket_n[label] += 1
                    break
            if secs < INSTANT_S:
                per_song[t][1] += 1
            elif secs >= LATE_S:
                per_song[t][2] += 1
    instant_share = 100 * sum(1 for i in skipped if tl.ms[i] < INSTANT_S * 1000) / (len(skipped) or 1)
    late_share = 100 * sum(1 for i in skipped if tl.ms[i] >= LATE_S * 1000) / (len(skipped) or 1)
    top_bucket = max(bucket_n.values()) if bucket_n else 1
    buckets = [{"label": label, "n": bucket_n[label], "pct": 100 * bucket_n[label] / (len(skipped) or 1),
                "bar": round(100 * bucket_n[label] / top_bucket)} for *_, label in BUCKETS]

    def song_list(index, min_n, n_out=10):
        rows = [(t, v) for t, v in per_song.items() if v[index] >= min_n]
        rows.sort(key=lambda tv: (-tv[1][index] / tv[1][0], -tv[1][index]))
        return [{"name": tl.track_info[t][0], "artist": tl.track_info[t][1], "track": tl.track_ids[t],
                 "n": v[index], "of": v[0], "pct": round(100 * v[index] / v[0])} for t, v in rows[:n_out]]
    instant_songs = song_list(1, 10)
    late_songs = song_list(2, 5)

    # ---- Burnout: the average curve, and the songs it happened to.
    seq = defaultdict(list)                       # song -> skip flags (and positions) in order
    for i in known:
        seq[tl.track[i]].append(i)
    curve = defaultdict(lambda: [0, 0])
    heavy = [t for t, ps in seq.items() if len(ps) >= 60]
    for t in heavy:
        for k, i in enumerate(seq[t][:150]):
            curve[k // 10][0] += 1
            curve[k // 10][1] += tl.skip[i]
    burn_curve = [{"from": k * 10 + 1, "to": k * 10 + 10, "rate": rate(curve[k])}
                  for k in sorted(curve) if curve[k][0] >= 50]
    tipping = next((c["from"] for c in burn_curve if c["rate"] >= burn_curve[0]["rate"] + 0.75 *
                    (max(x["rate"] for x in burn_curve) - burn_curve[0]["rate"])), None) if burn_curve else None

    burnout = []
    for t, ps in seq.items():
        if len(ps) < 30:
            continue
        flags = [tl.skip[i] for i in ps]
        recent = flags[-15:]
        if sum(recent) / len(recent) < 0.5:
            continue
        # The turning point: after the last time a 10-in-a-row window was mostly listened to.
        turn = None
        for k in range(len(flags) - 10, -1, -1):
            if sum(flags[k:k + 10]) < 5:
                turn = k + 10
                break
        if turn is None or turn < 15:
            continue
        before = flags[:turn]
        loved_rate = sum(before) / len(before)
        if loved_rate > 0.2:
            continue
        burnout.append({
            "name": tl.track_info[t][0], "artist": tl.track_info[t][1], "track": tl.track_ids[t],
            "loved_plays": len(before) - sum(before), "loved_rate": round(100 * loved_rate),
            "loved_from": tl.local(ps[0]).strftime("%b %Y"), "turned": tl.local(ps[min(turn, len(ps) - 1)]).strftime("%b %Y"),
            "now_rate": round(100 * sum(recent) / len(recent)),
        })
    burnout.sort(key=lambda b: -b["loved_plays"])

    # ---- Streaks: 5+ skips in a row, and what finally stopped them.
    streaks, run = [], []
    for i in range(n):
        if tl.skip[i] == 1 and (not run or tl.start(i) - tl.end[run[-1]] <= SESSION_GAP_S):
            run.append(i)
            continue
        if len(run) >= STREAK:
            stopper = i if (tl.skip[i] == 0 and tl.listened(i) and tl.start(i) - tl.end[run[-1]] <= SESSION_GAP_S) else None
            streaks.append((run, stopper))
        run = [i] if tl.skip[i] == 1 else []
    streak_info = None
    if streaks:
        longest_run, _ = max(streaks, key=lambda s: len(s[0]))
        stoppers = Counter(tl.track[s] for _, s in streaks if s is not None)
        stopper_artists = Counter(tl.track_info[tl.track[s]][1] for _, s in streaks if s is not None)
        by_hour_streak = Counter(tl.local(r[0]).hour for r, _ in streaks)
        peak_hour = max(range(24), key=lambda h: by_hour_streak[h] / (by_hour[h][0] or 1))
        per_year = Counter(tl.local(r[0]).year for r, _ in streaks)
        busiest_year = per_year.most_common(1)[0]
        lengths = sorted(len(r) for r, _ in streaks)
        streak_info = {
            "count": len(streaks),
            "median": lengths[len(lengths) // 2],
            "longest": {"n": len(longest_run), "date": fmt_date(tl.local(longest_run[0])),
                        "time": tl.local(longest_run[0]).strftime("%H:%M"),
                        "minutes": max(1, round((tl.end[longest_run[-1]] - tl.start(longest_run[0])) / 60))},
            "stoppers": [{"name": tl.track_info[t][0], "artist": tl.track_info[t][1], "track": tl.track_ids[t], "n": c}
                         for t, c in stoppers.most_common(8)],
            "stopper_artist": stopper_artists.most_common(1)[0] if stopper_artists else None,
            "peak_hour": peak_hour,
            "busiest_year": {"year": busiest_year[0], "n": busiest_year[1]},
            "stopped_share": round(100 * sum(1 for _, s in streaks if s is not None) / len(streaks)),
        }

    first_year, last_year = (years[0], years[-1]) if years else (None, None)
    lowest = min(years, key=lambda y: y["rate"]) if years else None
    highest = max(years, key=lambda y: y["rate"]) if years else None
    return {
        "overall": overall, "known": len(known), "skipped": len(skipped),
        "years": years, "lowest": lowest, "highest": highest, "first_year": first_year, "last_year": last_year,
        "year_chart": rate_chart([(str(y["year"]), y["rate"], f"{y['year']}: {y['rate']:.0f}% skipped ({y['seen']:,} songs)") for y in years], overall),
        "hour_chart": rate_chart([(f"{h['hour']:02d}", h["rate"], f"{h['hour']:02d}:00–{h['hour']:02d}:59: {h['rate']:.0f}% skipped ({h['seen']:,} songs)" if h['rate'] is not None else "") for h in hours], overall, every=3),
        "day_chart": rate_chart([(d["day"], d["rate"], f"{d['day']}: {d['rate']:.0f}% skipped ({d['seen']:,} songs)") for d in days], overall),
        "starts": starts, "shuffle": shuffle,
        "instant_share": round(instant_share), "late_share": round(late_share), "buckets": buckets,
        "instant_songs": instant_songs, "late_songs": late_songs,
        "burn_curve": burn_curve, "tipping": tipping, "heavy": len(heavy),
        "curve_chart": rate_chart([(f"{c['from']}", c["rate"], f"Plays {c['from']}–{c['to']}: skipped {c['rate']:.0f}% of the time") for c in burn_curve], overall, every=2),
        "burnout": burnout[:10], "burnout_count": len(burnout),
        "streaks": streak_info,
    }


def rate_chart(points, overall, every=1):
    """Bars of skip rate (0–100%) with a dashed line at the overall rate."""
    points = [p for p in points if p[1] is not None]
    if not points:
        return None
    W, H, LEFT, RIGHT, TOP, BOTTOM = 960, 220, 40, 12, 16, 28
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM
    top = max(10, 10 * math.ceil(max(p[1] for p in points) / 10))
    slot = pw / len(points)
    gap = 4 if slot > 20 else 2
    bars = []
    for n, (label, value, tip) in enumerate(points):
        h = ph * value / top
        bars.append({"x": round(LEFT + n * slot + gap / 2, 1), "w": round(slot - gap, 1), "y": round(TOP + ph - h, 1),
                     "h": round(h, 1), "label": label if n % every == 0 else "", "cx": round(LEFT + n * slot + slot / 2, 1),
                     "tip": tip, "above": value > overall, "slot_x": round(LEFT + n * slot, 1)})
    step = 10 if top <= 50 else 20
    return {"w": W, "h": H, "left": LEFT, "right": W - RIGHT, "bottom": TOP + ph, "slot": round(slot, 1), "bars": bars,
            "y_ticks": [(v, round(TOP + ph * (1 - v / top), 1)) for v in range(0, top + 1, step)],
            "overall_y": round(TOP + ph * (1 - overall / top), 1)}
