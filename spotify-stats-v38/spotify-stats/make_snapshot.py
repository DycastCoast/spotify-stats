"""
Make a snapshot: a folder of saved pages someone can click around in a browser,
with no server, no login and nothing to install. Seen as the person you choose.

Usage:  python make_snapshot.py --as ETHANS_ID
        python make_snapshot.py --as ETHANS_ID --fetch-art        (first looks up the 150 most visible missing covers)
        python make_snapshot.py --as ETHANS_ID --fetch-art 300    (... or more; slowly, to stay under Spotify's limits)

Then send them the zip it makes. They unzip it and double-click index.html.

What's in it: their overview (all time and each year), their Journey, Compare with
each friend, On this day around today, and the song and artist pages linked from
those, plus the same overview and Journey pages for everyone else (the same access
they'd have on the real site).

What's NOT in it, on purpose:
- Trips: private to each person. Building someone else's trips page on your computer
  would show them to you, which is exactly what the privacy rule stops.
- Top and Recent: they ask Spotify live, with the person's own login.
- The database and anyone's Spotify login: the pages are plain HTML, nothing else.

How it works: the website runs inside this script (Flask's "test client", the same
trick used for testing), pretending to be logged in as that person. Each page is
saved, and every link is rewritten to point at the saved file instead of the server.
A small script (static/snapshot.js) does the rest in the browser.
"""
import argparse
import html
import json
import re
import shutil
import sys
import time
import zipfile
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

HERE = Path(__file__).parent
ENTITY_PAGES = ("/song/", "/artist", "/album")
SNAPSHOT_CSS = """
.snapshot-banner { background: #1d2a44; color: #dfe8ff; font-size: 0.85rem; padding: 8px 24px; text-align: center; }
.snapshot-banner b { color: #fff; }
.snap-missing { opacity: 0.4; }
.logout, .share-btn, .together { display: none !important; }
"""


def key(url, drop, ignore):
    """A page's address as plain text, with default and empty settings removed, so two links to the
    same page always match. static/snapshot.js does exactly the same in the browser."""
    parts = urlsplit(url)
    params = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
              if v != "" and f"{k}={v}" not in drop and k not in ignore]
    params.sort(key=lambda kv: f"{kv[0]}={kv[1]}")
    return parts.path + ("?" + "&".join(f"{k}={v}" for k, v in params) if params else "")


def main():
    parser = argparse.ArgumentParser(description="Save the site as plain pages someone can click around.")
    parser.add_argument("--as", dest="viewer", required=True, help="Spotify user ID of who it's for")
    parser.add_argument("--out", default="snapshot", help="folder to write (default: snapshot)")
    parser.add_argument("--max-pages", type=int, default=500, help="most song/artist pages to include (default 500)")
    parser.add_argument("--days", type=int, default=7, help="On this day: this many days either side of today")
    parser.add_argument("--fetch-art", type=int, nargs="?", const=150, default=0, metavar="N",
                        help="look up to N missing pictures on Spotify first, most visible first (default N: 150)")
    args = parser.parse_args()

    import app as site           # the website itself (this also loads .env)
    import artwork
    import compare
    import entity
    import stats
    from db import all_user_ids, connect

    people = {p["id"]: p["display_name"] or p["id"] for p in stats.people()}
    viewer = args.viewer
    if viewer not in people:
        sys.exit(f"No one called {viewer} in the database. People: " + ", ".join(f"{n} ({i})" for i, n in people.items()))
    friends = [p for p in people if p != viewer]
    drop = ["year=all", "eras=broad", f"who={viewer}"] + ([f"with={friends[0]}"] if friends else [])
    ignore = ["tab", "format", "label"]
    k = lambda url: key(url, drop, ignore)

    # Pretend to be logged in as the viewer. No Spotify calls happen: the pages
    # in the snapshot only use the database.
    class NoSpotify:
        def current_user(self):
            return {"id": viewer, "display_name": people[viewer], "images": []}
    site.get_spotify = lambda: NoSpotify()
    client = site.app.test_client()
    with client.session_transaction() as s:
        s["user_id"] = viewer

    # --- 1. The main pages
    seeds = []
    for p in people:
        seeds += [f"/now?who={p}", f"/skips?who={p}"]
        seeds.append(f"/overview?who={p}")
        seeds += [f"/overview?who={p}&year={y}" for y in stats.years(p)]
        seeds += [f"/journey?who={p}&eras={e}" for e in ("broad", "detailed")]
    for f in friends:
        seeds.append(f"/compare?with={f}")
        seeds += [f"/compare?with={f}&period={y}" for y in compare.periods(viewer, f)[2]]
    today = datetime.now(stats.LOCAL_TZ).date()
    seeds += [f"/on-this-day?date={(today + timedelta(days=d)):%m-%d}" for d in range(-args.days, args.days + 1)]

    pages, order, fragments = {}, [], {}
    started = time.time()

    def fetch(url):
        res = client.get(url)
        if res.status_code != 200:
            return None
        return res.get_data(as_text=True)

    print(f"Saving the main pages as {people[viewer]}...")
    for url in seeds:
        page = fetch(url)
        if page and k(url) not in pages:
            pages[k(url)] = page
            order.append(k(url))

    # --- 2. Song and artist pages linked from the main pages (most prominent first)
    links = []
    for kk in list(order):
        for href in re.findall(r'href="(/[^"#]*)"', pages[kk]):
            href = html.unescape(href)
            if href.startswith(ENTITY_PAGES) and k(href) not in pages and k(href) not in links:
                links.append(k(href))
    # Artist and album pages first (fewer and richer), then songs fill the rest.
    links.sort(key=lambda u: 0 if u.startswith("/artist") else 1 if u.startswith("/album") else 2)
    print(f"Saving {min(len(links), args.max_pages)} song and artist pages (of {len(links)} linked)...")
    for n, url in enumerate(links[:args.max_pages], 1):
        page = fetch(url)
        if not page:
            continue
        pages[url] = page
        order.append(url)
        # The "Compare with ..." panels, saved into the page so the buttons still work.
        parts = dict(parse_qsl(urlsplit(url).query))
        who = parts.get("who", viewer)
        for f in people:
            if f == who:
                continue
            if url.startswith("/song/"):
                q = {"kind": "song", "track": urlsplit(url).path.split("/")[-1], "who": who, "with": f}
            elif url.startswith("/album"):
                q = {"kind": "album", "name": parts.get("name", ""), "artist": parts.get("artist", ""), "who": who, "with": f}
            else:
                q = {"kind": "artist", "name": parts.get("name", ""), "who": who, "with": f}
            frag = fetch("/vs?" + urlencode(q))
            if frag:
                fragments.setdefault(url, {})[f] = frag
        if n % 50 == 0:
            print(f"  {n} done")

    # --- 3. File names, then rewrite every page's links to point at the files
    files = {}
    counters = {}
    home = k(f"/overview?who={viewer}")
    for kk in order:
        if kk == home:
            files[kk] = "index.html"
            continue
        kind = kk.split("?")[0].strip("/").split("/")[0] or "page"
        counters[kind] = counters.get(kind, 0) + 1
        files[kk] = f"{kind}-{counters[kind]}.html"
    files["/"] = files["/overview"] = "index.html"
    for alias in ("/now", "/skips"):                    # nav links without ?who= mean "the viewer"
        if k(f"{alias}?who={viewer}") in files:
            files[alias] = files[k(f"{alias}?who={viewer}")]
    today_page = k(f"/on-this-day?date={today:%m-%d}")
    if today_page in files:
        files["/on-this-day"] = files[today_page]      # the nav link has no date: it means today

    def rewrite(page):
        page = re.sub(r'(href|src)="/static/', r'\1="static/', page)
        return re.sub(r'href="(/[^"]*)"',
                      lambda m: f'href="{files.get(k(html.unescape(m.group(1))), "missing.html")}"', page)

    made = datetime.now(stats.LOCAL_TZ)
    banner = (f'<div class="snapshot-banner">A snapshot of the listening stats site, saved {made.day} {made:%b %Y}, '
              f'seen as <b>{html.escape(people[viewer])}</b>. Dimmed links are only on the live site.</div>')
    out = HERE / args.out
    if out.exists():
        shutil.rmtree(out)
    (out / "static").mkdir(parents=True)
    for kk in order:
        page = rewrite(pages[kk])
        head = (f'<script>window.SNAPSHOT_PATH = {json.dumps(kk.split("?")[0])};</script>'
                f'<script src="snapshot-data.js"></script><script src="static/snapshot.js"></script>'
                f'<style>{SNAPSHOT_CSS}</style>')
        page = page.replace("</head>", head + "</head>", 1)
        page = re.sub(r"(<body[^>]*>)", r"\1" + banner, page, count=1)
        templates = "".join(f'<template data-vs="{html.escape(f)}">{rewrite(frag)}</template>'
                            for f, frag in fragments.get(kk, {}).items())
        page = page.replace("</body>", templates + "</body>", 1)
        (out / files[kk]).write_text(page, encoding="utf-8")

    # --- 4. Pictures: every cover/photo the pages ask for, from the saved artwork
    # In page order (main pages first), so the most visible pictures come first.
    wanted = []
    for text in [pages[kk] for kk in order] + [f for kk in order for f in fragments.get(kk, {}).values()]:
        for pair in re.findall(r'data-art-kind="(\w+)" data-art-track="(\w+)"', text):
            if pair not in wanted:
                wanted.append(pair)
    if args.fetch_art:
        missing = [(kind, t) for kind, t in wanted if artwork.get(kind, t) is None][:args.fetch_art]
        holder = next(iter(all_user_ids()), None)
        if missing and holder:
            print(f"Looking up {len(missing)} missing pictures on Spotify...")
            from spotify_client import make_client, retry_after
            done = 0
            try:
                sp = make_client(site.DatabaseCacheHandler(holder))     # fails fast instead of waiting hours
                for done, (kind, t) in enumerate(missing, 1):
                    artwork.fetch(sp, kind, t)
                    if done % 25 == 0:
                        print(f"  {done} done")
                    time.sleep(0.6)       # each picture is 1-2 Spotify requests: stay well under the limit
            except site.spotipy.SpotifyException as err:
                if err.http_status == 429:
                    wait = retry_after(err)
                    print(f"  Spotify says too many requests{f' (wait {wait // 60} minutes)' if wait else ''}. "
                          f"Stopped after {done - 1}; carrying on with the pictures already saved.")
                else:
                    print(f"  Spotify error {err.http_status}; carrying on with the pictures already saved.")
            except Exception as err:                     # offline etc.: keep what we have
                print(f"  stopped early ({type(err).__name__}); carrying on with the pictures already saved")
    art = {}
    with connect() as conn:
        for kind, t in wanted:
            row = conn.execute("SELECT image_url, colour_hex, colour_rgb, colour_name FROM artwork "
                               "WHERE kind = ? AND track_id = ?", (kind, t)).fetchone()
            if row and row["image_url"]:
                art[f"{kind}:{t}"] = dict(row)

    # --- 5. The search box: the songs and artists that have a page in the snapshot
    songs, artists = [], []
    for kk in order:
        parts = urlsplit(kk)
        who = dict(parse_qsl(parts.query)).get("who", viewer)
        tl = entity.timeline(who)
        if parts.path.startswith("/song/"):
            t = tl.index_of.get(parts.path.split("/")[-1])
            if t is not None:
                name, artist_name, _ = tl.track_info[t]
                songs.append({"who": who, "track": tl.track_ids[t], "name": name, "artist": artist_name,
                              "plays": sum(1 for i in tl.by_track[t] if tl.listened(i))})
        elif parts.path == "/artist":
            a = dict(parse_qsl(parts.query)).get("name")
            if a in tl.artist_ms:
                best = tl.artist_best.get(a)
                artists.append({"who": who, "name": a, "hours": round(tl.artist_ms[a] / 3_600_000),
                                "track": tl.track_ids[best] if best is not None else ""})
    songs.sort(key=lambda s: -s["plays"])
    artists.sort(key=lambda a: -a["hours"])
    data = {"viewer": viewer, "pages": files, "drop": drop, "ignore": ignore, "art": art,
            "songs": songs, "artists": artists}
    (out / "snapshot-data.js").write_text("window.SNAPSHOT_DATA = " + json.dumps(data, separators=(",", ":")) + ";",
                                          encoding="utf-8")

    # --- 6. The rest: styles, the "not in this snapshot" page, a readme, and the zip
    shutil.copy(HERE / "static" / "style.css", out / "static" / "style.css")
    shutil.copy(HERE / "static" / "snapshot.js", out / "static" / "snapshot.js")
    (out / "missing.html").write_text(
        '<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Not in this snapshot</title>'
        '<link rel="stylesheet" href="static/style.css"></head><body><main><h1>Not in this snapshot</h1>'
        '<p class="small">This page needs the live site (Top and Recent ask Spotify live, and Trips are private), '
        'or it wasn\'t one of the pages saved. <a class="inline-link" href="javascript:history.back()">Go back</a> '
        'or <a class="inline-link" href="index.html">start again</a>.</p></main></body></html>', encoding="utf-8")
    (out / "README.txt").write_text(
        f"Listening stats snapshot for {people[viewer]}, saved {made:%d %b %Y}.\n\n"
        "Open index.html in a browser (double-click it). Everything works offline except the\n"
        "album covers and fonts, which load from the internet.\n", encoding="utf-8")
    zip_path = HERE / f"{args.out}-{people[viewer].lower().replace(' ', '-')}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(out.rglob("*")):
            if f.is_file():
                z.write(f, Path(args.out) / f.relative_to(out))

    size = sum(f.stat().st_size for f in out.rglob("*") if f.is_file())
    print(f"Done in {time.time() - started:.0f} s: {len(order)} pages, {len(art)} of {len(wanted)} pictures "
          f"({size / 1e6:.0f} MB, zip {zip_path.stat().st_size / 1e6:.1f} MB).")
    if len(art) < len(wanted):
        print(f"  {len(wanted) - len(art)} pictures haven't been looked up yet (they show as blank squares).")
        print("  Run again later with --fetch-art to look up the 150 most visible (more: --fetch-art 300).")
    print(f"Send them: {zip_path.name}")


if __name__ == "__main__":
    main()
