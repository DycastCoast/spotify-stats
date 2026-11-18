"""
Your listening as a journey: eras (phases), what changed between them, how you
discover new artists, who you've stayed loyal to, and how your top artists rose and fell.

How eras are found
------------------
Each month becomes a "mix": what share of that month went on each artist. An era
is a run of months whose mixes are alike. For a given number of eras, we try
EVERY way of cutting the history into that many runs (each at least 4 months)
and keep the cut where months are most like the rest of their own run.

Trying every cut sounds impossible, but dynamic programming makes it quick:
the best way to cut the first b months into k eras = the best way to cut the
first a months into k-1 eras, plus one era from a to b, for the best a.
So we only ever extend answers we've already worked out.

The similarity score is the same "overlap" as the Compare page: add up, for
each artist, the smaller of the two shares.

numpy does the arithmetic on whole tables of numbers at once, which is what
makes this take about a second instead of half a minute.
"""
import math

import charts
from collections import Counter, defaultdict
from datetime import datetime
from functools import lru_cache

from diskcache import disk_cached

import numpy as np

import entity
from entity import fmt_date
from stats import LOCAL_TZ, MIN_PLAY_MS, latest_play_id

MIN_ERA_MONTHS = 4          # an era is at least 4 months: shorter is a phase within a phase
QUIET_MONTH_HOURS = 3       # months with less than this are skipped when finding eras (too little to judge)
TOP_ARTISTS = 400           # only the biggest artists are compared: the rest are tiny shares that barely move the score
STUCK_PLAYS = 10            # a new artist "stuck" if you went on to play them 10+ times
WARMUP_MONTHS = 3           # in the first months of anyone's history EVERY artist is "new", so they don't count as discoveries


# ------------------------------------------------------------------ months

def month_index(year, month):
    return year * 12 + month - 1


def month_label(m, short=False):
    return datetime(m // 12, m % 12 + 1, 1).strftime("%b %Y" if short else "%B %Y")


def monthly(tl):
    """Per month (as a single number, year*12+month-1): listening time per artist,
    plus every artist's first listen and play count."""
    months = defaultdict(Counter)
    first, plays = {}, Counter()
    info = tl.track_info
    for i in range(len(tl.track)):
        ms = tl.ms[i]
        if ms < MIN_PLAY_MS:
            continue
        when = datetime.fromtimestamp(tl.end[i], LOCAL_TZ)
        m = month_index(when.year, when.month)
        artist = info[tl.track[i]][1]
        months[m][artist] += ms
        if artist not in first:
            first[artist] = (m, i)
        plays[artist] += 1
    return months, first, plays


# ------------------------------------------------------------------ finding eras

def find_eras(months, k):
    """Cut the active months into k eras. Returns a list of (first month, last month)."""
    keys = [m for m in sorted(months) if sum(months[m].values()) >= QUIET_MONTH_HOURS * 3_600_000]
    n = len(keys)
    k = max(1, min(k, n // MIN_ERA_MONTHS))
    if k <= 1:
        return [(keys[0], keys[-1])] if keys else []

    # The table: one row per month, one column per big artist, each cell = ms.
    total = Counter()
    for m in keys:
        total.update(months[m])
    artists = [a for a, _ in total.most_common(TOP_ARTISTS)]
    col = {a: j for j, a in enumerate(artists)}
    ms = np.zeros((n, len(artists)))
    for r, m in enumerate(keys):
        for a, v in months[m].items():
            j = col.get(a)
            if j is not None:
                ms[r, j] = v
    month_total = np.array([sum(months[m].values()) for m in keys])
    shares = ms / month_total[:, None]                 # each month's mix (rows add up to just under 1)
    running = np.vstack([np.zeros(len(artists)), np.cumsum(ms, axis=0)])   # running totals: any run's mix in one subtraction
    running_total = np.concatenate([[0], np.cumsum(month_total)])

    # cost[a, b] = how badly months a..b-1 fit together as one era (0 = all identical)
    cost = np.full((n + 1, n + 1), np.inf)
    for a in range(n - MIN_ERA_MONTHS + 1):
        for b in range(a + MIN_ERA_MONTHS, n + 1):
            mix = (running[b] - running[a]) / (running_total[b] - running_total[a])
            fit = np.minimum(shares[a:b], mix).sum(axis=1)       # each month's overlap with the era's mix
            cost[a, b] = (1 - fit).sum()

    # Dynamic programming: best[j][b] = the lowest total cost of cutting the first b months into j eras.
    best = np.full((k + 1, n + 1), np.inf)
    cut = np.zeros((k + 1, n + 1), dtype=int)
    best[0, 0] = 0
    for j in range(1, k + 1):
        for b in range(j * MIN_ERA_MONTHS, n + 1):
            options = best[j - 1, :b] + cost[:b, b]
            a = int(np.argmin(options))
            best[j, b], cut[j, b] = options[a], a
    bounds, b = [], n
    for j in range(k, 0, -1):
        a = cut[j, b]
        bounds.append((keys[a], keys[b - 1]))
        b = a
    return bounds[::-1]


def overlap(a, b):
    ta, tb = sum(a.values()) or 1, sum(b.values()) or 1
    return 100 * sum(min(a[x] / ta, b[x] / tb) for x in a.keys() & b.keys())


def describe(tl, months, first, plays, bounds, all_mix, all_total):
    """Each era: its name, dates, defining artists, and how much you listened."""
    eras = []
    used_names = set()
    active_months = [m for m in months if sum(months[m].values()) >= QUIET_MONTH_HOURS * 3_600_000]
    avg_per_month = sum(sum(months[m].values()) for m in active_months) / (len(active_months) or 1)
    for lo, hi in bounds:
        mix = Counter()
        for m in range(lo, hi + 1):
            mix.update(months.get(m, {}))
        total = sum(mix.values()) or 1
        n_months = hi - lo + 1

        # What DEFINES an era: big in it AND much bigger in it than in the rest of your history.
        # Plain "biggest" would name every era after your all-time favourite.
        def typical(a):
            here = mix[a] / total
            elsewhere = (all_mix[a] - mix[a]) / max(all_total - total, 1)
            return here * min(here / max(elsewhere, 1e-4), 20)
        defining = sorted((a for a in mix if mix[a] / total >= 0.01), key=typical, reverse=True)[:3]
        if not defining:
            defining = [a for a, _ in mix.most_common(3)]
        lead = next((a for a in defining if a not in used_names), defining[0])
        used_names.add(lead)

        start = min(months) + WARMUP_MONTHS
        found = [(a, f) for a, f in first.items() if max(lo, start) <= f[0] <= hi]
        stuck = [a for a, _ in found if plays[a] >= STUCK_PLAYS]
        best_find = max(stuck, key=lambda a: all_mix[a]) if stuck else None

        per_month = total / n_months
        eras.append({
            "lo": lo, "hi": hi,
            "name": era_name(lead),
            "lead": lead,
            "lead_track": art_track(tl, lead),
            "dates": f"{month_label(lo, True)} – {month_label(hi, True)}",
            "months": n_months,
            "length": length_text(n_months),
            "hours": total / 3_600_000,
            "pace": per_month / avg_per_month if avg_per_month else 1,
            "defining": [{"name": a, "here": 100 * mix[a] / total,
                          "elsewhere": 100 * (all_mix[a] - mix[a]) / max(all_total - total, 1),
                          "track": art_track(tl, a)} for a in defining],
            "top": [{"name": a, "pct": 100 * v / total, "track": art_track(tl, a)} for a, v in mix.most_common(5)],
            "new_artists": len(found),
            "stuck": len(stuck),
            "best_find": {"name": best_find, "track": art_track(tl, best_find),
                          "hours": all_mix[best_find] / 3_600_000} if best_find else None,
            "mix": mix,
        })
    return eras


def era_name(artist):
    """'The Juice WRLD era'; an artist already starting with 'The' doesn't get a second one."""
    return f"The {artist[4:] if artist.lower().startswith('the ') else artist} era"


def art_track(tl, artist):
    best = tl.artist_best.get(artist)
    return tl.track_ids[best] if best is not None else None


def length_text(months):
    if months < 12:
        return f"{months} months"
    years = months / 12
    return f"{years:.0f} year{'s' if round(years) != 1 else ''}" if abs(years - round(years)) < 0.15 else f"{years:.1f} years"


def era_songs(tl, eras):
    """Each era's top 3 songs, and the covers of its top 4 albums (for the album wall),
    in one pass over the history."""
    bounds = [(datetime(e["lo"] // 12, e["lo"] % 12 + 1, 1, tzinfo=LOCAL_TZ).timestamp(),
               datetime((e["hi"] + 1) // 12, (e["hi"] + 1) % 12 + 1, 1, tzinfo=LOCAL_TZ).timestamp()) for e in eras]
    counts = [Counter() for _ in eras]
    albums = [Counter() for _ in eras]
    e = 0
    for i in range(len(tl.track)):
        t = tl.end[i]
        while e < len(bounds) and t >= bounds[e][1]:
            e += 1
        if e == len(bounds):
            break
        if t >= bounds[e][0] and tl.ms[i] >= MIN_PLAY_MS:
            counts[e][tl.track[i]] += tl.ms[i]
            albums[e][tl.album[i]] += tl.ms[i]
    for era, c, alb in zip(eras, counts, albums):
        era["songs"] = [{"name": tl.track_info[t][0], "artist": tl.track_info[t][1], "track": tl.track_ids[t],
                         "hours": ms / 3_600_000} for t, ms in c.most_common(3)]
        covers = []
        for a, _ in alb.most_common(8):
            name, artist = tl.album_info[a]
            # the album's cover comes from one of its songs: its most-played one in this era
            best = max((t for t in c if tl.album_best.get(a) == t or tl.track_info[t][2] == name and tl.track_info[t][1] == artist),
                       key=lambda t: c[t], default=None)
            if best is not None and name:
                covers.append({"track": tl.track_ids[best], "album": name, "artist": artist})
            if len(covers) == 4:
                break
        era["covers"] = covers


def transition(prev, nxt):
    """One plain sentence (plus details) about what changed from one era to the next."""
    a, b = prev["mix"], nxt["mix"]
    ta, tb = sum(a.values()) or 1, sum(b.values()) or 1
    change = {x: b[x] / tb - a[x] / ta for x in a.keys() | b.keys()}
    arrived = [x for x in sorted(change, key=change.get, reverse=True)
               if b[x] / tb >= 0.02 and a[x] / ta < 0.005][:2]
    rose = [x for x in sorted(change, key=change.get, reverse=True)
            if x not in arrived and change[x] >= 0.015 and a[x] / ta >= 0.005][:1]
    fell = [x for x in sorted(change, key=change.get) if change[x] <= -0.015][:2]
    kept = [x for x in sorted(a.keys() & b.keys(), key=lambda x: -min(a[x] / ta, b[x] / tb))
            if min(a[x] / ta, b[x] / tb) >= 0.02][:2]
    pct = lambda share: f"{100 * share:.0f}%" if share >= 0.01 else "under 1%"

    parts = []
    if fell:
        x = fell[0]
        parts.append(f"{x} went from {pct(a[x] / ta)} to {pct(b[x] / tb)}")
    if arrived:
        parts.append(" and ".join(f"{x} ({pct(b[x] / tb)})" for x in arrived) + " arrived")
    elif rose:
        x = rose[0]
        parts.append(f"{x} rose from {pct(a[x] / ta)} to {pct(b[x] / tb)}")
    sentence = "; ".join(parts) if parts else "A gradual shift rather than a clean break"
    pace = (sum(b.values()) / nxt["months"]) / max(sum(a.values()) / prev["months"], 1)
    pace_text = (f"you listened {pace:.1f}× as much per month" if pace >= 1.3
                 else f"you listened {1 / pace:.1f}× less per month" if pace <= 0.77 else None)
    return {
        "sentence": sentence[0].upper() + sentence[1:] + ".",
        "kept": kept,
        "similarity": overlap(a, b),
        "pace": pace_text,
    }


# ------------------------------------------------------------------ discovery

def discovery_chart(months, first, plays, eras):
    """New artists per month, and how many of them stuck. Bars for an SVG chart, eras shaded behind."""
    new, stuck, names = Counter(), Counter(), defaultdict(list)
    warmup_end = min(months) + WARMUP_MONTHS
    for a, (m, _) in first.items():
        if m < warmup_end:
            continue
        new[m] += 1
        if plays[a] >= STUCK_PLAYS:
            stuck[m] += 1
            names[m].append(a)
    keys = list(range(min(months), max(months) + 1))
    W, H, LEFT, RIGHT, TOP, BOTTOM = 960, 240, 48, 16, 34, 30
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM
    slot = pw / len(keys)
    peak = max(new.values()) if new else 1
    rough = peak / 4
    mag = 10 ** math.floor(math.log10(rough)) if rough >= 1 else 1
    step = max(1, int(next(x * mag for x in (1, 2, 5, 10) if x * mag >= rough)))
    nice = step * math.ceil(peak / step)
    gap = 1 if slot > 4 else 0
    bars = []
    for n, m in enumerate(keys):
        v, s = new.get(m, 0), stuck.get(m, 0)
        x = LEFT + n * slot
        h_all = ph * v / nice
        h_stuck = ph * s / nice
        top_names = sorted(names[m], key=lambda a: -plays[a])[:3]
        tip = f"{month_label(m, True)} · {v} new artist{'s' if v != 1 else ''}, {s} stuck"
        if top_names:
            tip += ": " + ", ".join(top_names)
        bars.append({"x": round(x + gap / 2, 2), "w": round(max(slot - gap, 0.5), 2),
                     "y": round(TOP + ph - h_all, 2), "h": round(h_all, 2),
                     "sy": round(TOP + ph - h_stuck, 2), "sh": round(h_stuck, 2),
                     "slot_x": round(x, 2), "tip": tip})
    shades = []
    for idx, e in enumerate(eras):
        x0 = LEFT + (e["lo"] - keys[0]) * slot
        x1 = LEFT + (e["hi"] + 1 - keys[0]) * slot
        shades.append({"x": round(x0, 1), "w": round(x1 - x0, 1), "odd": idx % 2,
                       "label": e["lead"], "mid": round((x0 + x1) / 2, 1), "wide": x1 - x0 > 70})
    years = sorted({m // 12 for m in keys})
    every = max(1, math.ceil(len(years) / 10))
    x_ticks = [(str(y), round(LEFT + (month_index(y, 1) - keys[0]) * slot, 1))
               for y in years[::every] if month_index(y, 1) >= keys[0]]

    total_new = sum(new.values())
    total_stuck = sum(stuck.values())
    # Busiest month for finding artists that stuck (more telling than raw "new", which one-off plays inflate)
    best_m = max(stuck, key=stuck.get) if stuck else None
    # Last 12 full months vs your all-time monthly average
    recent = keys[-12:]
    recent_avg = sum(new.get(m, 0) for m in recent) / len(recent)
    overall_avg = total_new / len(keys)
    return {
        "w": W, "h": H, "left": LEFT, "right": W - RIGHT, "bottom": TOP + ph, "top": TOP,
        "warmup": {"x": LEFT, "w": round(WARMUP_MONTHS * slot, 1)},
        "bars": bars, "shades": shades, "x_ticks": x_ticks, "slot": round(slot, 2),
        "y_ticks": [(v, round(TOP + ph * (1 - v / nice), 1)) for v in range(0, nice + 1, step)],
        "total_new": total_new, "total_stuck": total_stuck,
        "stick_rate": 100 * total_stuck / (total_new or 1),
        "best": {"month": month_label(best_m), "n": stuck[best_m], "names": sorted(names[best_m], key=lambda a: -plays[a])[:3]}
                if best_m is not None else None,
        "recent_avg": recent_avg, "overall_avg": overall_avg,
    }


def loyalty(tl, months, eras, plays):
    """Ride-or-dies (still big across many eras) and ones left behind (once big, now gone)."""
    if not eras:
        return None
    shares = []
    for e in eras:
        total = sum(e["mix"].values()) or 1
        shares.append({a: v / total for a, v in e["mix"].items()})
    last_month = max(months)
    recent = Counter()
    for m in range(last_month - 5, last_month + 1):     # the last 6 months
        recent.update(months.get(m, {}))
    candidates = set()
    for s in shares:
        candidates.update(a for a, v in s.items() if v >= 0.01)

    def eras_in(a):
        return sum(1 for s in shares if s.get(a, 0) >= 0.005)   # "present" = at least 0.5% of that era

    loyal = sorted(candidates, key=lambda a: (eras_in(a), sum(s.get(a, 0) for s in shares)), reverse=True)[:10]
    left = []
    for a in candidates:
        peak_era = max(range(len(eras)), key=lambda i: shares[i].get(a, 0))
        peak = shares[peak_era].get(a, 0)
        if peak >= 0.01 and recent[a] == 0 and peak_era < len(eras) - 1:
            left.append((a, peak, peak_era))
    left.sort(key=lambda x: -x[1])

    def last_played(a):
        for m in range(last_month, min(months) - 1, -1):
            if months.get(m, {}).get(a):
                return month_label(m, True)
        return None

    return {
        "n_eras": len(eras),
        "loyal": [{"name": a, "eras": eras_in(a), "track": art_track(tl, a),
                   "dots": [s.get(a, 0) >= 0.005 for s in shares],
                   "hours": tl.artist_ms[a] / 3_600_000} for a in loyal],
        "left": [{"name": a, "peak": 100 * p, "era": eras[i]["name"], "track": art_track(tl, a),
                  "last": last_played(a)} for a, p, i in left[:10]],
    }


def artist_lines(months, eras, n=8):
    """Each of your biggest artists' share of listening over time: one small chart (row) each,
    all on the same time axis. Uses 3-month blocks so single odd months don't make spikes."""
    keys = list(range(min(months), max(months) + 1))
    total = Counter()
    for m in keys:
        total.update(months.get(m, {}))
    # "Biggest" by PEAK share rather than total, so an artist who defined one era makes the list
    quarter = []
    for q in range(0, len(keys), 3):
        mix = Counter()
        for m in keys[q:q + 3]:
            mix.update(months.get(m, {}))
        quarter.append((keys[q], mix, sum(mix.values())))
    peaks = Counter()
    for _, mix, t in quarter:
        if t >= 10 * 3_600_000:                        # ignore near-empty quarters
            for a, v in mix.items():
                peaks[a] = max(peaks[a], v / t)
    # The eras' own artists first (so the chart lines up with the eras), then your biggest overall.
    picks = []
    for a in [e["lead"] for e in eras] + [a for a, _ in total.most_common(n * 2)]:
        if a not in picks and len(picks) < n:
            picks.append(a)
    picks.sort(key=lambda a: min(m for m in keys if months.get(m, {}).get(a)))   # in order of arrival
    W, ROW, LEFT, RIGHT = 960, 46, 0, 0
    pw = W - LEFT - RIGHT
    top_share = max(peaks[a] for a in picks) if picks else 1
    rows = []
    for a in picks:
        pts = []
        for q, (m0, mix, t) in enumerate(quarter):
            share = mix[a] / t if t else 0
            x = LEFT + pw * (m0 - keys[0] + 1.5) / len(keys)
            y = ROW - 4 - (ROW - 8) * share / top_share
            pts.append((round(x, 1), round(y, 1), share, m0))
        smooth = charts.smooth_points([(x, y) for x, y, _, _ in pts], ROW)    # smooth, never overshooting
        line, area = smooth["line"], smooth["area"]
        peak_pt = max(pts, key=lambda p: p[2])
        rows.append({"name": a, "area": area, "line": line,
                     "peak": f"{100 * peak_pt[2]:.0f}%", "peak_when": month_label(peak_pt[3], True),
                     "px": peak_pt[0], "py": peak_pt[1],
                     "cols": [{"x": round(x - pw / len(quarter) / 2, 1), "w": round(pw / len(quarter), 1),
                               "tip": f"{a} · {month_label(m, True)}–{month_label(min(m + 2, keys[-1]), True)}: {100 * s:.1f}% of your listening"}
                              for x, _, s, m in pts]})
    shades = []
    for idx, e in enumerate(eras):
        x0 = LEFT + pw * (e["lo"] - keys[0]) / len(keys)
        x1 = LEFT + pw * (e["hi"] + 1 - keys[0]) / len(keys)
        shades.append({"x": round(x0, 1), "w": round(x1 - x0, 1), "odd": idx % 2})
    years = sorted({m // 12 for m in keys})
    every = max(1, math.ceil(len(years) / 10))
    x_ticks = [(str(y), round(LEFT + pw * (month_index(y, 1) - keys[0]) / len(keys), 1))
               for y in years[::every] if month_index(y, 1) >= keys[0]]
    return {"w": W, "row": ROW, "rows": rows, "shades": shades, "x_ticks": x_ticks,
            "scale": f"{100 * top_share:.0f}%"}


# ------------------------------------------------------------------ the whole page

def journey(user_id, detail="broad"):
    return _journey(user_id, detail, latest_play_id(user_id))


@lru_cache(maxsize=8)
@disk_cached(versions=1)
def _journey(user_id, detail, _latest):
    tl = entity.timeline(user_id)
    months, first, plays = monthly(tl)
    if len(months) < 2 * MIN_ERA_MONTHS:
        return None
    span_years = (max(months) - min(months) + 1) / 12
    # More history, more eras: about one per two years (broad) or a bit under one a year (detailed).
    k = max(2, round(span_years / 2)) if detail == "broad" else max(3, round(span_years * 0.8))
    bounds = find_eras(months, k)
    all_mix = Counter()
    for m in months.values():
        all_mix.update(m)
    all_total = sum(all_mix.values())
    eras = describe(tl, months, first, plays, bounds, all_mix, all_total)
    era_songs(tl, eras)
    for prev, nxt in zip(eras, eras[1:]):
        nxt["from_prev"] = transition(prev, nxt)

    strip = []
    lo, hi = min(months), max(months)
    for idx, e in enumerate(eras):
        strip.append({"name": e["lead"], "flex": e["hi"] - e["lo"] + 1, "odd": idx % 2, "dates": e["dates"]})

    result = {
        "eras": eras,
        "strip": strip,
        "k": len(eras),
        "range": f"{month_label(lo, True)} – {month_label(hi, True)}",
        "discovery": discovery_chart(months, first, plays, eras),
        "loyalty": loyalty(tl, months, eras, plays),
        "lines": artist_lines(months, eras),
    }
    for e in eras:            # the mix was only needed for the calculations; don't send it to the page
        e.pop("mix")
    return result
