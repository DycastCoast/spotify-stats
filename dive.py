"""
Crate Dive: the engine. Name any right answer; rarer answers score more.

The game logic is ported from Trench (the geography version): the same tiers, typo-tolerant
matching, the collision guard, relative rarity and dive picking. The answers come from data
rather than hand-written lists:

  general prompts   from the artist pool (artistmeta.py): Last.fm listeners for rarity,
                    Last.fm tags for genres, MusicBrainz for where from / when / band or solo
  song prompts      from every song anyone in the group has played (the prompt says so);
                    rarity uses the artist's Last.fm listeners, so a deep cut by a huge
                    artist still counts as common
  personal prompts  from YOUR history: rarity = how far down your own list it sits
  group prompts     you and a friend: rarity = how much you both play it

Rarity is RELATIVE: answers to one prompt are ranked against each other (most familiar 20%
= Bargain Bin ... least familiar 7% = White Label). Anything under 1,000 Last.fm listeners
still scores White Label but is never listed in the reveal (it'd be random SoundCloud uploads).

No prompt is offered with fewer than MIN_ANSWERS answers: narrow prompts turn into "guess
which one they marked as rare", which is exactly what this game isn't.

Try it on the real database:  python dive.py --preview [user_id]
"""
import hashlib
import json
import random
import re
import sys
import time
import unicodedata
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from functools import lru_cache

from db import connect
from stats import MIN_PLAY_MS

MIN_ANSWERS = 100
TIME_LIMIT = 25
FLOOR_LISTENERS = 1000                       # under this: White Label, but hidden from the reveal

TIERS = {
    "bin":   {"name": "Bargain Bin",  "pts": 10},
    "radio": {"name": "Radio Edit",   "pts": 30},
    "album": {"name": "Album Track",  "pts": 60},
    "bside": {"name": "B-Side",       "pts": 85},
    "white": {"name": "White Label",  "pts": 100},
    "boot":  {"name": "Bootleg",      "pts": 15},    # technically true, bends the rules
    "claim": {"name": "Trust Me Bro", "pts": 30},    # not in the crate, counted on your word
    "miss":  {"name": "Dead Air",     "pts": 0},
}
# Where each tier starts, as a share of a prompt's answers sorted most familiar first.
CUTS = [(0.20, "bin"), (0.45, "radio"), (0.75, "album"), (0.93, "bside"), (1.01, "white")]
ZONES = [(0, "the Shop Floor"), (20, "the Back Racks"), (100, "the Basement"), (400, "the Storage Unit"), (600, "the Vault")]


def zone(points):
    return [name for start, name in ZONES if points >= start][-1]


# ------------------------------------------------------------------ names
NUMBER_WORDS = {w: str(n) for n, w in enumerate(
    "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen "
    "sixteen seventeen eighteen nineteen twenty".split())}
STOP = {"the"}
# Song titles: drop "(Remastered 2011)", "[Live]", "(feat. X)", " - Radio Edit", " - Live at Wembley"...
TITLE_CUT = re.compile(r"\s*[\(\[][^)\]]*[\)\]]|\s+-\s+.*$|\s+(feat|ft)\.?\s.*$", re.I)


def strip_title(title):
    """'Song (Remastered 2011)' -> 'Song'; 'Song - Live at X' -> 'Song'. Never returns empty."""
    t = TITLE_CUT.sub("", title or "").strip()
    return t or (title or "").strip()


def norm(s):
    """The key two spellings must share to count as the same answer.

    The Beatles = Beatles, Guns N' Roses = Guns and Roses, P!nk = Pink, Ke$ha = Kesha,
    AC/DC = ACDC, Beyoncé = Beyonce, 5 Seconds of Summer = Five Seconds of Summer.
    """
    t = unicodedata.normalize("NFKD", str(s).lower())
    t = "".join(ch for ch in t if not unicodedata.combining(ch))
    t = re.sub(r"(?<=[a-z])!(?=[a-z])", "i", t)          # P!nk -> pink
    t = re.sub(r"\$(?=[a-z])|(?<=[a-z])\$", "s", t)      # Ke$ha, $uicideboy$, A$AP
    t = t.replace("&", " and ").replace("+", " and ")
    t = re.sub(r"[’'`ʻ]", "", t)
    t = re.sub(r"[^a-z0-9]+", " ", t)
    words = [NUMBER_WORDS.get(w, "and" if w == "n" else w) for w in t.split()]
    kept = [w for w in words if w not in STOP]
    return "".join(kept or words)


def dist(a, b, cap=3):
    """Typo distance (optimal string alignment: swaps of two letters count as one)."""
    if abs(len(a) - len(b)) >= cap:
        return cap
    prev2, prev = None, list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def allowed_typos(key):
    return 2 if len(key) >= 9 else 1 if len(key) >= 5 else 0


# ------------------------------------------------------------------ rarity
def assign_tiers(answers, floor=None):
    """answers: dicts with "weight" (bigger = more familiar). Sets "tier" and "hidden".
    Ties share their average position, so equally familiar answers get the same tier."""
    real = sorted((a for a in answers if not a.get("boot")), key=lambda a: -a["weight"])
    n = len(real)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and real[j + 1]["weight"] == real[i]["weight"]:
            j += 1
        pct = ((i + j) / 2) / (n - 1) if n > 1 else 0
        tier = next(t for cut, t in CUTS if pct < cut)
        for k in range(i, j + 1):
            real[k]["tier"], real[k]["pct"] = tier, pct
        i = j + 1
    for a in answers:
        if a.get("boot"):
            a["tier"], a["pct"] = "boot", None
        a["hidden"] = floor is not None and not a.get("boot") and (a["weight"] or 0) < floor
        if a["hidden"]:
            a["tier"] = "white"
    return answers


def make_prompt(pid, q, pack, family, answers, kind="general", note="", space="artist", floor=FLOOR_LISTENERS, min_answers=MIN_ANSWERS):
    """Merge duplicates (same key -> keep the most familiar), give tiers, add matching keys.
    Returns None when there aren't enough answers to make a good prompt."""
    merged = {}
    for a in answers:
        k = norm(a["name"])
        if not k:
            continue
        prev = merged.get(k)
        if prev is None or (a["weight"] or 0) > (prev["weight"] or 0):
            aliases = (prev["aliases"] if prev else []) + [x for x in a.get("aliases", []) if x]
            merged[k] = {**a, "aliases": aliases}
        else:
            prev["aliases"] = prev["aliases"] + [a["name"]] + [x for x in a.get("aliases", []) if x]
    out = list(merged.values())
    if sum(1 for a in out if not a.get("boot")) < min_answers:
        return None
    assign_tiers(out, floor=floor)
    for a in out:
        a["keys"] = sorted({norm(x) for x in [a["name"], *a["aliases"]] if norm(x)})
    return {"id": pid, "q": q, "pack": pack, "family": family, "kind": kind, "note": note,
            "space": space, "answers": out}


# ------------------------------------------------------------------ general prompts (the artist pool)
TAG_LABELS = {   # Last.fm tag -> how the prompt says it
    "rock": "rock", "pop": "pop", "hip-hop": "hip-hop", "rap": "rap", "indie": "indie", "electronic": "electronic",
    "metal": "metal", "punk": "punk", "jazz": "jazz", "soul": "soul", "rnb": "R&B", "folk": "folk", "country": "country",
    "k-pop": "K-pop", "house": "house", "grime": "grime", "uk hip hop": "UK hip-hop", "drum and bass": "drum and bass",
    "britpop": "Britpop", "shoegaze": "shoegaze", "emo": "emo", "reggae": "reggae", "afrobeats": "Afrobeats",
    "latin": "Latin", "techno": "techno", "trap": "trap", "singer-songwriter": "singer-songwriter",
    "alternative": "alternative", "classical": "classical", "disco": "disco", "hard rock": "hard rock",
    "post-punk": "post-punk", "dance": "dance", "dubstep": "dubstep", "garage": "garage", "blues": "blues",
    "funk": "funk", "ambient": "ambient", "hardcore": "hardcore", "psychedelic": "psychedelic", "grunge": "grunge",
    "new wave": "new wave", "synthpop": "synth-pop", "j-pop": "J-pop", "dream pop": "dream pop", "lo-fi": "lo-fi",
}
COUNTRIES = {"GB": "the UK", "US": "the US", "CA": "Canada", "AU": "Australia", "IE": "Ireland", "SE": "Sweden",
             "NO": "Norway", "DK": "Denmark", "FI": "Finland", "IS": "Iceland", "DE": "Germany", "FR": "France",
             "NL": "the Netherlands", "BE": "Belgium", "ES": "Spain", "IT": "Italy", "JP": "Japan", "KR": "South Korea",
             "BR": "Brazil", "MX": "Mexico", "NG": "Nigeria", "JM": "Jamaica", "NZ": "New Zealand", "PR": "Puerto Rico",
             "CO": "Colombia", "AR": "Argentina", "ZA": "South Africa", "RU": "Russia", "PL": "Poland"}
NUMBERISH = re.compile(r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|hundred|thousand|million)\b", re.I)


def pool_fingerprint():
    """Changes when the artist pool does (at most hourly, so prompts aren't rebuilt constantly)."""
    with connect() as conn:
        r = conn.execute("SELECT COUNT(*), SUM(lf_status = 'ok'), SUM(mb_status = 'ok') FROM artist_meta").fetchone()
    return f"{r[0]}.{(r[1] or 0) // 200}.{(r[2] or 0) // 200}"


def _pool():
    with connect() as conn:
        rows = conn.execute(
            """SELECT name, lf_name, listeners, tags, mb_type, mb_country, mb_begin_area, mb_begin_year, mb_gender
               FROM artist_meta WHERE lf_status = 'ok' AND listeners IS NOT NULL""").fetchall()
    pool = []
    for r in rows:
        tags = json.loads(r["tags"]) if r["tags"] else []
        aliases = [r["lf_name"]] if r["lf_name"] and r["lf_name"] != r["name"] else []
        pool.append({"name": r["name"], "aliases": aliases, "weight": r["listeners"], "tags": tags,
                     "type": r["mb_type"], "country": r["mb_country"], "area": r["mb_begin_area"],
                     "year": r["mb_begin_year"], "gender": r["mb_gender"]})
    return pool


def _initial(name):
    words = [w for w in re.sub(r"[^A-Za-z0-9 ]", " ", name).split() if w.lower() != "the"]
    return words[0][0].upper() if words else ""


@lru_cache(maxsize=2)
def general_prompts(fingerprint):
    """Every prompt the artist pool can support right now. fingerprint = pool_fingerprint()."""
    pool = _pool()
    prompts = []

    def add(*args, **kw):
        p = make_prompt(*args, **kw)
        if p:
            prompts.append(p)

    lf_note = "Rarity: Last.fm listeners worldwide"
    # Genres (Last.fm tags)
    for tag, label in TAG_LABELS.items():
        an = "an" if label[0].lower() in "aeiou" else "a"
        add(f"tag-{tag}", f"Name {an} {label} artist", "genres", "genre",
            [a for a in pool if tag in a["tags"]], note=lf_note)
    # Places (MusicBrainz)
    for cc, label in COUNTRIES.items():
        add(f"from-{cc}", f"Name an artist from {label}", "places", "country",
            [a for a in pool if a["country"] == cc], note=lf_note)
    by_area = defaultdict(list)
    for a in pool:
        if a["area"]:
            by_area[a["area"]].append(a)
    for area, items in by_area.items():
        if len(items) >= MIN_ANSWERS and area not in ("United States", "United Kingdom", "England"):
            add(f"city-{norm(area)}", f"Name an artist who started out in {area}", "places", "city", items, note=lf_note)
    # Eras (MusicBrainz start year)
    for dec in range(1950, 2030, 10):
        add(f"band-{dec}s", f"Name a band or group formed in the {dec}s", "eras", "band-decade",
            [a for a in pool if a["type"] == "Group" and a["year"] and dec <= a["year"] < dec + 10], note=lf_note)
        add(f"solo-{dec}s", f"Name a solo artist whose career started in the {dec}s", "eras", "solo-decade",
            [a for a in pool if a["type"] == "Person" and a["year"] and dec <= a["year"] < dec + 10], note=lf_note)
        for cc in ("GB", "US"):
            add(f"band-{cc}-{dec}s", f"Name a band from {COUNTRIES[cc]} formed in the {dec}s", "eras", "band-decade",
                [a for a in pool if a["type"] == "Group" and a["country"] == cc and a["year"] and dec <= a["year"] < dec + 10], note=lf_note)
    add("solo-female", "Name a female solo artist", "eras", "solo", [a for a in pool if a["type"] == "Person" and a["gender"] == "Female"], note=lf_note)
    add("solo-male", "Name a male solo artist", "eras", "solo", [a for a in pool if a["type"] == "Person" and a["gender"] == "Male"], note=lf_note)
    # Names
    add("one-word", "Name an artist known by one word", "names", "one-word",
        [a for a in pool if re.fullmatch(r"[^\s]+", a["name"].strip()) and not a["name"].lower().startswith("the")], note=lf_note)
    add("has-number", "Name an artist with a number in their name", "names", "number",
        [a for a in pool if NUMBERISH.search(a["name"])], note=lf_note)
    for letter in "ABCDEFGHIJKLMNOPRSTUVWY":
        add(f"letter-{letter}", f"Name an artist beginning with {letter} (ignore any \"The\")", "names", "letter",
            [a for a in pool if _initial(a["name"]) == letter], note=lf_note)
    # Charts (listener bands)
    bands = [("big", "Name an artist with over 3 million Last.fm listeners", 3_000_000, None),
             ("mid", "Name an artist with between 500,000 and 3 million Last.fm listeners", 500_000, 3_000_000),
             ("small", "Name an artist with under 100,000 Last.fm listeners", None, 100_000)]
    for key, q, lo, hi in bands:
        add(f"listeners-{key}", q, "charts", "listeners",
            [a for a in pool if (lo is None or a["weight"] >= lo) and (hi is None or a["weight"] < hi)],
            note="Rarity: fewer Last.fm listeners = rarer", floor=None if key == "small" else FLOOR_LISTENERS)
    return prompts


# ------------------------------------------------------------------ plays: songs, personal, group
def _artist_listeners():
    with connect() as conn:
        return {r["name"]: r["listeners"] for r in conn.execute(
            "SELECT name, listeners FROM artist_meta WHERE listeners IS NOT NULL")}


SONG_WORDS = {
    "colour": ("a colour", r"\b(red|blue|green|yellow|black|white|pink|purple|orange|gold|golden|grey|gray|brown|silver|violet|scarlet|crimson|indigo)\b"),
    "number": ("a number", r"\d|\b(one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|hundred|thousand|million|first|second|third)\b"),
    "love": ('"love"', r"\blove"),
    "night": ('"night" or "tonight"', r"night"),
    "girl": ('"girl", "boy", "man" or "woman"', r"\b(girl|girls|boy|boys|man|woman|women|men)\b"),
    "time": ("a day, month or time of day", r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday|january|february|march|april|may|june|july|august|september|october|november|december|morning|midnight|noon|tonight|today|tomorrow|yesterday)\b"),
    "place": ("a city or country", r"\b(london|paris|new york|tokyo|berlin|california|texas|london|england|america|africa|brooklyn|miami|chicago|vegas|hollywood|rome|ibiza|manchester|atlanta|detroit|memphis|mexico|jamaica|china|japan|spain|ireland|scotland)\b"),
}


@lru_cache(maxsize=4)
def song_prompts(group_key):
    """Prompts about song titles, from songs anyone in the group has played. group_key changes
    when anyone's plays do (it's every person's latest_play_id joined)."""
    listeners = _artist_listeners()
    with connect() as conn:
        rows = conn.execute(
            """SELECT track_name, artist_name FROM plays_best
               WHERE listened_ms >= ? AND track_name IS NOT NULL GROUP BY track_name, artist_name""",
            (MIN_PLAY_MS,)).fetchall()
    songs = [{"name": strip_title(r["track_name"]), "detail": r["artist_name"],
              "weight": listeners.get(r["artist_name"]) or 0} for r in rows]
    prompts = []
    note = "Only songs one of us has played · rarity: the artist's Last.fm listeners"
    for key, (label, pattern) in SONG_WORDS.items():
        rx = re.compile(pattern, re.I)
        p = make_prompt(f"song-{key}", f"Name a song with {label} in the title", "songs", "song-title",
                        [s for s in songs if rx.search(s["name"])], kind="group", note=note, space="song", floor=None)
        if p:
            prompts.append(p)
    return prompts


def _user_artists(user_id):
    with connect() as conn:
        return conn.execute(
            """SELECT artist_name AS name, COUNT(*) AS plays, MIN(played_at) AS first, MAX(played_at) AS last
               FROM plays_best WHERE user_id = ? AND listened_ms >= ? AND artist_name IS NOT NULL
               GROUP BY artist_name""", (user_id, MIN_PLAY_MS)).fetchall()


# Personal prompt templates: the same ids for everyone (so the daily is the same set of
# questions), but each person's answers come from their own history.
PERSONAL = [
    ("me-this-year", "Name an artist you've played this year", "you", "me-recent"),
    ("me-last-year", "Name an artist you played last year", "you", "me-recent"),
    ("me-50", "Name an artist you've played at least 50 times", "you", "me-count"),
    ("me-new", "Name an artist you discovered in the last 12 months", "you", "me-new"),
    ("me-songs-20", "Name a song you've played at least 20 times", "you", "me-songs"),
    ("me-albums", "Name an album in your top 150", "you", "me-albums"),
    ("me-forgotten", "Name an artist you loved (25+ plays) but haven't played for a year", "you", "me-old"),
] + [(f"me-first-{y}", f"Name an artist you first played in {y}", "you", "me-first") for y in range(2017, date.today().year)]
GROUP = [
    ("us-both", "Name an artist you and {friend} both play", "group", "us-both"),
    ("us-only-me", "Name an artist you play that {friend} has never played", "group", "us-only"),
]
PERSONAL_NOTE = "Your own history · rarity: how far down your list it sits"


def best_friend(user_id):
    """The person you share most artists with (for "you and X" prompts)."""
    with connect() as conn:
        rows = conn.execute(
            """SELECT b.user_id AS friend, COUNT(DISTINCT b.artist_name) AS shared
               FROM (SELECT DISTINCT artist_name FROM plays WHERE user_id = ?) a
               JOIN (SELECT DISTINCT user_id, artist_name FROM plays WHERE user_id != ?) b USING (artist_name)
               GROUP BY b.user_id ORDER BY shared DESC LIMIT 1""", (user_id, user_id)).fetchall()
        if not rows:
            return None, None
        name = conn.execute("SELECT display_name FROM users WHERE id = ?", (rows[0]["friend"],)).fetchone()
    return rows[0]["friend"], (name["display_name"] if name and name["display_name"] else rows[0]["friend"])


def personal_prompt(pid, user_id):
    """One personal / group prompt resolved for one person (None if their history can't support it).
    Personal prompts may go below MIN_ANSWERS (your history is your history) but need at least 30."""
    tmpl = next((t for t in PERSONAL + GROUP if t[0] == pid), None)
    if not tmpl:
        return None
    _, q, pack, family = tmpl
    now = datetime.now(timezone.utc)
    this_year, kw = str(now.year), dict(kind="personal", note=PERSONAL_NOTE, floor=None, min_answers=30)
    arts = _user_artists(user_id)
    if pid == "me-this-year" or pid == "me-last-year":
        y = this_year if pid == "me-this-year" else str(now.year - 1)
        with connect() as conn:
            rows = conn.execute(
                """SELECT artist_name AS name, COUNT(*) AS n FROM plays_best WHERE user_id = ? AND listened_ms >= ?
                   AND substr(played_at, 1, 4) = ? GROUP BY artist_name""", (user_id, MIN_PLAY_MS, y)).fetchall()
        return make_prompt(pid, q, pack, family, [{"name": r["name"], "weight": r["n"]} for r in rows if r["name"]], **kw)
    if pid == "me-50":
        return make_prompt(pid, q, pack, family, [{"name": a["name"], "weight": a["plays"]} for a in arts if a["plays"] >= 50], **kw)
    if pid == "me-new":
        cutoff = (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
        return make_prompt(pid, q, pack, family, [{"name": a["name"], "weight": a["plays"]} for a in arts if a["first"] >= cutoff], **kw)
    if pid == "me-forgotten":
        cutoff = (now - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%S")
        return make_prompt(pid, q, pack, family, [{"name": a["name"], "weight": a["plays"]} for a in arts if a["plays"] >= 25 and a["last"] < cutoff], **kw)
    if pid.startswith("me-first-"):
        y = pid.rsplit("-", 1)[1]
        return make_prompt(pid, q, pack, family, [{"name": a["name"], "weight": a["plays"]} for a in arts if a["first"][:4] == y], **kw)
    if pid == "me-songs-20":
        with connect() as conn:
            rows = conn.execute(
                """SELECT track_name, artist_name, COUNT(*) AS n FROM plays_best WHERE user_id = ? AND listened_ms >= ?
                   AND track_name IS NOT NULL GROUP BY track_name, artist_name HAVING n >= 20""", (user_id, MIN_PLAY_MS)).fetchall()
        return make_prompt(pid, q, pack, family, [{"name": strip_title(r["track_name"]), "detail": r["artist_name"], "weight": r["n"]} for r in rows],
                           space="song", **kw)
    if pid == "me-albums":
        with connect() as conn:
            rows = conn.execute(
                """SELECT album_name, artist_name, COUNT(*) AS n FROM plays_best WHERE user_id = ? AND listened_ms >= ?
                   AND album_name IS NOT NULL AND album_name != '' GROUP BY album_name, artist_name ORDER BY n DESC LIMIT 150""",
                (user_id, MIN_PLAY_MS)).fetchall()
        return make_prompt(pid, q, pack, family, [{"name": strip_title(r["album_name"]), "detail": r["artist_name"], "weight": r["n"]} for r in rows],
                           space="album", **kw)
    if pid in ("us-both", "us-only-me"):
        friend, friend_name = best_friend(user_id)
        if not friend:
            return None
        theirs = {a["name"]: a["plays"] for a in _user_artists(friend)}
        if pid == "us-both":
            items = [{"name": a["name"], "weight": min(a["plays"], theirs[a["name"]])} for a in arts if a["name"] in theirs]
        else:
            items = [{"name": a["name"], "weight": a["plays"]} for a in arts if a["name"] not in theirs and a["plays"] >= 3]
        p = make_prompt(pid, q.format(friend=friend_name), pack, family, items,
                        **{**kw, "kind": "group", "note": f"You and {friend_name} · rarity: how much you both play it"})
        if p:
            p["friend"] = friend
        return p
    return None


# ------------------------------------------------------------------ looking prompts up
def group_key():
    with connect() as conn:
        r = conn.execute("SELECT MAX(id), (SELECT SUM(data_version) FROM users) FROM plays").fetchone()
    return f"{r[0]}.{r[1]}"


def templates():
    """Every prompt id that could be asked today, with pack/family, for picking dives."""
    out = [{"id": p["id"], "pack": p["pack"], "family": p["family"]} for p in general_prompts(pool_fingerprint())]
    out += [{"id": p["id"], "pack": p["pack"], "family": p["family"]} for p in song_prompts(group_key())]
    out += [{"id": t[0], "pack": t[2], "family": t[3]} for t in PERSONAL + GROUP]
    return out


@lru_cache(maxsize=64)
def _resolved(pid, user_id, pool_fp, gkey):
    for p in general_prompts(pool_fp):
        if p["id"] == pid:
            return p
    for p in song_prompts(gkey):
        if p["id"] == pid:
            return p
    return personal_prompt(pid, user_id)


def prompt(pid, user_id):
    """A prompt with its answers, for this person (personal prompts differ per person)."""
    return _resolved(pid, user_id, pool_fingerprint(), group_key())


@lru_cache(maxsize=4)
def known_names(pool_fp, gkey):
    """Every real name the game knows, per kind of answer, so a real-but-wrong answer is never
    fuzzy-matched to a lookalike that IS on the list ("Slovenia" never becomes "Slovakia")."""
    with connect() as conn:
        artists = {norm(r["name"]) for r in conn.execute("SELECT name FROM artist_meta")}
        artists |= {norm(r["lf_name"]) for r in conn.execute("SELECT lf_name FROM artist_meta WHERE lf_name IS NOT NULL")}
        songs = {norm(strip_title(r[0])) for r in conn.execute("SELECT DISTINCT track_name FROM plays WHERE track_name IS NOT NULL")}
        albums = {norm(strip_title(r[0])) for r in conn.execute("SELECT DISTINCT album_name FROM plays WHERE album_name IS NOT NULL")}
    by_len = {}
    for space, names in (("artist", artists), ("song", songs), ("album", albums)):
        idx = defaultdict(set)
        for k in names:
            if k:
                idx[len(k)].add(k)
        by_len[space] = (names, idx)
    return by_len


def match(p, raw, known=None):
    """Is `raw` a right answer to prompt p?
    -> {"ok": True, "answer": a, "exact": bool} or {"ok": False, "reason": "empty"|"known"|"unknown"}"""
    q = norm(raw or "")
    if not q:
        return {"ok": False, "reason": "empty"}
    for a in p["answers"]:
        if q in a["keys"]:
            return {"ok": True, "answer": a, "exact": True}
    names, by_len = known.get(p["space"], (set(), {})) if known else (set(), {})
    if q in names:
        return {"ok": False, "reason": "known"}          # real, just not right for this prompt
    allowed = allowed_typos(q)
    if not allowed:
        return {"ok": False, "reason": "unknown"}
    best, best_d, second = None, 99, 99
    for a in p["answers"]:
        d = min(dist(q, k) for k in a["keys"])
        if d < best_d:
            best, best_d, second = a, d, best_d
        elif d < second:
            second = d
    if best is None or best_d > allowed or second <= best_d:
        return {"ok": False, "reason": "unknown"}
    mine = {k for a in p["answers"] for k in a["keys"]}
    for length in range(len(q) - best_d, len(q) + best_d + 1):
        for k in by_len.get(length, ()):
            if k not in mine and dist(q, k) <= best_d:
                return {"ok": False, "reason": "known"}  # just as close to a real name that isn't valid
    return {"ok": True, "answer": best, "exact": False}


def search(p, raw, limit=8):
    """'Would it have counted?': answers whose names contain what you typed (or match it)."""
    q = norm(raw or "")
    if len(q) < 2:
        return []
    hits = [a for a in p["answers"] if any(q in k for k in a["keys"])]
    hits.sort(key=lambda a: (not any(k.startswith(q) for k in a["keys"]), -a["weight"]))
    if not hits:
        m = match(p, raw)
        hits = [m["answer"]] if m["ok"] else []
    return hits[:limit]


def reveal(p, answer=None):
    """What the reveal shows: the commonest answers, the rarest ones (never hidden ones),
    and how rare yours was."""
    shown = [a for a in p["answers"] if not a["hidden"] and not a.get("boot")]
    by_weight = sorted(shown, key=lambda a: -a["weight"])
    out = {"bargain": [a["name"] for a in by_weight[:4]],
           # the deepest answers you could have given: the rarest ones that aren't hidden
           "deepest": [a["name"] for a in by_weight if a["tier"] in ("white", "bside")][-6:][::-1],
           "count": len([a for a in p["answers"] if not a.get("boot")])}
    if answer and answer.get("pct") is not None:
        out["rarer_than"] = round(answer["pct"] * 100)
    return out


# ------------------------------------------------------------------ picking dives
def _rng(seed_text):
    return random.Random(int(hashlib.sha256(seed_text.encode()).hexdigest()[:16], 16))


def pick(pool, seed_text, avoid=(), size=7):
    """7 prompts: at most 2 per pack, never two from the same family, at least one personal.
    Relaxes the rules on a second pass if it can't fill 7."""
    rng = _rng(seed_text)
    pool = [t for t in pool if t["id"] not in avoid] if len([t for t in pool if t["id"] not in avoid]) >= size else list(pool)
    order = pool[:]
    rng.shuffle(order)
    order.sort(key=lambda t: t["pack"] != "you")          # a personal one first (stable sort keeps the shuffle)
    out, per_pack, families = [], defaultdict(int), set()
    for strict in (True, False):
        for t in order:
            if len(out) == size:
                break
            if t in out or (strict and (per_pack[t["pack"]] >= 2 or t["family"] in families)):
                continue
            out.append(t)
            per_pack[t["pack"]] += 1
            families.add(t["family"])
    rng.shuffle(out)
    return [t["id"] for t in out]


DIVE_SQL = """
CREATE TABLE IF NOT EXISTS dive_daily (day TEXT PRIMARY KEY, ids TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dive_results (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL, day TEXT,
    ids TEXT NOT NULL, tiers TEXT NOT NULL, answers TEXT NOT NULL, points INTEGER NOT NULL,
    timer INTEGER NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_dive_results_user ON dive_results (user_id, mode, day);
CREATE TABLE IF NOT EXISTS dive_claims (id INTEGER PRIMARY KEY, user_id TEXT, prompt_id TEXT, q TEXT, answer TEXT, at REAL);
"""


def setup():
    with connect() as conn:
        conn.executescript(DIVE_SQL)


def daily_ids(day):
    """The day's 7 prompt ids: picked once, the first time anyone asks, then frozen, so the
    pool growing during the day doesn't change the daily under people's feet."""
    with connect() as conn:
        row = conn.execute("SELECT ids FROM dive_daily WHERE day = ?", (day,)).fetchone()
        if row:
            return json.loads(row["ids"])
        recent = [i for r in conn.execute("SELECT ids FROM dive_daily ORDER BY day DESC LIMIT 6") for i in json.loads(r["ids"])]
        ids = pick(templates(), "crate-dive|" + day, avoid=recent)
        conn.execute("INSERT OR IGNORE INTO dive_daily (day, ids) VALUES (?, ?)", (day, json.dumps(ids)))
        row = conn.execute("SELECT ids FROM dive_daily WHERE day = ?", (day,)).fetchone()
    return json.loads(row["ids"])


def resolve(ids, user_id, seed_text):
    """The prompts for a dive, for this person. A personal prompt their history can't support
    (e.g. "first played in 2017" for someone who joined Spotify in 2020) is swapped for another
    one, picked the same way every time for the same person and day."""
    out, used = [], set(ids)
    spare = None
    for pid in ids:
        p = prompt(pid, user_id)
        if p is None:
            if spare is None:
                spare = [t for t in templates() if t["id"] not in used]
                _rng(seed_text + "|" + user_id).shuffle(spare)
                spare.sort(key=lambda t: t["pack"] != "you")
            while spare and p is None:
                t = spare.pop(0)
                used.add(t["id"])
                p = prompt(t["id"], user_id)
        if p is not None:
            out.append(p)
    return out


def unlimited_ids(user_id, pack="all"):
    with connect() as conn:
        recent = [i for r in conn.execute("SELECT ids FROM dive_results WHERE user_id = ? ORDER BY at DESC LIMIT 12", (user_id,))
                  for i in json.loads(r["ids"])][:80]
    pool = [t for t in templates() if pack == "all" or t["pack"] == pack]
    return pick(pool, f"unlimited|{user_id}|{time.time()}", avoid=recent)


# ------------------------------------------------------------------ preview on the real database
def preview(user_id=None):
    setup()
    gp = general_prompts(pool_fingerprint())
    sp = song_prompts(group_key())
    print(f"{len(gp)} general prompts, {len(sp)} song prompts")
    for p in sorted(gp + sp, key=lambda p: -len(p["answers"])):
        tiers = defaultdict(list)
        for a in sorted(p["answers"], key=lambda a: -a["weight"]):
            tiers[a["tier"]].append(a["name"])
        print(f"\n{p['q']}  [{p['pack']}, {len(p['answers'])} answers]")
        for t in ("bin", "radio", "album", "bside", "white"):
            print(f"  {TIERS[t]['name']:<12} {', '.join(tiers[t][:5])}")
    if user_id:
        for t in PERSONAL + GROUP:
            p = personal_prompt(t[0], user_id)
            print(f"\n{t[1]}: {'not enough answers' if not p else str(len(p['answers'])) + ' answers'}")
    print("\nToday's daily:", daily_ids(date.today().isoformat()))


if __name__ == "__main__":
    preview(sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--preview" else None)
