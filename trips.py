"""
Trips abroad, worked out from the country Spotify logged with each play
(conn_country, export data only), and the soundtrack of each one.

PRIVATE: trips say where someone was, so the page only ever shows the
logged-in person their OWN trips (see app.py).

Rules, and why:
- "ZZ" means Spotify didn't know the country (smart speakers report this), so it's ignored.
- A phone on a home SIM abroad often still reports the home country while roaming.
  So a day counts as "away" if the other country is at least a quarter of that day's
  plays, not most of them, and a home-looking day or two doesn't end a trip.
- A trip needs 2+ away days: a single odd day is more likely a VPN or a network quirk.
- VPNs can still create a fake trip now and then; the page says so.
"""
import bisect
import math
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from functools import lru_cache

import entity
import geo
from compare import COUNTRIES
from db import connect
from entity import local_ts
from stats import LOCAL_TZ, MIN_PLAY_MS, latest_play_id

AWAY_SHARE = 0.25      # the other country is at least this share of a day's plays
AWAY_MIN_PLAYS = 3     # ... and at least this many plays
MERGE_GAP_DAYS = 4     # away days of the same country this close together are one trip
MIN_TRIP_DAYS = 2      # a trip needs at least this many away days
CHAIN_DAYS = 2         # a trip starting this soon after another (elsewhere) is part of one journey
TRACE_GAP_DAYS = 3     # faint traces this close together belong to the same short trip
NOISE_DAYS = 5         # a country that shows up on its own on this many separate days is VPN/network noise
LONG_STAY_DAYS = 30    # this long or more reads as "a long stay", not a holiday

NAMES = {**COUNTRIES, "GB": "the UK", "BE": "Belgium", "CH": "Switzerland", "AT": "Austria", "PL": "Poland",
         "CZ": "Czechia", "HU": "Hungary", "HR": "Croatia", "RO": "Romania", "BG": "Bulgaria", "DK": "Denmark",
         "SE": "Sweden", "NO": "Norway", "FI": "Finland", "IS": "Iceland", "MA": "Morocco", "EG": "Egypt",
         "TN": "Tunisia", "CV": "Cape Verde", "CA": "Canada", "MX": "Mexico", "BR": "Brazil", "AR": "Argentina",
         "AE": "the UAE", "TH": "Thailand", "VN": "Vietnam", "ID": "Indonesia", "AU": "Australia",
         "NZ": "New Zealand", "KR": "South Korea", "SG": "Singapore", "IN": "India", "ZA": "South Africa",
         "CY": "Cyprus", "MT": "Malta", "SI": "Slovenia", "SK": "Slovakia", "ME": "Montenegro", "AL": "Albania",
         "LU": "Luxembourg", "EE": "Estonia", "LV": "Latvia", "LT": "Lithuania", "TW": "Taiwan", "MY": "Malaysia",
         "PH": "the Philippines", "QA": "Qatar", "SA": "Saudi Arabia", "IL": "Israel", "JO": "Jordan"}


def country_name(code):
    return NAMES.get(code, code)


def heading(name):
    """'the US' -> 'The US' for the start of a line (str.capitalize would give 'The us')."""
    return name[:1].upper() + name[1:]


def day_of(ts):
    """The UK calendar day of a UTC timestamp."""
    return date(1970, 1, 1) + timedelta(days=int(local_ts(ts) // 86400))


def located_days(user_id):
    """Day -> Counter of countries, from plays that have a known country."""
    days = defaultdict(Counter)
    with connect() as conn:
        rows = conn.execute(
            """SELECT (julianday(played_at) - 2440587.5) * 86400.0, conn_country
               FROM plays WHERE user_id = ? AND conn_country IS NOT NULL AND conn_country != 'ZZ'
                 AND ms_played >= ?""", (user_id, MIN_PLAY_MS))
        for ts, country in rows:
            days[day_of(ts)][country] += 1
    return days


def find_trips(days):
    if not days:
        return None, [], {"noise": [], "maybes": []}
    totals = Counter()
    for c in days.values():
        totals.update(c)
    home = totals.most_common(1)[0][0]

    away = {}
    for d, c in days.items():
        n_located = sum(c.values())
        foreign = [(cc, n) for cc, n in c.most_common() if cc != home]
        if foreign:
            cc, n = foreign[0]
            if n >= AWAY_MIN_PLAYS and n / n_located >= AWAY_SHARE:
                away[d] = cc

    trips = []
    for d in sorted(away):
        cc = away[d]
        if trips and trips[-1]["country"] == cc and (d - trips[-1]["end"]).days <= MERGE_GAP_DAYS:
            trips[-1]["end"] = d
            trips[-1]["away_days"] += 1
        else:
            trips.append({"country": cc, "start": d, "end": d, "away_days": 1})
    trips = [t for t in trips if t["away_days"] >= MIN_TRIP_DAYS]
    for t in trips:
        t["stops"] = [t["country"]]
    maybes = add_short_stops(days, home, trips)
    trips.sort(key=lambda t: t["start"])
    for prev, t in zip(trips, trips[1:]):
        if (t["start"] - prev["end"]).days <= CHAIN_DAYS and t["country"] != prev["country"]:
            t["came_from"] = prev["country"]
    return home, trips, maybes


def add_short_stops(days, home, trips):
    """Catch short trips that the main rule misses because the phone mostly reported home
    (e.g. a day each in Lithuania and Estonia), without letting VPNs in.

    - A "trace" is any day outside a trip with at least one play from another country.
    - Countries that show up on their own on many separate days are this person's VPN or
      network noise (VPN apps keep picking the same few countries), so they're ignored here.
    - Remaining traces a few days apart are chained:
        2+ different countries        -> a short multi-country trip
        1 country on 2+ days          -> a short trip
        1 day next to a trip there    -> that trip is extended to include it
        1 day next to a trip elsewhere -> a stopover on that trip
      Anything else is listed as a "maybe", not counted.
    """
    in_trip = set()
    for t in trips:
        d = t["start"]
        while d <= t["end"]:
            in_trip.add(d)
            d += timedelta(days=1)
    traces = {}
    for d, c in days.items():
        if d in in_trip:
            continue
        foreign = [(cc, n) for cc, n in c.most_common() if cc != home]
        if foreign:
            traces[d] = foreign[0]
    seen = Counter(cc for cc, _ in traces.values())
    noise = {cc for cc, n in seen.items() if n >= NOISE_DAYS}
    keep = {d: x for d, x in traces.items() if x[0] not in noise}

    chains = []
    for d in sorted(keep):
        if chains and (d - chains[-1][-1]).days <= TRACE_GAP_DAYS:
            chains[-1].append(d)
        else:
            chains.append([d])
    maybes = []
    for ch in chains:
        stops = list(dict.fromkeys(keep[d][0] for d in ch))
        plays = sum(keep[d][1] for d in ch)
        near = [t for t in trips if -TRACE_GAP_DAYS <= (ch[0] - t["end"]).days <= TRACE_GAP_DAYS
                or -TRACE_GAP_DAYS <= (t["start"] - ch[-1]).days <= TRACE_GAP_DAYS]
        near.sort(key=lambda t: t["country"] != stops[0])          # a trip to the same country first
        if len(stops) >= 2 or len(ch) >= 2:
            trips.append({"country": stops[0], "stops": stops, "start": ch[0], "end": ch[-1],
                          "away_days": len(ch), "faint": plays})
        elif near and near[0]["country"] == stops[0]:
            t = near[0]
            t["start"], t["end"] = min(t["start"], ch[0]), max(t["end"], ch[-1])
        elif near:
            near[0].setdefault("stopovers", []).append(stops[0])
        else:
            maybes.append({"date": ch[0], "country": stops[0], "plays": plays})
    return {"noise": sorted(noise), "maybes": maybes}


def bounds(d0, d1):
    """UTC timestamps for the start of UK day d0 and the end of UK day d1."""
    lo = datetime(d0.year, d0.month, d0.day, tzinfo=LOCAL_TZ).timestamp()
    end = d1 + timedelta(days=1)
    return lo, datetime(end.year, end.month, end.day, tzinfo=LOCAL_TZ).timestamp()


def window(tl, lo, hi):

    return range(bisect.bisect_left(tl.end, lo), bisect.bisect_left(tl.end, hi))


def soundtrack(tl, trip, first_listen_artist, artist_plays):
    """What you played on a trip, what made it different from normal, and what you found there."""
    lo, hi = bounds(trip["start"], trip["end"])
    songs, artists, ms_total = Counter(), Counter(), 0
    for i in window(tl, lo, hi):
        if tl.ms[i] >= MIN_PLAY_MS:
            t = tl.track[i]
            songs[t] += 1
            artists[tl.track_info[t][1]] += tl.ms[i]
            ms_total += tl.ms[i]
    if not songs:
        return None
    # The 60 days before, to tell "the song of this trip" from songs you always play.
    before = Counter()
    for i in window(tl, lo - 60 * 86400, lo):
        if tl.ms[i] >= MIN_PLAY_MS:
            before[tl.track[i]] += 1
    days_before = len({day_of(tl.end[i]) for i in window(tl, lo - 60 * 86400, lo) if tl.ms[i] >= MIN_PLAY_MS}) or 1
    ms_before = sum(tl.ms[i] for i in window(tl, lo - 60 * 86400, lo) if tl.ms[i] >= MIN_PLAY_MS)

    def distinct(t):
        return songs[t] * songs[t] / (before[t] + 1)
    trip_song = max((t for t in songs if songs[t] >= 5), key=distinct, default=None)
    if trip_song is not None and before[trip_song] >= songs[trip_song]:
        trip_song = None                       # nothing stood out: you played it as much before

    found = [a for a, (m, i) in first_listen_artist.items() if lo <= tl.end[i] < hi and artist_plays[a] >= 10]
    found.sort(key=lambda a: -tl.artist_ms[a])
    n_days = (trip["end"] - trip["start"]).days + 1
    per_day = ms_total / n_days
    usual = ms_before / days_before if ms_before else None

    def song(t, n=None):
        name, artist, _ = tl.track_info[t]
        return {"name": name, "artist": artist, "track": tl.track_ids[t], "plays": songs[t] if n is None else n}
    return {
        "hours": ms_total / 3_600_000,
        "per_day_min": per_day / 60000,
        "vs_usual": per_day / usual if usual else None,
        "top": [song(t) for t, _ in songs.most_common(5)],
        "covers": covers(tl, songs),
        "trip_song": ({**song(trip_song), "before": before[trip_song]} if trip_song is not None else None),
        "top_artist": artists.most_common(1)[0][0],
        "top_artist_track": tl.track_ids[tl.artist_best[artists.most_common(1)[0][0]]],
        "found": [{"name": a, "track": tl.track_ids[tl.artist_best[a]], "hours": tl.artist_ms[a] / 3_600_000}
                  for a in found[:3]],
        "found_count": len(found),
    }


def covers(tl, songs, n=4):
    """Up to n different albums from the trip's most-played songs: their covers make the
    trip's 2×2 picture on the map (one song per album, so it's 4 albums, not 4 songs of one)."""
    out, seen = [], set()
    for t, _ in songs.most_common(60):
        name, artist, album = tl.track_info[t]
        key = ((album or name).lower(), artist)
        if key not in seen:
            seen.add(key)
            out.append(tl.track_ids[t])
        if len(out) == n:
            break
    return out


def trips(user_id):
    return _trips(user_id, latest_play_id(user_id))


@lru_cache(maxsize=4)
def _trips(user_id, _latest):
    days = located_days(user_id)
    home, found, extra = find_trips(days)
    if not found:
        return {"home": country_name(home) if home else None, "trips": [], "located_from": None}
    tl = entity.timeline(user_id)
    from journey import monthly
    _, first, plays = monthly(tl)

    out = []
    for t in found:
        n_days = (t["end"] - t["start"]).days + 1
        s = soundtrack(tl, t, first, plays)
        if s is None:
            continue
        names = [country_name(c) for c in t["stops"]]
        out.append({
            "country": " & ".join(names), "code": t["country"], "stops": t["stops"],
            "heading": heading(" & ".join(names) if len(names) <= 2 else ", ".join(names[:-1]) + " & " + names[-1]),
            "faint": t.get("faint"),
            "stopovers": [(c, country_name(c)) for c in dict.fromkeys(t.get("stopovers", []))],
            "dates": (f"{t['start'].day} – {t['end'].day} {t['end']:%b %Y}" if (t["start"].month, t["start"].year) == (t["end"].month, t["end"].year)
                      else f"{t['start'].day} {t['start']:%b} – {t['end'].day} {t['end']:%b %Y}" if t["start"].year == t["end"].year
                      else f"{t['start'].day} {t['start']:%b %Y} – {t['end'].day} {t['end']:%b %Y}"),
            "year": t["start"].year,
            "days": n_days,
            "long": n_days >= LONG_STAY_DAYS,
            "came_from": country_name(t["came_from"]) if t.get("came_from") else None,
            "came_from_code": t.get("came_from"),
            "pin": geo.pin(t["country"]),
            "route": [p for p in (geo.pin(c) for c in t["stops"]) if p] if len(t["stops"]) > 1 else [],
            **s,
        })

    by_country = defaultdict(lambda: {"trips": 0, "days": 0, "first": None, "code": None})
    for t in out:
        # A multi-country trip shares its days out between its countries (at least 1 each).
        for code in t["stops"] + [c for c, _ in t["stopovers"]]:
            c = by_country[country_name(code)]
            c["code"] = code
            c["trips"] += 1
            c["days"] += max(1, t["days"] // len(t["stops"])) if code in t["stops"] else 1
            c["first"] = c["first"] or t["year"]
    countries = sorted(({"name": k, "heading": heading(k), **v} for k, v in by_country.items()), key=lambda c: -c["days"])
    top_days = countries[0]["days"] if countries else 1
    for c in countries:
        c["bar"] = round(100 * c["days"] / top_days)

    per_year = Counter()
    for t in out:
        per_year[t["year"]] += t["days"]
    busiest = per_year.most_common(1)[0] if per_year else None
    longest = max(out, key=lambda t: t["days"]) if out else None
    newest_first = list(reversed(out))
    return {
        "home": country_name(home),
        "home_code": home,
        "trips": newest_first,
        "count": len(out),
        "total_days": sum(t["days"] for t in out),
        "countries": countries,
        "busiest_year": {"year": busiest[0], "days": busiest[1]} if busiest else None,
        "longest": longest,
        "located_from": min(days).strftime("%B %Y"),
        "years": sorted({t["year"] for t in out}, reverse=True),
        "noise": [(c, country_name(c)) for c in extra["noise"] if c != home and c not in {t["code"] for t in out}],
        "maybes": [{"date": f"{m['date'].day} {m['date']:%b %Y}", "code": m["country"], "country": country_name(m["country"]),
                    "plays": m["plays"]} for m in reversed(extra["maybes"])],
    }


# ------------------------------------------------------------------ trips together (opt-in, both people)

@lru_cache(maxsize=8)
def _raw_trips(user_id, _latest):
    return [t for t in find_trips(located_days(user_id))[1] if not t.get("faint")]   # together: solid trips only


def raw_trips(user_id):
    return _raw_trips(user_id, latest_play_id(user_id))


def together(me, other, other_name):
    """Trips where you were both in the same country on the same days (2+ days in common).

    Only the overlapping days are shown, never the rest of either person's trip.
    The caller must check that BOTH people have switched sharing on.
    """
    return _together(me, other, other_name, latest_play_id(me), latest_play_id(other))


@lru_cache(maxsize=4)
def _together(me, other, other_name, _l1, _l2):
    import compare
    mine, theirs = raw_trips(me), raw_trips(other)
    ta, tb = entity.timeline(me), entity.timeline(other)
    jam_sessions = compare.jams(ta, tb)
    out = []
    for a in mine:
        for b in theirs:
            if a["country"] != b["country"]:
                continue
            start, end = max(a["start"], b["start"]), min(a["end"], b["end"])
            days = (end - start).days + 1
            if days < 2:
                continue
            lo, hi = bounds(start, end)
            sa, sb = Counter(), Counter()
            for tl, c in ((ta, sa), (tb, sb)):
                for i in window(tl, lo, hi):
                    if tl.ms[i] >= MIN_PLAY_MS:
                        c[compare.song_key(tl, tl.track[i])] += 1
            idx = compare.song_index(me)
            both = sorted(sa.keys() & sb.keys(), key=lambda k: -(sa[k] + sb[k]))

            def song(k, n):
                t = idx.get(k)
                return {"name": ta.track_info[t][0] if t is not None else k[0], "artist": k[1],
                        "track": ta.track_ids[t] if t is not None else None, "plays": n}
            jams_here = [s for s in jam_sessions if lo <= ta.end[s[0][0]] < hi]
            out.append({
                "code": a["country"],
                "country": country_name(a["country"]),
                "heading": heading(country_name(a["country"])),
                "dates": (f"{start.day} – {end.day} {end:%b %Y}" if (start.month, start.year) == (end.month, end.year)
                          else f"{start.day} {start:%b} – {end.day} {end:%b %Y}"),
                "year": start.year,
                "days": days,
                "shared_songs": [song(k, sa[k] + sb[k]) for k in both[:5]],
                "shared_count": len(both),
                "my_top": song(*sa.most_common(1)[0]) if sa else None,
                "their_top": their_song(tb, other, *sb.most_common(1)[0]) if sb else None,
                "jams": len(jams_here),
                "jam_songs": sum(len(s) for s in jams_here),
            })
    out.reverse()                              # newest first (trips come oldest first)
    return {"with": other_name, "trips": out}


def their_song(tl, user_id, key, n):
    import compare
    t = compare.song_index(user_id).get(key)
    return {"name": tl.track_info[t][0] if t is not None else key[0], "artist": key[1], "plays": n}
