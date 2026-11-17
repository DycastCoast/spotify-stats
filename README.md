# Listening Stats

A personal Spotify stats site for me and a few friends.

## Setup

1. **Create a virtual environment** (keeps this project's packages separate from everything else):
   ```bash
   python -m venv .venv
   # Windows:      .venv\Scripts\activate
   # Mac/Linux:    source .venv/bin/activate
   ```
2. **Install packages:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Add your secrets:** copy `.env.example` to `.env` and fill in the values.
4. **Run it:**
   ```bash
   python app.py
   ```
   Then open http://127.0.0.1:8000. Use `127.0.0.1`, not `localhost`, so it matches the redirect URI.

## Collecting listening history

Logging in on the website saves your account and your last 50 plays.
After that, run the poller to save anything new:

```bash
python poller.py     # fetch and save new plays for every user
python db.py         # show how many plays are saved
```

The poller is safe to run as often as you like; already-saved plays are skipped.

## Importing a Spotify export (full history)

Request "Extended streaming history" from Spotify's privacy page, put the zip in
`data/` (no need to extract it), then:

```bash
python import_export.py data/my_spotify_data.zip                         # your own, if you're the only user
python import_export.py data/friend.zip --user <their-spotify-id> --name Ethan
```

Their Spotify ID is the last part of their profile link (Share -> Copy link to profile).
Private-session plays are skipped unless you add `--include-private`.
Running it again, or importing a newer export, only adds plays that are new.

## Importing an Apple Music history

On privacy.apple.com: "Request a copy of your data" -> tick "Apple Media Services
information" -> wait for the email (up to about a week). Then, without unzipping:

```bash
python import_apple.py "Apple Media Services information.zip" --inspect      # look first, changes nothing
python import_apple.py "Apple Media Services information.zip" --user <their-spotify-id> --name Sam
```

They still log in to the site with a Spotify account (a free one is fine), so use that ID.
Apple songs get made-up IDs ("am-..."): every page works and they compare with Spotify users
by song + artist name, but there are no covers or album tracklists for them. If Apple's file
doesn't fit, `--inspect` shows its columns; the names to look for are listed at the top of
import_apple.py.

## Project layout

```
app.py              website routes and Spotify login
poller.py           saves recently played tracks to the database
db.py               database tables and helpers (stats.db)
spotify_client.py   Spotify login settings shared by app and poller
templates/          HTML pages (Jinja templates)
static/style.css    all styling; colours live at the top in :root
.env                your secrets (never committed)
stats.db            your listening data (never committed)
```
