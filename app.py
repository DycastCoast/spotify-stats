"""
Spotify Stats — milestone 1
Log in with Spotify, then see your top artists, top tracks and recently played.

Run with:  python app.py   then open http://127.0.0.1:8000
"""
import os
import re
import hmac
import secrets
import time
from datetime import datetime, timedelta
from functools import wraps

import spotipy
from flask import Flask, abort, jsonify, redirect, render_template, request, session, url_for
from spotipy.cache_handler import MemoryCacheHandler

import albumpage
import albums
import artwork
import compare
import entity
import journey
import now
import playlists
import nowplaying
import poller
import scheduler
import skips
import stats
import trips
import uploads
import warm
from db import (DatabaseCacheHandler, init_db, save_user, set_share_playing, set_share_trips, shares_playing,
                shares_trips)
from spotify_client import make_client, make_oauth, pause_after, paused as spotify_paused  # also loads .env

TIME_RANGES = {
    "short_term": "Last 4 weeks",
    "medium_term": "Last 6 months",
    "long_term": "All time",
}

app = Flask(__name__)
# Used to sign the session cookie so users can't tamper with it.
app.secret_key = os.environ["FLASK_SECRET_KEY"]
# Uploads (the Import page) over this size are refused by Flask before they're even saved.
app.config["MAX_CONTENT_LENGTH"] = uploads.MAX_UPLOAD_MB * 1_000_000
# The login cookie: never readable by JavaScript, and not sent along when another site links here.
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
# On the live server (PRODUCTION=1 in its .env) the cookie also only ever travels over HTTPS,
# so nobody on the same Wi-Fi can read it. (Not on your PC: http://127.0.0.1 isn't HTTPS.)
if os.environ.get("PRODUCTION") == "1":
    app.config.update(SESSION_COOKIE_SECURE=True)

init_db()  # creates stats.db and its tables on first run

# Behind a reverse proxy (Traefik in the cluster), the browser talks HTTPS to the proxy and the
# proxy talks plain HTTP to us. These headers say what the browser really used, so Flask builds
# https:// links. Only trusted when TRUST_PROXY=1, since anyone can send these headers otherwise.
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)


# ---------------------------------------------------------------- helpers

def get_spotify():
    """Return a logged-in Spotify client, or None if the user isn't logged in.

    The browser's session cookie only holds the user's Spotify ID. Their actual
    token lives in our database, where the poller can also use it. Spotipy
    refreshes expired tokens by itself (they last an hour) and saves the new one.
    """
    user_id = session.get("user_id")
    if not user_id:
        return None
    cache = DatabaseCacheHandler(user_id)
    if cache.get_cached_token() is None:  # e.g. the database was deleted
        session.clear()
        return None
    return make_client(cache)


def login_required(view):
    """Decorator: send logged-out visitors to the home page instead of crashing."""

    @wraps(view)
    def wrapper(*args, **kwargs):
        sp = get_spotify()
        if sp is None:
            return redirect(url_for("index"))
        return view(sp, *args, **kwargs)

    return wrapper


# ---------------------------------------------------------- template filters

import charts
import geo
app.add_template_filter(charts.smooth_bars, "smooth_bars")
app.add_template_filter(lambda songs: playlists.covers_of(songs), "covers")
app.jinja_env.globals.update(geo_outline=geo.outline, geo_size=(geo.WIDTH, geo.HEIGHT))
app.add_template_filter(charts.hour_lines, "hour_lines")
app.add_template_filter(charts.trend_line, "trend_line")


@app.template_filter("map_pins")
def map_pins(trip_list):
    """Just what the map's pins and cards need from each trip (the page's list has the rest)."""
    out = []
    for n, x in enumerate(trip_list, 1):
        if not x.get("pin"):
            continue
        out.append({"n": n, "heading": x["heading"], "flags": "".join(flag(c) for c in x["stops"]),
                    "dates": x["dates"], "days": x["days"], "year": x["year"], "hours": hours(x["hours"]),
                    "artist": x["top_artist"], "song": x["trip_song"]["name"] if x.get("trip_song") else None,
                    "song_artist": x["trip_song"]["artist"] if x.get("trip_song") else None,
                    "covers": x.get("covers") or [], "pin": x["pin"], "route": x.get("route") or []})
    return out


@app.template_filter("duration")
def duration(ms):
    """215000 -> '3:35'"""
    minutes, seconds = divmod(int(ms) // 1000, 60)
    return f"{minutes}:{seconds:02d}"


@app.template_filter("played_time")
def played_time(iso_string):
    """'2026-11-13T21:40:12.345Z' -> '13 Nov, 21:40' (UTC)"""
    dt = datetime.fromisoformat(iso_string.replace("Z", "+00:00"))
    return dt.strftime("%d %b, %H:%M")


@app.context_processor
def logged_in_user():
    """Pages that don't pass `user` themselves (error pages, mostly) still get the menu.
    A page's own `user=` wins over this. No Spotify request: just the saved name."""
    me = session.get("user_id")
    if not me:
        return {}
    from db import connect
    with connect() as conn:
        row = conn.execute("SELECT display_name FROM users WHERE id = ?", (me,)).fetchone()
    if not row:
        return {}
    return {"user": {"id": me, "display_name": row["display_name"] or me, "images": []},
            "has_history": stats.latest_play_id(me) is not None}


@app.context_processor
def asset_version():
    """The stylesheet's address carries a number that changes whenever the file does
    (style.css?v=1731782400), so browsers fetch the new one instead of a stale saved copy."""
    try:
        return {"style_version": int(os.path.getmtime(os.path.join(app.static_folder, "style.css")))}
    except OSError:
        return {"style_version": 0}


@app.context_processor
def csrf_token():
    """A random token per login, put in every form that changes something. A form posted from
    another website can't know it, so it gets refused (protection against "CSRF")."""
    if "user_id" in session and "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(16)
    return {"csrf": session.get("csrf")}


@app.template_filter("flag")
def flag(code):
    """'ES' -> the Spanish flag emoji. A flag emoji is just the country's two letters written with
    special 'regional indicator' characters (A = U+1F1E6, B = U+1F1E7, ...)."""
    if not code or len(code) != 2 or not code.isalpha():
        return ""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in code.upper())


@app.template_filter("pct")
def pct(value):
    """A percentage with enough digits to not read as zero: 5.47 -> '5.5%', 0.044 -> '0.04%'."""
    if value >= 0.1 or value == 0:
        return f"{value:.1f}%"
    return f"{value:.2f}%" if value >= 0.01 else "<0.01%"


@app.template_filter("hours")
def hours(value):
    """Hours, short: 16.3 -> '16 h', 2.44 -> '2.4 h', 0.5 -> '30 min'."""
    if value >= 10:
        return f"{value:,.0f} h"
    return f"{value:.1f} h" if value >= 1 else f"{value * 60:.0f} min"


@app.template_filter("image")
def image(images, fallback=""):
    """Pick the middle-sized image from Spotify's list (they come largest first)."""
    if not images:
        return fallback
    return images[len(images) // 2]["url"]


# ------------------------------------------------------------------- routes

@app.route("/")
def index():
    if get_spotify():
        return redirect(url_for("now_page"))   # home = what you're listening to right now
    return render_template("index.html")


@app.route("/login")
def login():
    # A random "state" value protects against CSRF: we check that the login
    # response coming back is answering a request *we* started.
    state = secrets.token_urlsafe(16)
    session["oauth_state"] = state
    return redirect(make_oauth(MemoryCacheHandler(), state=state).get_authorize_url())


@app.route("/callback")
def callback():
    error = request.args.get("error")
    if error:
        # Spotify sends the reason back in the address. "access_denied" usually means Cancel was
        # pressed (or the account isn't in the app's User Management); anything else is Spotify's side.
        if error == "access_denied":
            message = ("Spotify said no: either Cancel was pressed, or this Spotify account hasn't been "
                       "added to the app's User Management yet.")
        else:
            message = (f"Spotify couldn't log you in right now (it said: {error[:60]}). "
                       "If the app hit Spotify's request limit recently, try again later.")
        return render_template("error.html", message=message), 400
    if request.args.get("state") != session.pop("oauth_state", None):
        abort(400)  # state mismatch: not a login we started

    # Swap the one-time code for tokens. We don't know who the user is yet,
    # so hold the token in memory, ask Spotify "who is this?", then save it.
    oauth = make_oauth(MemoryCacheHandler())
    oauth.get_access_token(request.args["code"], as_dict=False)
    token_info = oauth.cache_handler.get_cached_token()
    me = spotipy.Spotify(auth=token_info["access_token"]).current_user()

    save_user(me["id"], me.get("display_name"), token_info)
    session.clear()             # fresh session on login: a standard safety habit
    session["user_id"] = me["id"]

    # Start collecting history straight away rather than waiting for the timer.
    try:
        poller.poll_user(me["id"])
    except spotipy.SpotifyException:
        pass  # not fatal: the scheduled poller will try again

    return redirect(url_for("now_page"))


@app.route("/top")
@login_required
def top(sp):
    """Spotify's own ranking of your top artists and songs (live, not from the database)."""
    time_range = request.args.get("range", "short_term")
    if time_range not in TIME_RANGES:  # never pass unchecked user input to an API
        time_range = "short_term"
    artists = sp.current_user_top_artists(limit=20, time_range=time_range)["items"]
    tracks = sp.current_user_top_tracks(limit=20, time_range=time_range)["items"]

    # Link to a song or artist page only if it's in your history (else the page would be empty).
    who = session.get("user_id")
    known_tracks, known_artists = set(), set()
    if who in [p["id"] for p in stats.people()]:
        tl = entity.timeline(who)
        known_tracks, known_artists = set(tl.index_of), set(tl.artist_ms)
    page_theme = saved_art("track", tracks[0]["id"])[1] if tracks else None

    return render_template(
        "top.html",
        user=sp.current_user(),
        artists=artists,
        tracks=tracks,
        time_range=time_range,
        time_ranges=TIME_RANGES,
        known_tracks=known_tracks,
        known_artists=known_artists,
        page_theme=page_theme,
        who=who,
    )


@app.route("/recent")
@login_required
def recent(sp):
    """Your last 50 plays, live from Spotify, grouped by day in UK time."""
    plays = sp.current_user_recently_played(limit=50)["items"]
    today = datetime.now(stats.LOCAL_TZ).date()
    days = []
    for play in plays:
        when = datetime.fromisoformat(play["played_at"].replace("Z", "+00:00")).astimezone(stats.LOCAL_TZ)
        d = when.date()
        label = ("Today" if d == today else "Yesterday" if d == today - timedelta(days=1)
                 else f"{d:%A} {d.day} {d:%B}")
        if not days or days[-1]["label"] != label:
            days.append({"label": label, "plays": []})
        days[-1]["plays"].append({"track": play["track"], "time": when.strftime("%H:%M")})

    who = session.get("user_id")
    known_tracks = set()
    if who in [p["id"] for p in stats.people()]:
        known_tracks = set(entity.timeline(who).index_of)
    return render_template("recent.html", user=sp.current_user(), days=days,
                           known_tracks=known_tracks, who=who)


@app.route("/overview")
@login_required
def overview(sp):
    everyone = stats.people()
    ids = [p["id"] for p in everyone]

    # Every filter is checked against the allowed values; anything else falls back
    # to a safe default. Never trust what arrives in the URL.
    who = request.args.get("who") or session.get("user_id")
    if who not in ids:
        who = session.get("user_id") if session.get("user_id") in ids else (ids[0] if ids else None)

    available_years = stats.years(who) if who else []
    year = request.args.get("year")
    if year not in available_years:   # "all", missing, or anything unexpected -> all time
        year = None

    available_devices = stats.devices(who) if who else []
    device = request.args.get("device")
    if device not in available_devices:
        device = None

    tab = request.args.get("tab")
    if tab not in ("when", "what", "how"):
        tab = "when"

    data = stats.overview(who, year, device) if who else None

    # Built separately rather than added to `data`: data is cached and shared
    # between requests, so changing it in place would add a copy on every visit.
    found_card = None
    hero = None
    page_theme = None
    if data and data.get("has_plays"):
        hero = build_hero(who, year, device, data, available_years, everyone)

        # The top song's cover goes in the hero, and its colour themes the page.
        # If it's already saved, use it straight away; if not, the page shows
        # the default colour and fetches it in the background (see /art).
        top_songs = data["top"]["songs"]["time"]
        if top_songs:
            song = top_songs[0]
            hero["cover"] = {"track": song["art"]["track"], "name": song["name"], "artist": song["artist"]}
            saved = artwork.get("track", song["art"]["track"])
            if saved:
                hero["cover"]["url"] = saved["image_url"]
                if saved["colour_hex"]:
                    page_theme = {"hex": saved["colour_hex"], "rgb": saved["colour_rgb"], "name": saved["colour_name"]}
        found = stats.discovery(who, year, stats.latest_play_id(who))
        if found:
            if year:
                text = (f"you got into {found['total']} new artists in {year} (10+ plays); "
                        f"{found['found_you']} of them first came on by themselves")
            else:
                text = (f"{found['found_you']} of the {found['total']} artists you've played 10+ times "
                        f"first came on by themselves, rather than you putting them on")
            found_card = {"label": "Artists that found you", "value": f"{found['pct']}%", "detail": text, "count": True}

    # "On this day" teaser: the most recent earlier year with listening on today's date.
    memory = None
    if data and data.get("has_plays"):
        today = datetime.now(stats.LOCAL_TZ).date()
        past = entity.on_this_day(who, today.month, today.day, today)
        if past:
            p = past[0]
            top = p["songs"][0]
            memory = {"label": f"On this day, {p['ago']} year{'s' if p['ago'] != 1 else ''} ago",
                      "value": top["name"], "link": {"type": "song", "track": top["track"]},
                      "art": {"kind": "track", "track": top["track"]},
                      "detail": f"played {top['fig'].rstrip('×')} times on {p['date']}"}

    return render_template(
        "overview.html",
        user=sp.current_user(),
        people=everyone,
        who=who,
        year=year,
        years=available_years,
        device=device,
        devices=available_devices,
        tab=tab,
        data=data,
        hero=hero,
        page_theme=page_theme,
        found_card=found_card,
        memory=memory,
    )


def build_hero(who, year, device, data, available_years, everyone):
    """The big headline at the top of the overview: one number, one comparison."""
    name = next((p["display_name"] for p in everyone if p["id"] == who), who)
    whose = "Your" if who == session.get("user_id") else f"{name}'s"
    hours = data["total_hours"]

    if year:
        kicker = f"{whose} {year}"
        # Only compare two complete years: a person's first and latest years are
        # usually partial (export starts mid-year, or the year isn't over), and a
        # partial year makes the change meaningless ("up 4,000%"). The device
        # filter has no yearly totals, so no comparison then either.
        prev = str(int(year) - 1)
        full = sorted(available_years)[1:-1]          # drop the first and latest year
        if not device and year in full and prev in full:
            before = stats.year_total_hours(who, prev)
            change = (hours - before) / before * 100 if before else 0
            arrow = "↑" if change >= 0 else "↓"
            compare = f"{arrow} {abs(change):.0f}% on {prev}"
        else:
            first, last = data["first_day"], data["last_day"]
            compare = f"{first.day} {first:%b} to {last.day} {last:%b %Y}"   # "1 Jan", not "01 Jan"
    else:
        kicker = f"{whose} listening, all time"
        compare = f"since {data['first_day'].strftime('%B %Y')}"
    unit = "hours of music" + (f" on {'your ' if whose == 'Your' else ''}{device.lower()}" if device else "")
    colour_prefix = f"{whose} {year} was" if year else f"{whose} all-time colour is"
    return {"kicker": kicker, "number": f"{hours:,.0f}", "unit": unit, "compare": compare,
            "colour_prefix": colour_prefix}


SPOTIFY_ID = re.compile(r"[0-9A-Za-z]{22}")   # what every Spotify track ID looks like
SONG_ID = re.compile(r"[0-9A-Za-z]{22}|am-[0-9a-f]{20}")   # ...or a made-up one for Apple Music plays (import_apple.py)


def chosen_person():
    """Whose stats a page shows: ?who= if it's someone in the database, else you."""
    ids = [p["id"] for p in stats.people()]
    who = request.args.get("who") or session.get("user_id")
    if who not in ids:
        who = session.get("user_id") if session.get("user_id") in ids else (ids[0] if ids else None)
    return who


def saved_art(kind, track_id):
    """(picture URL, page colour) if this item's artwork has been fetched before, else (None, None)."""
    saved = artwork.get(kind, track_id)
    if not saved:
        return None, None
    colour = ({"hex": saved["colour_hex"], "rgb": saved["colour_rgb"], "name": saved["colour_name"]}
              if saved["colour_hex"] else None)
    return saved["image_url"], colour


@app.route("/search")
@login_required
def search(sp):
    who = chosen_person()
    query = request.args.get("q", "")[:100]
    results = entity.search(who, query) if who else {"songs": [], "artists": [], "albums": []}
    if request.args.get("format") == "json":           # the search box asks for this as you type
        return jsonify(results)
    return render_template("search.html", user=sp.current_user(), who=who, query=query,
                           results=entity.search(who, query, songs=30, artists=15, albums=15) if who else results)


@app.route("/song/<track_id>")
@login_required
def song(sp, track_id):
    if not SONG_ID.fullmatch(track_id):
        abort(404)
    who = chosen_person()
    data = entity.song(who, track_id) if who else None
    if not data:
        abort(404)
    cover_url, colour = saved_art("track", data["track"])
    return render_template("song.html", user=sp.current_user(), who=who, s=data,
                           cover_url=cover_url, page_theme=colour, friends=friends_of(who), whose=possessive(who))


@app.route("/artist")
@login_required
def artist_page(sp):
    who = chosen_person()
    name = request.args.get("name", "")[:200]
    data = entity.artist(who, name) if who and name else None
    if not data:
        abort(404)
    photo_url, colour = saved_art("artist", data["art_track"])
    return render_template("artist.html", user=sp.current_user(), who=who, a=data,
                           photo_url=photo_url, page_theme=colour, friends=friends_of(who), whose=possessive(who))


last_import = {}          # person imported into -> when (at most one upload every 2 minutes)

# The site owner(s) (SITE_OWNERS in the deployment, comma-separated Spotify IDs) can also import
# an export into SOMEONE ELSE's history: e.g. a friend's zip they sent you, so the friend only
# has to log in. Everyone else can only ever import into their own history.
SITE_OWNERS = {u.strip() for u in os.environ.get("SITE_OWNERS", "").split(",") if u.strip()}
USER_ID = re.compile(r"[A-Za-z0-9._-]{1,64}")      # what a Spotify user ID can look like


def everyone_known():
    """Everyone with a row in users (including people imported for who haven't logged in yet)."""
    from db import connect
    with connect() as conn:
        return {r["id"]: r["display_name"] or r["id"] for r in conn.execute("SELECT id, display_name FROM users")}


@app.route("/import", methods=["GET", "POST"])
@login_required
def import_page(sp):
    """Upload a Spotify export. Into your OWN history, or (site owners only) a chosen person's."""
    me = session["user_id"]
    owner = me in SITE_OWNERS
    known = everyone_known()
    result, error, need_confirm, target, target_name = None, None, None, me, "you"
    if request.method == "POST":
        if request.form.get("csrf") != session.get("csrf"):
            abort(400)
        if owner and request.form.get("for") not in (None, "", me):
            if request.form.get("for") == "new":
                target = request.form.get("new_id", "").strip()
                target_name = request.form.get("new_name", "").strip()[:60] or target
            else:
                target = request.form.get("for")
                target_name = known.get(target, target)
            if not USER_ID.fullmatch(target or "") or (request.form.get("for") != "new" and target not in known):
                error = "That isn't a valid Spotify user ID (it's the last part of their profile link)."
        if error:
            pass
        elif time.time() - last_import.get(target, 0) < 120:
            error = "That history was just imported: give it a couple of minutes."
        elif "export" not in request.files or not request.files["export"].filename:
            error = "Choose the my_spotify_data.zip first."
        else:
            last_import[target] = time.time()
            people = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
            if target != me:
                from db import add_user_if_missing
                add_user_if_missing(target, target_name)     # their plays need a users row; filled in when they log in
            try:
                result = uploads.handle_upload(request.files["export"], target, people,
                                               confirmed=request.form.get("mine") == "1")
                if result.get("need_confirm"):
                    need_confirm, result = result, None
                    last_import.pop(target, None)      # let them re-upload straight away with the box ticked
            except uploads.Refused as err:
                error = str(err)
    return render_template("import.html", user=sp.current_user(), who=me, result=result, error=error,
                           need_confirm=need_confirm, mine=uploads.my_history(me), max_mb=uploads.MAX_UPLOAD_MB,
                           owner=owner, known={k: v for k, v in known.items() if k != me},
                           target=target, target_name=target_name)


@app.route("/import/delete", methods=["POST"])
@login_required
def import_delete(sp):
    """Remove everything you imported from exports. Needs the confirm box ticked."""
    if request.form.get("csrf") != session.get("csrf") or request.form.get("sure") != "1":
        abort(400)
    removed = uploads.delete_export(session["user_id"])
    return render_template("import.html", user=sp.current_user(), who=session["user_id"], removed=removed,
                           mine=uploads.my_history(session["user_id"]), max_mb=uploads.MAX_UPLOAD_MB,
                           owner=session["user_id"] in SITE_OWNERS, known={}, target=session["user_id"], target_name="you")


@app.errorhandler(413)
def too_big(_err):
    return render_template("error.html", message=f"That file is too big (the limit is {uploads.MAX_UPLOAD_MB} MB). "
                           "A Spotify export zip is normally 10–30 MB: make sure it's the zip, not something else."), 413


@app.route("/now")
@login_required
def now_page(sp):
    """Your listening right now vs your usual. ?who=."""
    me = session["user_id"]
    if not request.args.get("who") and stats.latest_play_id(me) is None:
        # Brand new here: nothing to show yet, so say hello and point at the Import page.
        friends = [p for p in stats.people() if p["id"] != me]
        return render_template("welcome.html", user=sp.current_user(), friends=friends)
    who = chosen_person()
    data = now.right_now(who) if who else None
    if not data:
        return render_template("error.html", message="Not enough listening yet to say what's going on right now."), 404
    page_theme = saved_art("track", data["hero"]["track"])[1] if data["hero"] else None
    stale = (time.time() - data["as_of_ts"]) > 3 * 86400
    return render_template("now.html", user=sp.current_user(), who=who, n=data, people=stats.people(),
                           whose=possessive(who), page_theme=page_theme, stale=stale,
                           sharing=shares_playing(session["user_id"]), can_see=can_see_playing(who))


@app.route("/skips")
@login_required
def skips_page(sp):
    """Skipping in depth: habits, instant vs late, burnout, streaks. ?who=."""
    who = chosen_person()
    data = skips.skips(who) if who else None
    if not data:
        return render_template("error.html", message="Not enough skip data (skips come from the full history export)."), 404
    # One number per friend for the headline comparison.
    others = []
    for p in friends_of(who):
        theirs = skips.skips(p["id"])
        if theirs:
            others.append({"name": "you" if p["id"] == session.get("user_id") else p["display_name"],
                           "rate": theirs["overall"]})
    return render_template("skips.html", user=sp.current_user(), who=who, k=data, people=stats.people(),
                           others=others, whose=possessive(who))


@app.route("/album")
@login_required
def album_page(sp):
    """One album. ?name=...&artist=... (as your plays name them), plus ?who=."""
    who = chosen_person()
    name, artist = request.args.get("name", "")[:300], request.args.get("artist", "")[:200]
    tl = entity.timeline(who) if who else None
    if not tl or albumpage.find(tl, name, artist) is None:
        abort(404)       # only albums someone has actually played: random names never reach Spotify
    # The real tracklist: asked from Spotify the first time anyone opens this album, then saved.
    saved = albums.lookup(sp, name, artist)
    data = albumpage.album(who, name, artist, saved)
    if not data:
        abort(404)
    cover_url, colour = saved_art("track", data["art_track"]) if data["art_track"] else (None, None)
    return render_template("album.html", user=sp.current_user(), who=who, al=data, cover_url=cover_url,
                           page_theme=colour, friends=friends_of(who), whose=possessive(who))


@app.route("/on-this-day")
@login_required
def on_this_day(sp):
    """On this day in earlier years. ?date=MM-DD to look at another day (default: today)."""
    who = chosen_person()
    today = datetime.now(stats.LOCAL_TZ).date()
    month, day = today.month, today.day
    m = re.fullmatch(r"(\d{2})-(\d{2})", request.args.get("date", ""))
    if m:
        try:
            datetime(2024, int(m[1]), int(m[2]))      # 2024 is a leap year, so 29 Feb is allowed
            month, day = int(m[1]), int(m[2])
        except ValueError:
            pass                                       # not a real date: stay on today
    shown = datetime(2024, month, day).date()
    step = lambda days: (shown + timedelta(days=days)).strftime("%m-%d")
    return render_template(
        "on_this_day.html", user=sp.current_user(), who=who,
        label=f"{shown.day} {shown:%B}", is_today=(month, day) == (today.month, today.day),
        prev_day=step(-1), next_day=step(1),
        years=entity.on_this_day(who, month, day, today) if who else [],
    )


@app.route("/compare")
@login_required
def compare_page(sp):
    """You vs a friend. ?with=<their ID> (default: the first other person), ?period=<year>."""
    me = session.get("user_id")
    everyone = stats.people()
    others = [p for p in everyone if p["id"] != me]
    if me not in [p["id"] for p in everyone] or not others:
        return render_template("error.html", message="Comparing needs your history and at least one friend's."), 404
    other = request.args.get("with")
    if other not in [p["id"] for p in others]:          # only people actually in the database
        other = others[0]["id"]
    data = compare.compare(me, other, request.args.get("period"))   # an unknown period falls back to shared years
    if not data:
        return render_template("error.html", message="Your histories don't overlap in time, so there's nothing fair to compare."), 404
    them = next(p for p in others if p["id"] == other)
    return render_template("compare.html", user=sp.current_user(), who=me, c=data,
                           them=them, others=others, other=other)


@app.route("/vs")
@login_required
def vs_panel(sp):
    """The 'Compare with ...' panel on song and artist pages, loaded only when the button is clicked.
    ?kind=song&track=ID or ?kind=artist&name=..., plus ?who= (whose page) and ?with= (the friend)."""
    who = chosen_person()
    ids = {p["id"]: p["display_name"] for p in stats.people()}
    friend = request.args.get("with")
    if friend not in ids or friend == who:
        abort(400)
    me = session.get("user_id")
    name_a = "You" if who == me else ids[who]
    name_b = "You" if friend == me else ids[friend]
    kind = request.args.get("kind")
    if kind == "song":
        track = request.args.get("track", "")
        if not SONG_ID.fullmatch(track):
            abort(400)
        data = compare.vs_song(who, friend, track, name_a, name_b)
    elif kind == "artist":
        data = compare.vs_artist(who, friend, request.args.get("name", "")[:200], name_a, name_b)
    elif kind == "album":
        name, artist = request.args.get("name", "")[:300], request.args.get("artist", "")[:200]
        data = compare.vs_album(who, friend, name, artist, name_a, name_b, albums.get(name, artist))
    else:
        abort(400)
    if data is None:
        abort(404)
    return render_template("_vs.html", v=data, who=who, friend=friend,
                           subject=request.args.get("label", "")[:200])


def possessive(who):
    """('Your', 'your') for your own pages, ("Ethan's", "Ethan's") for someone else's."""
    if who == session.get("user_id"):
        return "Your", "your"
    name = next((p["display_name"] for p in stats.people() if p["id"] == who), who)
    return f"{name}'s", f"{name}'s"


def friends_of(who):
    """Everyone else in the database, for the 'Compare with ...' buttons."""
    return [p for p in stats.people() if p["id"] != who]


@app.route("/trips")
@login_required
def trips_page(sp):
    """Trips abroad. PRIVATE: always the logged-in person's own trips. There is deliberately
    no ?who= here, so nobody can look up where someone else has been."""
    me = session.get("user_id")
    data = trips.trips(me) if me in [p["id"] for p in stats.people()] else None
    page_theme = None
    if data and data["trips"]:
        page_theme = saved_art("track", data["trips"][0]["top"][0]["track"])[1]

    # Trips together: only with friends who have ALSO switched sharing on.
    sharing = shares_trips(me)
    together = []
    if sharing and data and data["trips"]:
        for friend in friends_of(me):
            if shares_trips(friend["id"]):
                result = trips.together(me, friend["id"], friend["display_name"])
                if result["trips"]:
                    together.append(result)
    waiting = [f["display_name"] for f in friends_of(me) if not shares_trips(f["id"])]
    return render_template("trips.html", user=sp.current_user(), who=me, t=data, page_theme=page_theme,
                           sharing=sharing, together=together, waiting=waiting)


# ------------------------------------------------------------------ listening right now

def can_see_playing(who):
    """Your own current song, always; a friend's only if they switched sharing on."""
    return who == session.get("user_id") or shares_playing(who)


@app.route("/api/playing")
@login_required
def api_playing(sp):
    """What someone is playing this second (JSON), plus what their history says about it.
    The browser asks every ~20 s while a page is open; nowplaying.py caches Spotify's answer."""
    me = session["user_id"]
    who = request.args.get("who") or me
    names = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
    if who != me and who not in names:
        abort(404)
    if not can_see_playing(who):
        return jsonify({"state": "private", "who": who, "name": names.get(who, who)})
    data = nowplaying.playing(who)
    data.update(who=who, name="You" if who == me else names.get(who, who))
    if data["state"] in ("playing", "paused") and who in names:
        data["about"] = nowplaying.about(who, data["track_id"], data["song"], data["artist"], data["album"])
    return jsonify(data)


@app.route("/api/playing/friends")
@login_required
def api_playing_friends(sp):
    """Friends who share what they're playing, and what it is (only the ones playing something)."""
    me = session["user_id"]
    out = []
    for p in stats.people():
        if p["id"] == me or not shares_playing(p["id"]):
            continue
        data = nowplaying.playing(p["id"])
        if data["state"] in ("playing", "paused"):
            out.append({k: data[k] for k in ("state", "track_id", "song", "artist", "image")} |
                       {"who": p["id"], "name": p["display_name"] or p["id"]})
    return jsonify(out)


@app.route("/now/share", methods=["POST"])
@login_required
def now_share(sp):
    """Switch 'friends can see what I'm playing' on or off for yourself."""
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)
    set_share_playing(session["user_id"], request.form.get("on") == "1")
    return redirect(url_for("now_page") + "#playing")


# ------------------------------------------------------------------ playlists

PLAYLIST_ARGS = ("kind", "level", "id", "track", "new", "with", "year", "season", "n")


def playlist_for(me, args):
    """Work out the playlist an address asks for (?kind=...). None if the request doesn't make sense.
    Everything is checked against allowed values: people must be on the site, IDs must look right."""
    kind = args.get("kind", "")
    limit = int(args["n"]) if args.get("n") in ("20", "30", "50") else 30
    level = int(args["level"]) if args.get("level") in ("1", "2", "3", "4") else 2
    others = {p["id"] for p in stats.people()} - {me}
    if kind == "mood":
        return playlists.mood_playlist(me, level, args.get("id", ""), limit)
    if kind == "now":
        return playlists.right_now(me, level, limit=limit)
    if kind == "like":
        track = args.get("track", "")
        return playlists.like(me, track, only_new=args.get("new") == "1", limit=limit) if SONG_ID.fullmatch(track) else None
    if kind in ("blend", "wayin"):
        friend = args.get("with", "")
        if friend not in others:
            return None
        return (playlists.blend if kind == "blend" else playlists.way_in)(me, friend, limit)
    if kind == "group":
        friends = [f for f in args.get("with", "").split(",") if f in others][:4]
        if not friends:
            return None
        return playlists.group_mix(me, friends, limit) if len(friends) > 1 else playlists.blend(me, friends[0], limit)
    if kind == "forgotten":
        return playlists.forgotten(me, limit)
    if kind == "comfort":
        return playlists.comfort(me, limit)
    if kind == "season":
        year, season = args.get("year", ""), args.get("season", "")
        if year.isdigit() and season in ("winter", "spring", "summer", "autumn"):
            return playlists.season(me, int(year), season, limit)
    return None


def playlist_args():
    """The query, tidied: several ?with= boxes become one comma list, and a season pick "2022:summer" splits."""
    args = {k: request.args.get(k, "") for k in PLAYLIST_ARGS if request.args.get(k)}
    if len(request.args.getlist("with")) > 1:
        args["with"] = ",".join(request.args.getlist("with"))
    if ":" in request.args.get("pick", ""):
        args["year"], args["season"] = request.args["pick"].split(":", 1)
    return args


@app.route("/playlists")
@login_required
def playlists_page(sp):
    """Playlists made from how you listen: moods, right now, more like this, friends, time machine."""
    me = session["user_id"]
    if stats.latest_play_id(me) is None:
        return render_template("welcome.html", user=sp.current_user(),
                               friends=[p for p in stats.people() if p["id"] != me])
    level = int(request.args["level"]) if request.args.get("level") in ("1", "2", "3", "4") else 2
    return render_template("playlists.html", user=sp.current_user(), who=me, level=level,
                           level_names=playlists.LEVEL_NAMES, moods=playlists.moods(me, level),
                           now=playlists.right_now(me, level, limit=4),
                           friends=[p for p in stats.people() if p["id"] != me],
                           seasons=playlists.seasons_available(me)[:24])


@app.route("/playlist")
@login_required
def playlist_page(sp):
    me = session["user_id"]
    args = playlist_args()
    pl = playlist_for(me, args)
    if not pl:
        abort(404)
    return render_template("playlist.html", user=sp.current_user(), who=me, pl=pl, args=args,
                           can_save=playlists.can_save(me), saved=session.pop("saved_playlist", None))


@app.route("/playlist/save", methods=["POST"])
@login_required
def playlist_save(sp):
    """Save the playlist into YOUR Spotify (private). It's worked out again here from the
    address, never taken from the page, so the page can't slip other songs in."""
    me = session["user_id"]
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)
    args = {k: request.form[k] for k in PLAYLIST_ARGS if request.form.get(k)}
    pl = playlist_for(me, args)
    if not pl or not pl["songs"]:
        abort(404)
    if not playlists.can_save(me):
        return render_template("error.html", title="One more login needed",
                               message="Saving to Spotify needs a permission the site didn't have when you last "
                                       "logged in. Log out and back in once, then try again."), 403
    try:
        url = playlists.save_to_spotify(me, pl["title"], f"{pl['subtitle']} · made by Listening Stats",
                                        [s["track"] for s in pl["songs"]])
    except Exception as err:
        return render_template("error.html", title="Spotify didn't save it",
                               message=f"Spotify said no ({str(err)[:120]}). Try again in a minute."), 502
    session["saved_playlist"] = url
    return redirect(url_for("playlist_page", **args))


@app.route("/moods/rename", methods=["POST"])
@login_required
def mood_rename(sp):
    """Give one of your mood groups your own name (empty = back to the automatic one)."""
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)
    level = int(request.form["level"]) if request.form.get("level") in ("1", "2", "3", "4") else 2
    playlists.rename(session["user_id"], level, request.form.get("id", ""), request.form.get("name", ""))
    return redirect(url_for("playlists_page", level=level) + "#moods")


@app.route("/trips/share", methods=["POST"])
@login_required
def trips_share(sp):
    """Switch 'trips together' on or off for yourself (and only yourself)."""
    if request.form.get("csrf") != session.get("csrf"):
        abort(400)        # not a form from our own page
    set_share_trips(session["user_id"], request.form.get("on") == "1")
    return redirect(url_for("trips_page") + "#together")


@app.route("/journey")
@login_required
def journey_page(sp):
    """Eras, what changed between them, discovery, loyalty. ?who= and ?eras=broad|detailed."""
    who = chosen_person()
    detail = request.args.get("eras")
    if detail not in ("broad", "detailed"):
        detail = "broad"
    data = journey.journey(who, detail) if who else None
    if not data:
        return render_template("error.html", message="Not enough history yet to find eras (needs about 8 months)."), 404
    current = data["eras"][-1]
    _, colour = saved_art("artist", current["lead_track"]) if current["lead_track"] else (None, None)
    return render_template("journey.html", user=sp.current_user(), who=who, j=data, detail=detail,
                           people=stats.people(), page_theme=colour,
                           whose="Your" if who == session.get("user_id") else
                           next((p["display_name"] for p in stats.people() if p["id"] == who), who) + "'s")


@app.errorhandler(400)
def bad_request(_err):
    """Usually a form from a page that was open from before the last login (its hidden security
    code no longer matches), or a double-click. Reloading fixes it."""
    return render_template("error.html", title="That page was out of date",
                           message="This usually means the page was open from before you last logged in, or a button "
                                   "was pressed twice. Go back, reload the page (Ctrl+F5) and try again."), 400


@app.errorhandler(500)
def server_error(_err):
    return render_template("error.html", title="Something broke on our side",
                           message="The site hit an error working that out. Try again in a minute; if it keeps "
                                   "happening, tell Jack which page it was."), 500


@app.errorhandler(404)
def not_found(err):
    return render_template("error.html", message="Nothing here: that song, artist or page isn't in this person's history."), 404


@app.route("/favicon.ico")
def favicon():
    """Some browsers ask for /favicon.ico whatever the page says: send them the SVG one."""
    return redirect(url_for("static", filename="favicon.svg"), code=301)


@app.route("/map/world.svg")
def world_map():
    """The world map behind the Trips page's pins (drawn once from static/geo; see geo.py).
    Public and the same for everyone, so browsers may keep it for a week."""
    import geo
    response = app.response_class(geo.world_svg(), mimetype="image/svg+xml")
    response.headers["Cache-Control"] = "public, max-age=604800"
    return response


@app.route("/art")
@login_required
def art(sp):
    """Picture + colour for one item, as JSON. Fetched from Spotify the first time,
    then served from the database. Called by the page in the background."""
    kind, track = request.args.get("kind"), request.args.get("track", "")
    if kind not in ("track", "artist") or not SPOTIFY_ID.fullmatch(track):
        abort(400)                              # never pass unchecked input to Spotify
    saved = artwork.get(kind, track)
    if saved is None:
        if spotify_paused():                         # Spotify told us to wait: don't keep asking
            return jsonify({"error": "spotify", "status": 429}), 503
        try:
            saved = artwork.fetch(sp, kind, track)
        except spotipy.SpotifyException as err:
            pause_after(err)        # on a 429, ALL extra lookups pause for as long as Spotify asked
            # Not saved, so it's tried again next time.
            return jsonify({"error": "spotify", "status": err.http_status}), 503
    response = jsonify(saved)
    response.cache_control.max_age = 86400      # the browser can keep it for a day too
    return response


# ------------------------------------------------------------------ the "alarm clock"

last_poll = 0.0
POLL_EVERY_AT_MOST = 5 * 60      # seconds: however often it's called, Spotify is asked at most this often


@app.route("/internal/poll", methods=["POST"])
def internal_poll():
    """Fetch everyone's new plays. Called every 30 minutes by your PC or Raspberry Pi
    (see ping_site.py), because the free PythonAnywhere plan has no timers of its own.

    Protected by a key: the caller must send the same POLL_KEY as in the server's .env,
    in an "X-Poll-Key" header. Without POLL_KEY set, the address doesn't exist at all.
    """
    global last_poll
    key = os.environ.get("POLL_KEY")
    if not key:
        abort(404)
    # compare_digest takes the same time whether the first or last character is wrong,
    # so the key can't be guessed one character at a time by timing the answers.
    if not hmac.compare_digest(request.headers.get("X-Poll-Key", ""), key):
        abort(403)
    if time.time() - last_poll < POLL_EVERY_AT_MOST:
        return jsonify({"skipped": "polled less than 5 minutes ago"}), 429
    last_poll = time.time()
    results = poller.poll_all()
    # Then work out everyone's main pages straight away, while nobody is waiting.
    return jsonify({"new_plays": results, "warmed": warm.warm()})


@app.route("/healthz")
def healthz():
    """For Kubernetes: answers "ok" if the app is up and can read its database."""
    from db import connect
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()
    return "ok"


@app.route("/logout")
def logout():
    # Logs you out of the website only. Your token stays in the database so the
    # poller keeps recording plays. To stop that completely, remove the app at
    # spotify.com -> Account -> Manage apps.
    session.clear()
    return redirect(url_for("index"))


@app.errorhandler(spotipy.SpotifyException)
def spotify_error(err):
    """Turn Spotify API errors into a readable page instead of a stack trace."""
    if err.http_status == 403:
        message = ("Spotify refused access. In Development Mode, every user has to be "
                   "added under User Management in the developer dashboard.")
    elif err.http_status == 429:
        message = "Too many requests to Spotify. Wait a minute and try again."
    else:
        message = f"Spotify returned an error ({err.http_status})."
    return render_template("error.html", message=message), 502


scheduler.start()   # only does anything when RUN_SCHEDULER=1 (in the cluster)


if __name__ == "__main__":
    # 127.0.0.1 (not "localhost"): Spotify only accepts explicit loopback addresses.
    # debug=True auto-reloads when you save a file. Never use it on a public server.
    app.run(host="127.0.0.1", port=8000, debug=True)
