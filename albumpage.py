"""
One album: how you listen to it. Do you play it in one go, in order, which songs you
come back for, where you drop off, and what you skip.

Definitions (shown on the page too):
- A SITTING is a run of plays from this album back to back (nothing else in between,
  no gap over 30 minutes). Skipped songs count as part of the run.
- A FULL LISTEN is a sitting that covers at least 80% of the album's songs.
- IN ORDER: most steps from one song to the next go up by exactly one track, shuffle off.
  Needs the real tracklist (albums.py); without it, the album's size is estimated from
  the songs you've played from it, and order can't be judged.
"""
import math
import re
from collections import Counter
from functools import lru_cache

from entity import (MIN_PLAY_MS, SESSION_GAP_S, comeback, fmt_date, monthly_chart, timeline,
                    when_you_play)
from stats import latest_play_id

FULL_SHARE = 0.8         # a full listen covers 80%+ of the album's songs
IN_ORDER_SHARE = 0.75    # ... and "in order" if 75%+ of the steps are to the next track
ALBUM_RUN = 3            # 3+ songs from one album back to back = playing it "as an album"
INSTANT_S = 10           # skipped within 10 seconds = an instant skip


def clean(name):
    """Song names to match plays with the tracklist: lower case, and without the
    ' - Remastered 2011' / ' (feat. X)' extras that differ between versions."""
    name = (name or "").lower().strip()
    name = re.sub(r"\s+-\s+.*$", "", name)
    name = re.sub(r"\s*[\(\[](feat|with|ft)\.?\s.*?[\)\]]", "", name)
    return re.sub(r"\s+", " ", name).strip()


def sittings(tl, positions):
    """Group an album's plays into back-to-back runs (see the top of this file)."""
    runs, last = [], None
    for i in positions:
        if last is not None and i == last + 1 and tl.start(i) - tl.end[last] <= SESSION_GAP_S:
            runs[-1].append(i)
        else:
            runs.append([i])
        last = i
    return runs


@lru_cache(maxsize=4)
def _album_habit(user_id, _latest):
    """Across ALL your listening: what share of plays happen as part of an album run
    (3+ songs from one album back to back)? The baseline every album is compared with."""
    tl = timeline(user_id)
    in_runs, total, run_start = 0, 0, 0
    n = len(tl.track)
    for i in range(1, n + 1):
        if i == n or tl.album[i] != tl.album[i - 1] or tl.start(i) - tl.end[i - 1] > SESSION_GAP_S:
            run = [j for j in range(run_start, i) if tl.ms[j] >= MIN_PLAY_MS]
            total += len(run)
            if len({tl.track[j] for j in run}) >= ALBUM_RUN:
                in_runs += len(run)
            run_start = i
    return in_runs / (total or 1)


def album_habit(user_id):
    return _album_habit(user_id, latest_play_id(user_id))


def find(tl, name, artist):
    return tl.album_of.get(((name or "").lower(), artist))


def album(user_id, name, artist, saved=None, also=()):
    """Everything for the album page. 'saved' is the albums.py row (with tracks) if known.
    'also': extra song names to count as part of the album when estimating its tracklist
    (so two people compared without a real tracklist use the same estimated album)."""
    tl = timeline(user_id)
    a = find(tl, name, artist)
    if a is None or not tl.album_ms.get(a):
        return None
    positions = tl.by_album[a]
    plays = [i for i in positions if tl.listened(i)]
    album_name, album_artist = tl.album_info[a]

    # ---- The tracklist: real if looked up, otherwise the songs you've played from it.
    tracklist = (saved or {}).get("tracks") or None
    if tracklist:
        tracks = [{"n": t["n"], "disc": t.get("disc", 1), "name": t["name"], "ms": t.get("ms"), "key": clean(t["name"])}
                  for t in tracklist]
        multi_disc = len({t["disc"] for t in tracks}) > 1
        index = {}
        for pos, t in enumerate(tracks):
            index.setdefault(t["key"], pos)
    else:
        # Grouped by the cleaned-up name: the same song can be stored as "X (feat. Y)" and "X (feat Y )".
        seen, label = Counter(), {}
        for i in plays:
            name_ = tl.track_info[tl.track[i]][0]
            seen[clean(name_)] += 1
            label.setdefault(clean(name_), name_)
        for extra in also:
            seen.setdefault(clean(extra), 0)
            label.setdefault(clean(extra), extra)
        tracks = [{"n": None, "disc": 1, "name": label[k], "ms": None, "key": k} for k, _ in seen.most_common()]
        multi_disc = False
        index = {t["key"]: pos for pos, t in enumerate(tracks)}
    known_order = bool(tracklist)
    size = len(tracks)

    def slot(i):
        """Which tracklist row play i belongs to (None: a version not on this tracklist)."""
        return index.get(clean(tl.track_info[tl.track[i]][0]))

    # ---- Per song: plays, time, skips, instant skips, plays outside album runs.
    rows = [{"plays": 0, "ms": 0, "seen": 0, "skips": 0, "instant": 0, "alone": 0, "reach": 0} for _ in tracks]
    other_plays = 0
    runs = sittings(tl, positions)
    in_run = set()
    for run in runs:
        if len({tl.track[i] for i in run if tl.listened(i)}) >= ALBUM_RUN:
            in_run.update(run)
    for i in positions:
        s = slot(i)
        if s is None:
            other_plays += tl.listened(i)
            continue
        r = rows[s]
        if tl.listened(i):
            r["plays"] += 1
            r["ms"] += tl.ms[i]
            if i not in in_run:
                r["alone"] += 1
        if tl.skip[i] >= 0:
            r["seen"] += 1
            r["skips"] += tl.skip[i]
            if tl.skip[i] and tl.ms[i] < INSTANT_S * 1000:
                r["instant"] += 1

    # ---- Sittings: full listens, in order, where you drop off.
    full, in_order, longest, starts_at_one = [], 0, None, []
    needed = size if size < 4 else max(4, math.ceil(FULL_SHARE * size))
    for run in runs:
        heard = [i for i in run if tl.listened(i)]
        slots = [slot(i) for i in heard if slot(i) is not None]
        distinct = len(set(slots))
        if distinct >= needed and size >= 2:
            full.append(run)
            if known_order and len(slots) >= 2:
                steps = sum(1 for x, y in zip(slots, slots[1:]) if y == x + 1)
                shuffled = sum(1 for i in run if tl.shuffle[i] == 1)
                if steps / (len(slots) - 1) >= IN_ORDER_SHARE and shuffled <= len(run) / 2:
                    in_order += 1
        if slots and (longest is None or distinct > longest[0]):
            longest = (distinct, run)
        # Sittings that start from track 1 in order: how far do they get?
        if known_order and slots and slots[0] == 0 and len(slots) >= 2:
            reached = 0
            for x in slots:
                if x == reached + 1:
                    reached = x
            starts_at_one.append(reached)
    if starts_at_one:
        for pos in range(size):
            rows[pos]["reach"] = round(100 * sum(1 for r in starts_at_one if r >= pos) / len(starts_at_one))

    habit_here = len([i for i in plays if i in in_run]) / (len(plays) or 1)
    habit_usual = album_habit(user_id)

    # ---- Songs, for the page.
    top_plays = max((r["plays"] for r in rows), default=1) or 1
    song_rows = []
    for t, r in zip(tracks, rows):
        song_rows.append({
            "n": t["n"], "disc": t["disc"], "name": t["name"],
            "length": (f"{t['ms'] // 60000}:{t['ms'] // 1000 % 60:02d}" if t.get("ms") else None),
            "plays": r["plays"], "hours": r["ms"] / 3_600_000, "bar": round(100 * r["plays"] / top_plays),
            "skip": round(100 * r["skips"] / r["seen"]) if r["seen"] >= 5 else None, "seen": r["seen"],
            "instant": r["instant"], "alone": r["alone"], "reach": r["reach"] if starts_at_one else None,
            "track": next((tl.track_ids[tl.track[i]] for i in plays if slot(i) == len(song_rows)), None),
        })
    played = [s for s in song_rows if s["plays"]]
    favourites = sorted(played, key=lambda s: -s["hours"])[:5]
    # The one you come back for: most played on its own, outside album runs. Not the favourite
    # again (that's already its own card), so it says something new.
    come_back = max((s for s in played if not favourites or s is not favourites[0]),
                    key=lambda s: s["alone"], default=None)
    never = [s["name"] for s in song_rows if not s["plays"]] if known_order else []

    # ---- Skipping pattern.
    seen_all = sum(r["seen"] for r in rows)
    skips_all = sum(r["skips"] for r in rows)
    rate = skips_all / seen_all if seen_all >= 10 else None
    rated = [s for s in song_rows if s["skip"] is not None]
    most_skipped = max(rated, key=lambda s: s["skip"], default=None)
    never_skipped = [s["name"] for s in rated if s["skip"] == 0 and s["seen"] >= 8]

    def skip_rate(subset):
        known = [i for i in subset if tl.skip[i] >= 0]
        return round(100 * sum(tl.skip[i] for i in known) / len(known)) if len(known) >= 10 else None
    skip_in_runs = skip_rate([i for i in positions if i in in_run])
    skip_alone = skip_rate([i for i in positions if i not in in_run])

    first = plays[0]
    first_song = tl.track_info[tl.track[first]][0]
    best = tl.album_best.get(a)
    last_full = full[-1] if full else None
    return {
        "name": (saved or {}).get("name") or album_name, "artist": album_artist,
        "art_track": tl.track_ids[best] if best is not None else None,
        "release": (saved or {}).get("release_date"),
        "known_order": known_order, "size": size, "multi_disc": multi_disc,
        "rank": tl.album_rank[a], "of": len(tl.album_rank),
        "share_of_artist": round(100 * tl.album_ms[a] / (tl.artist_ms.get(album_artist) or 1)),
        "tiles": [
            ("Listening time", f"{tl.album_ms[a] / 3_600_000:,.1f} h" if tl.album_ms[a] < 36_000_000
             else f"{tl.album_ms[a] / 3_600_000:,.0f} h"),
            ("Plays", f"{len(plays):,}"),
            ("Full listens", f"{len(full):,}"),
            ("First listen", fmt_date(tl.local(first))),
        ],
        "first": {"date": fmt_date(tl.local(first)), "song": first_song, "track": tl.track_ids[tl.track[first]],
                  "kind": tl.kind[first]},
        "one_go": {
            "full": len(full), "in_order": in_order if known_order else None, "needed": needed,
            "sittings": sum(1 for r in runs if len({tl.track[i] for i in r if tl.listened(i)}) >= ALBUM_RUN),
            "here": round(100 * habit_here), "usual": round(100 * habit_usual),
            "ratio": habit_here / habit_usual if habit_usual else None,
            "last_full": fmt_date(tl.local(last_full[0])) if last_full else None,
            "longest": ({"songs": longest[0], "minutes": round((tl.end[longest[1][-1]] - tl.start(longest[1][0])) / 60),
                         "date": fmt_date(tl.local(longest[1][0]))} if longest else None),
            "starts": len(starts_at_one),
            "finish": (round(100 * sum(1 for r in starts_at_one if r >= size - 1) / len(starts_at_one))
                       if starts_at_one else None),
        },
        "songs": song_rows,
        "other_plays": other_plays,
        "favourites": favourites,
        "come_back": come_back if come_back and come_back["alone"] >= 5 else None,
        "never": never,
        "skips": {
            "rate": round(100 * rate) if rate is not None else None,
            "usual": round(100 * tl.skip_rate) if tl.skip_rate is not None else None,
            "most": most_skipped if most_skipped and most_skipped["skip"] >= 20 else None,
            "never": never_skipped[:5],
            "in_runs": skip_in_runs, "alone": skip_alone,
            "instant": sum(r["instant"] for r in rows),
        },
        "history": monthly_chart(tl, positions),
        "comeback": comeback(tl, positions),
        "when": when_you_play(tl, positions, min_plays=20),
    }
