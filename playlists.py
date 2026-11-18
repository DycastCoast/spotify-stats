"""
Playlists made from how people actually listen (Spotify no longer gives small apps its
recommendations or "mood" data like energy/tempo, so everything here comes from history).

The core idea: songs played close together in the same listening session belong together.
_graph() links every song to the songs that keep turning up near it. Groups in that network
(found with the Louvain method, networkx) are a person's real "moods". The slider sets how
fine the groups are (Louvain's "resolution": low = a few broad groups, high = many tight ones).

Playlist kinds (each returns {"title", "subtitle", "tags", "songs": [...]}):
  mood        one of your groups            right_now   the group that fits this moment
  like        more like a chosen song (your + friends' sessions, so includes songs new to you)
  blend       you + one friend              group       you + several friends
  way_in      a friend's music, starting from what's closest to your taste
  forgotten   loved, then dropped           comfort     ones you keep coming back to
  season      a season of a past year
"""
import json
import math
from collections import Counter, defaultdict, deque
from datetime import datetime
from functools import lru_cache

import networkx as nx
from networkx.algorithms.community import louvain_communities

import entity
import stats
from db import connect
from diskcache import disk_cached
from entity import BANDS, SESSION_GAP_S, band_of, local_ts
from stats import MIN_PLAY_MS

MIN_PLAYS = 5            # a song needs this many real listens to be in someone's network
WINDOW = 4               # each play links to the 4 songs before it in the same session
TOP_LINKS = 25           # keep each song's 25 strongest links (the rest is noise)
LEVELS = {1: 0.5, 2: 1.0, 3: 2.0, 4: 3.5}   # slider: broad ... specific (Louvain resolution)
LEVEL_NAMES = {1: "Broad", 2: "Balanced", 3: "Specific", 4: "Very specific"}
MIN_GROUP = 12           # smaller groups aren't shown as moods
SEASONS = {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
           6: "summer", 7: "summer", 8: "summer", 9: "autumn", 10: "autumn", 11: "autumn"}
DAY = 86400
STYLES = [("sped up", "Sped up"), ("slowed", "Slowed"), ("nightcore", "Nightcore"), ("remix", "Remixes"),
          ("acoustic", "Acoustic"), ("live", "Live"), ("instrumental", "Instrumental")]


def key_of(tl, t):
    name, artist, _ = tl.track_info[t]
    return name.lower(), artist


# ------------------------------------------------------------------ the "played together" network

def graph(user_id):
    return _graph(user_id, stats.latest_play_id(user_id))


@lru_cache(maxsize=6)
@disk_cached(versions=1)
def _graph(user_id, _latest):
    """{"links": {song: [(other song, weight), ...]}, "plays": Counter, "skip": {song: rate},
    "last": {song: time of last listen}}. Songs are timeline song numbers."""
    tl = entity.timeline(user_id)
    n = len(tl.track)
    plays = Counter()
    skips, known = Counter(), Counter()
    last = {}
    for i in range(n):
        t = tl.track[i]
        if tl.skip[i] >= 0:
            known[t] += 1
            skips[t] += tl.skip[i]
        if tl.ms[i] >= MIN_PLAY_MS:
            plays[t] += 1
            last[t] = tl.end[i]
    keep = {t for t, c in plays.items() if c >= MIN_PLAYS}

    weight = defaultdict(float)
    recent = deque(maxlen=WINDOW)
    prev_end = None
    for i in range(n):
        if tl.ms[i] < MIN_PLAY_MS:
            continue
        start = tl.end[i] - tl.ms[i] / 1000
        if prev_end is None or start - prev_end > SESSION_GAP_S:
            recent.clear()                     # a new session: nothing before it counts
        prev_end = tl.end[i]
        t = tl.track[i]
        if t not in keep:
            continue
        for distance, u in enumerate(reversed(recent), 1):
            if u != t:
                a, b = (t, u) if t < u else (u, t)
                weight[(a, b)] += 1 / distance  # right next to each other counts most
        if not recent or recent[-1] != t:      # the same song on repeat isn't "together"
            recent.append(t)

    links = defaultdict(list)
    for (a, b), w in weight.items():
        if w < 1.5:
            continue                           # together once is a coincidence
        # Popular songs are next to everything; dividing by both songs' play counts
        # finds songs that are next to EACH OTHER more than chance.
        score = w / math.sqrt(plays[a] * plays[b])
        links[a].append((b, score))
        links[b].append((a, score))
    for t in links:
        links[t] = sorted(links[t], key=lambda x: -x[1])[:TOP_LINKS]
    skip = {t: skips[t] / known[t] for t in known if known[t] >= 4}
    return {"links": dict(links), "plays": plays, "skip": skip, "last": last}


# ------------------------------------------------------------------ moods (groups)

def moods(user_id, level=2):
    level = level if level in LEVELS else 2
    groups = _moods(user_id, level, stats.latest_play_id(user_id))
    names = saved_names(user_id, level)
    out = []
    for g in groups:
        name = next((n for sig, n in names if len(set(sig) & set(g["members_ids"])) >= 3), None)
        out.append({**g, "name": name or g["auto_name"], "renamed": bool(name)})
    return out


@lru_cache(maxsize=16)
@disk_cached(versions=1)
def _moods(user_id, level, _latest):
    tl = entity.timeline(user_id)
    g = graph(user_id)
    G = nx.Graph()
    for a, lst in g["links"].items():
        for b, w in lst:
            if a < b:
                G.add_edge(a, b, weight=w)
    if G.number_of_nodes() < MIN_GROUP:
        return []
    found = louvain_communities(G, weight="weight", resolution=LEVELS[level], seed=7)
    overall = context_profile(tl, range(len(tl.track)))
    groups = []
    for members in found:
        if len(members) < MIN_GROUP:
            continue
        groups.append(describe(tl, g, G, members, overall))
    groups.sort(key=lambda x: -x["plays"])
    return groups


def context_profile(tl, indexes):
    """Shares of listens by time of day, weekend, season and year, plus skip rate."""
    band, weekend, season, year = Counter(), 0, Counter(), Counter()
    total = skipped = known = 0
    for i in indexes:
        if tl.skip[i] >= 0:
            known += 1
            skipped += tl.skip[i]
        if tl.ms[i] < MIN_PLAY_MS:
            continue
        d = datetime.utcfromtimestamp(local_ts(tl.end[i]))
        band[band_of(d.hour)] += 1
        weekend += d.weekday() >= 5
        season[SEASONS[d.month]] += 1
        year[d.year] += 1
        total += 1
    total = total or 1
    return {"band": {b: band[b] / total for b, *_ in BANDS}, "weekend": weekend / total,
            "season": {s: season[s] / total for s in ("winter", "spring", "summer", "autumn")},
            "year": year, "skip": skipped / known if known >= 20 else None, "total": total}


def describe(tl, g, G, members, overall):
    """What makes this group distinctive: when it's played, which years, skips; and its songs ranked."""
    plays_idx = [i for t in members for i in tl.by_track[t]]
    p = context_profile(tl, plays_idx)
    tags = []
    best_band = max(p["band"], key=lambda b: p["band"][b] / (overall["band"][b] or 1))
    band_ratio = p["band"][best_band] / (overall["band"][best_band] or 1)
    when = best_band.capitalize() if band_ratio >= 1.3 else None
    if when:
        tags.append(when)
    wk = p["weekend"] / (overall["weekend"] or 1)
    if wk >= 1.25:
        tags.append("Weekends")
    elif wk <= 0.75:
        tags.append("Weekdays")
    best_season = max(p["season"], key=lambda s: p["season"][s] / (overall["season"][s] or 1))
    if p["season"][best_season] / (overall["season"][best_season] or 1) >= 1.35:
        tags.append(best_season.capitalize())
    years = sorted(p["year"])
    if years:
        # the years holding the middle 80% of this group's listening
        cum, total, lo, hi = 0, sum(p["year"].values()), None, None
        for y in years:
            cum += p["year"][y]
            if lo is None and cum >= total * 0.1:
                lo = y
            if hi is None and cum >= total * 0.9:
                hi = y
        latest_year = max(overall["year"])
        tags.append(f"Since {lo}" if hi >= latest_year and lo < latest_year else (str(lo) if lo == hi else f"{lo}–{hi}"))
    if p["skip"] is not None and overall["skip"]:
        if p["skip"] <= overall["skip"] * 0.6:
            tags.append("Rarely skipped")
        elif p["skip"] >= overall["skip"] * 1.4:
            tags.append("Often skipped")

    artists = Counter()
    for t in members:
        artists[tl.track_info[t][1]] += g["plays"][t]
    top_artists = [a for a, _ in artists.most_common(3)]
    who = " + ".join(top_artists[:2])
    # A style that runs through the group's song titles (sped up, slowed, remixes...)
    style = None
    for word, label in STYLES:
        if sum(1 for t in members if word in tl.track_info[t][0].lower()) >= max(4, 0.25 * len(members)):
            style = label
            tags.insert(0, label)
            break
    lead = style or when
    auto = f"{lead} · {who}" if lead else who

    # Rank songs: how central they are to the group, played a lot, not usually skipped.
    def score(t):
        inside = sum(w for u, w in g["links"].get(t, []) if u in members)
        return inside * math.log1p(g["plays"][t]) * (1 - 0.8 * g["skip"].get(t, 0))
    ranked = sorted(members, key=score, reverse=True)
    covers, seen_albums = [], set()
    for t in ranked:
        album = (tl.track_info[t][2] or tl.track_info[t][0]).lower()
        if album not in seen_albums:
            seen_albums.add(album)
            covers.append(tl.track_ids[t])
        if len(covers) == 4:
            break
    return {"covers": covers,"auto_name": auto, "tags": tags, "artists": top_artists, "size": len(members),
            "plays": sum(g["plays"][t] for t in members),
            "hours": round(sum(tl.track_ms[t] for t in members) / 3_600_000),
            "songs": ranked, "members_ids": [tl.track_ids[t] for t in ranked[:6]],
            "id": tl.track_ids[ranked[0]], "profile": {"band": p["band"], "weekend": p["weekend"], "season": p["season"]}}


def saved_names(user_id, level):
    with connect() as conn:
        rows = conn.execute("SELECT signature, name FROM mood_names WHERE user_id = ? AND level = ?",
                            (user_id, level)).fetchall()
    return [(json.loads(r["signature"]), r["name"]) for r in rows]


def rename(user_id, level, group_id, name):
    """Remember a name for a group. Groups get worked out again as new plays arrive, so the name
    is tied to the group's 6 most central songs and follows whichever group still has 3 of them."""
    group = next((g for g in _moods(user_id, level, stats.latest_play_id(user_id)) if g["id"] == group_id), None)
    if not group:
        return False
    sig = group["members_ids"]
    with connect() as conn:
        for row in conn.execute("SELECT rowid, signature FROM mood_names WHERE user_id = ? AND level = ?",
                                (user_id, level)).fetchall():
            if len(set(json.loads(row["signature"])) & set(sig)) >= 3:
                conn.execute("DELETE FROM mood_names WHERE rowid = ?", (row["rowid"],))
        if name.strip():
            conn.execute("INSERT INTO mood_names (user_id, level, signature, name) VALUES (?, ?, ?, ?)",
                         (user_id, level, json.dumps(sig), name.strip()[:60]))
    return True


# ------------------------------------------------------------------ turning song lists into playlists

def covers_of(songs, n=4):
    """Up to n covers for a playlist's 2×2: one per album, in playlist order."""
    out, seen = [], set()
    for s in songs:
        album = (s.get("album") or s["name"]).lower()
        if album not in seen:
            seen.add(album)
            out.append(s["track"])
        if len(out) == n:
            break
    return out


def song_rows(tl, ts, viewer_tl=None, notes=None, limit=30):
    """Song numbers (in tl) -> rows for the page: id, name, artist, plays, note."""
    rows, seen = [], set()
    for t in ts:
        k = key_of(tl, t)
        if k in seen:
            continue
        seen.add(k)
        name, artist, album = tl.track_info[t]
        rows.append({"track": tl.track_ids[t], "name": name, "artist": artist, "album": album,
                     "note": (notes or {}).get(t, "")})
        if len(rows) == limit:
            break
    return rows


def mood_playlist(user_id, level, group_id, limit=30):
    g = next((x for x in moods(user_id, level) if x["id"] == group_id), None)
    if not g:
        return None
    tl = entity.timeline(user_id)
    return {"title": g["name"], "subtitle": f"{g['size']} songs in this mood · {g['hours']} h listened",
            "tags": g["tags"], "songs": song_rows(tl, g["songs"], limit=limit), "group": g}


def right_now(user_id, level=2, when=None, limit=30):
    """The mood that best fits now: time of day, weekday/weekend and season, compared with how
    much each mood is usually played at such times."""
    groups = moods(user_id, level)
    if not groups:
        return None
    d = when or datetime.utcfromtimestamp(local_ts(datetime.now().timestamp()))
    band, weekend, season = band_of(d.hour), d.weekday() >= 5, SEASONS[d.month]
    def fit(g):
        pr = g["profile"]
        wk = pr["weekend"] if weekend else 1 - pr["weekend"]
        return (pr["band"][band] + 0.01) * (wk + 0.01) * (pr["season"][season] + 0.01) * math.log1p(g["plays"])
    best = max(groups, key=fit)
    pl = mood_playlist(user_id, level, best["id"], limit)
    day = "weekend" if weekend else d.strftime("%A")
    phrase = next(ph for label, lo, hi, ph in BANDS if label == band)
    pl["subtitle"] = f"For a {day} {phrase} in {season}: the mood you usually reach for now"
    return pl


def affinity(user_id):
    """{song key: 0-1} how much this person likes each song (plays, scaled to their top song,
    gently: square root, so the 300-play songs don't drown everything else)."""
    g = graph(user_id)
    tl = entity.timeline(user_id)
    top = max(g["plays"].values() or [1])
    return {key_of(tl, t): math.sqrt(c / top) for t, c in g["plays"].items() if c >= 2}


def like(viewer, track_id, only_new=False, limit=30):
    """More like this song: what's played around it in the viewer's sessions AND friends'
    (so it can suggest songs the viewer has never played). Two steps out, at lower weight."""
    from nowplaying import find
    seed = None
    scores, by, rep = Counter(), defaultdict(set), {}
    names = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
    vtl = entity.timeline(viewer)
    vplays = {key_of(vtl, t): c for t, c in graph(viewer)["plays"].items()}
    for person in names:
        tl = entity.timeline(person)
        g = graph(person)
        if seed is None and person == viewer:
            pass
        t0 = tl.index_of.get(track_id)
        if t0 is None and seed:
            t0 = tl.song_of.get(seed)
        if t0 is None:
            continue
        seed = seed or key_of(tl, t0)
        mine = 1.0 if person == viewer else 0.8
        first = g["links"].get(t0, [])
        top = first[0][1] if first else 1
        for u, w in first:
            k = key_of(tl, u)
            scores[k] += mine * w / top
            by[k].add(person)
            rep.setdefault(k, (tl, u))
            for v, w2 in g["links"].get(u, [])[:8]:          # two steps out, weaker
                if v == t0:
                    continue
                k2 = key_of(tl, v)
                scores[k2] += mine * 0.3 * (w / top) * (w2 / g["links"][u][0][1])
                by[k2].add(person)
                rep.setdefault(k2, (tl, v))
    if seed is None:
        return None
    scores.pop(seed, None)
    rows = []
    for k, s in scores.most_common():
        new = vplays.get(k, 0) < 2
        if only_new and not new:
            continue
        tl, t = rep[k]
        friends = sorted(names[p] for p in by[k] if p != viewer)
        note = ("New to you" + (f" · from {', '.join(friends[:2])}" if friends else "")) if new else \
               (f"Also in {', '.join(friends[:2])}'s sessions" if friends else "")
        rows += song_rows(tl, [t], notes={t: note})
        if len(rows) == limit:
            break
    seed_name = f"{seed[0].title() if seed[0].islower() else seed[0]}"
    for person in names:                                   # proper capitalisation of the seed
        tl = entity.timeline(person)
        t = tl.index_of.get(track_id) or tl.song_of.get(seed)
        if t is not None:
            seed_name = tl.track_info[t][0]
            break
    return {"title": f"More like {seed_name}", "subtitle": f"Played around it by you{' and friends' if len(names) > 1 else ''}"
            + (" · only songs new to you" if only_new else ""), "tags": [], "songs": rows, "seed": track_id}


def _people_rows(person_ids, scores, owners, limit):
    out, used = [], set()
    for k, s in scores:
        if k in used:
            continue
        used.add(k)
        pid, note = owners[k]
        tl = entity.timeline(pid)
        t = tl.song_of.get(k)
        if t is None:
            continue
        out += song_rows(tl, [t], notes={t: note})
        if len(out) == limit:
            break
    return out


def blend(viewer, friend, limit=30):
    """You + a friend: songs you both love, then each person's favourites the other barely plays,
    taken in turns so it's fair."""
    names = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
    a, b = affinity(viewer), affinity(friend)
    shared = sorted(((k, min(a[k], b[k])) for k in a.keys() & b.keys()), key=lambda x: -x[1])
    a_only = sorted(((k, v) for k, v in a.items() if b.get(k, 0) < 0.05), key=lambda x: -x[1])
    b_only = sorted(((k, v) for k, v in b.items() if a.get(k, 0) < 0.05), key=lambda x: -x[1])
    owners = {}
    for k, _ in shared:
        owners[k] = (viewer, "You both love it")
    for k, _ in a_only:
        owners.setdefault(k, (viewer, f"Yours · {names[friend]} hasn't really heard it"))
    for k, _ in b_only:
        owners.setdefault(k, (friend, f"{names[friend]}'s · new to you"))
    order, lists = [], [shared, a_only, shared, b_only]
    pos = [0, 0, 0, 0]
    while len(order) < limit * 2 and any(pos[i] < len(lists[i]) for i in range(4)):
        for i in range(4):
            if pos[i] < len(lists[i]):
                order.append(lists[i][pos[i]])
                pos[i] += 1
    return {"title": f"You + {names[friend]}", "subtitle": "Songs you both love, plus each other's favourites the other hasn't really heard",
            "tags": [], "songs": _people_rows([viewer, friend], order, owners, limit)}


def group_mix(viewer, friends, limit=30):
    """Several people: favour songs more of the group like, and take turns so nobody's left out."""
    people = [viewer] + [f for f in friends if f != viewer]
    names = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
    aff = {p: affinity(p) for p in people}
    keys = set().union(*aff.values())
    def score(k):
        likes = [aff[p].get(k, 0) for p in people]
        fans = sum(1 for x in likes if x >= 0.1)
        return sum(likes) / len(people) * (1 + 0.6 * (fans - 1))
    scored = {k: score(k) for k in keys}
    ranked_by = {p: sorted((k for k in aff[p]), key=lambda k: -scored[k]) for p in people}
    owners, order, pos, used = {}, [], {p: 0 for p in people}, set()
    while len(order) < limit * 2 and any(pos[p] < len(ranked_by[p]) for p in people):
        for p in people:
            while pos[p] < len(ranked_by[p]) and ranked_by[p][pos[p]] in used:
                pos[p] += 1
            if pos[p] < len(ranked_by[p]):
                k = ranked_by[p][pos[p]]
                used.add(k)
                fans = [names[q] if q != viewer else "you" for q in people if aff[q].get(k, 0) >= 0.1]
                owners[k] = (p, "Loved by " + (", ".join(fans) if len(fans) < len(people) else "everyone"))
                order.append((k, scored[k]))
    who = ", ".join(names[p] for p in people[1:])
    return {"title": f"Group mix: you, {who}", "subtitle": "Songs more of the group like come first; everyone gets a turn",
            "tags": [], "songs": _people_rows(people, order, owners, limit)}


def way_in(viewer, friend, limit=30):
    """A friend's music you don't know yet, starting with what sits closest to your taste:
    their songs that get played (by them) next to songs YOU love."""
    names = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
    mine = affinity(viewer)
    ftl, fg = entity.timeline(friend), graph(friend)
    ftop = max(fg["plays"].values() or [1])
    scored = []
    for t, c in fg["plays"].items():
        k = key_of(ftl, t)
        if mine.get(k, 0) >= 0.05 or c < MIN_PLAYS:
            continue
        near = [(u, w) for u, w in fg["links"].get(t, []) if mine.get(key_of(ftl, u), 0) >= 0.1]
        bridge = sum(w * mine[key_of(ftl, u)] for u, w in near)
        if bridge:
            scored.append((t, bridge * (0.5 + math.sqrt(c / ftop)), near[0][0] if near else None))
    scored.sort(key=lambda x: -x[1])
    notes = {t: (f"{names[friend]} plays it next to {ftl.track_info[u][0]}" if u is not None else "") for t, _, u in scored}
    return {"title": f"A way into {names[friend]}'s music", "subtitle": f"{names[friend]}'s songs you haven't played, closest to your taste first",
            "tags": [], "songs": song_rows(ftl, [t for t, _, _ in scored], notes=notes, limit=limit)}


def forgotten(user_id, limit=30):
    """Songs you loved and then stopped playing (25+ listens, nothing for 6+ months)."""
    tl, g = entity.timeline(user_id), graph(user_id)
    newest = max(g["last"].values() or [0])
    cands = [(t, c) for t, c in g["plays"].items() if c >= 25 and newest - g["last"][t] >= 180 * DAY]
    cands.sort(key=lambda x: -x[1])
    notes = {t: f"{c} plays · last {datetime.utcfromtimestamp(local_ts(g['last'][t])).strftime('%b %Y')}" for t, c in cands}
    return {"title": "Forgotten favourites", "subtitle": "Songs you played a lot and then left behind (nothing for 6+ months)",
            "tags": [], "songs": song_rows(tl, [t for t, _ in cands], notes=notes, limit=limit)}


def comfort(user_id, limit=30):
    """Songs you keep coming back to after long breaks (3+ returns after 2+ months away)."""
    tl, g = entity.timeline(user_id), graph(user_id)
    out = []
    for t, c in g["plays"].items():
        if c < 15:
            continue
        ends = [tl.end[i] for i in tl.by_track[t] if tl.ms[i] >= MIN_PLAY_MS]
        returns = sum(1 for a, b in zip(ends, ends[1:]) if b - a >= 60 * DAY)
        if returns >= 3:
            years = datetime.utcfromtimestamp(ends[-1]).year - datetime.utcfromtimestamp(ends[0]).year
            out.append((t, returns * math.log1p(c), returns, years))
    out.sort(key=lambda x: -x[1])
    notes = {t: f"Came back to it {r} times" + (f" over {y} years" if y >= 2 else "") for t, _, r, y in out}
    return {"title": "Comfort songs", "subtitle": "The ones you keep coming back to after months away",
            "tags": [], "songs": song_rows(tl, [t for t, *_ in out], notes=notes, limit=limit)}


def season(user_id, year, which, limit=30):
    """Your soundtrack for one season of one year (winter = December before + January, February)."""
    months = {"winter": [(year - 1, 12), (year, 1), (year, 2)], "spring": [(year, m) for m in (3, 4, 5)],
              "summer": [(year, m) for m in (6, 7, 8)], "autumn": [(year, m) for m in (9, 10, 11)]}.get(which)
    if not months:
        return None
    tl = entity.timeline(user_id)
    want = set(months)
    plays, skips, known = Counter(), Counter(), Counter()
    for i in range(len(tl.track)):
        d = datetime.utcfromtimestamp(local_ts(tl.end[i]))
        if (d.year, d.month) not in want:
            continue
        t = tl.track[i]
        if tl.skip[i] >= 0:
            known[t] += 1
            skips[t] += tl.skip[i]
        if tl.ms[i] >= MIN_PLAY_MS:
            plays[t] += 1
    ranked = [t for t, c in plays.most_common() if c >= 2 and not (known[t] >= 4 and skips[t] / known[t] > 0.6)]
    notes = {t: f"{plays[t]} plays that {which}" for t in ranked}
    return {"title": f"{which.capitalize()} {year}", "subtitle": f"What you were playing in {which} {year}",
            "tags": [], "songs": song_rows(tl, ranked, notes=notes, limit=limit)}


def seasons_available(user_id):
    """(year, season) pairs with enough listening to make a playlist, newest first."""
    tl = entity.timeline(user_id)
    count = Counter()
    for i in range(0, len(tl.track), 7):                  # a sample is plenty to know which exist
        d = datetime.utcfromtimestamp(local_ts(tl.end[i]))
        y = d.year + (1 if d.month == 12 else 0)
        count[(y, SEASONS[d.month])] += 1
    order = {"winter": 0, "spring": 1, "summer": 2, "autumn": 3}
    return sorted((k for k, c in count.items() if c >= 30), key=lambda k: (k[0], order[k[1]]), reverse=True)


# ------------------------------------------------------------------ saving into Spotify

SAVE_SCOPE = "playlist-modify-private"


def can_save(user_id):
    """Did this person log in after 'Save to Spotify' was added? (Checked from their saved token.)"""
    with connect() as conn:
        row = conn.execute("SELECT token_info FROM users WHERE id = ?", (user_id,)).fetchone()
    if not row or not row["token_info"]:
        return False
    return SAVE_SCOPE in (json.loads(row["token_info"]).get("scope") or "").split()


def save_to_spotify(user_id, title, description, track_ids):
    """Create a private playlist in this person's Spotify and fill it. Two requests.
    Returns the playlist's link. Raises spotipy.SpotifyException on Spotify errors."""
    import re

    from db import DatabaseCacheHandler
    from spotify_client import make_client, pause_after, paused
    if paused():
        raise RuntimeError("Spotify asked us to wait a bit (too many requests). Try again later.")
    real = [t for t in track_ids if re.fullmatch(r"[0-9A-Za-z]{22}", t)][:100]   # Apple songs can't go in
    if not real:
        raise RuntimeError("None of these songs are on Spotify.")
    sp = make_client(DatabaseCacheHandler(user_id))
    try:
        # Feb 2026 API: POST /me/playlists and POST /playlists/{id}/items (the old /users/... and
        # /tracks paths are gone for small apps), so these are called directly.
        playlist = sp._post("me/playlists", payload={"name": title[:100], "public": False,
                                                     "description": description[:280]})
        sp._post(f"playlists/{playlist['id']}/items", payload={"uris": [f"spotify:track:{t}" for t in real]})
    except Exception as err:
        pause_after(err)
        raise
    return (playlist.get("external_urls") or {}).get("spotify") or f"https://open.spotify.com/playlist/{playlist['id']}"
