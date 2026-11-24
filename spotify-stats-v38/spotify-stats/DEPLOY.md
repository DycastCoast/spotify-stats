# Putting the site online (PythonAnywhere, free plan)

About 1–2 hours. Do the steps in order. If your internet drops, just redo the step you were on:
nothing goes live until step 9, and your PC's copy isn't touched.

Wherever you see `USERNAME`, use your PythonAnywhere username. Your site will be
`https://USERNAME.pythonanywhere.com` (or `USERNAME.eu.pythonanywhere.com` if you sign up on the EU site).

**Never paste secrets (client secret, FLASK_SECRET_KEY, POLL_KEY) into a chat.** They only ever go in `.env` files.

---

## 0. Before you start
- Wait until Spotify's rate limit has lifted (try logging in locally; if it works, you're good).
- Commit and push your latest code to GitHub (Source Control → Commit → Sync).

## 1. Make the account
Sign up at pythonanywhere.com → "Create a Beginner account" (free). Pick the username carefully: it's in your web address.

## 2. Check the free plan can reach Spotify
Dashboard → **Consoles** → **Bash**. Paste:
```
curl -s -o /dev/null -w "%{http_code}\n" https://api.spotify.com/v1/
curl -s -o /dev/null -w "%{http_code}\n" https://accounts.spotify.com/
```
- `401` / `200` / `30x` = it reached Spotify. Carry on.
- `403`, or "Tunnel connection failed" = Spotify isn't on the free plan's allowlist. Stop here and tell Claude:
  the options are asking PythonAnywhere to add it (help.pythonanywhere.com → "Requesting Allowlist additions")
  or the $10/month plan.

## 3. Get the code
GitHub won't let a server read a private repo with your password. Make a read-only key:
GitHub → your picture → **Settings** → **Developer settings** → **Personal access tokens** → **Fine-grained tokens** →
**Generate new token**: only your stats repo, **Contents: Read-only**, expiry 90 days. Copy it.

In the Bash console (replace YOURGITHUB and REPO):
```
git clone https://github.com/YOURGITHUB/REPO.git spotify-stats
```
Username: your GitHub username. Password: paste the token (nothing shows as you paste: that's normal).

## 4. Install the packages
```
cd ~/spotify-stats
python3.13 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
Takes a few minutes. (Same as on your PC, except Linux says `source .venv/bin/activate`.)

## 5. The live site's .env
**Files** tab → open `spotify-stats` → type `.env` in the "new file" box → **New file**. Fill in:
```
SPOTIFY_CLIENT_ID=(same as your PC's)
SPOTIFY_CLIENT_SECRET=(same as your PC's)
SPOTIFY_REDIRECT_URI=https://USERNAME.pythonanywhere.com/callback
FLASK_SECRET_KEY=(a NEW random string)
POLL_KEY=(another NEW random string)
PRODUCTION=1
```
Make the random strings in the Bash console with `python3 -c "import secrets; print(secrets.token_hex(32))"`
(run it twice: one for each). Click **Save**.

## 6. Move your database across
On your PC:
1. **Task Scheduler** → your poller task → **Disable** (so the database isn't changing while you copy it,
   and because the live site takes over polling in step 10).
2. In File Explorer, right-click `stats.db` → **Send to** → **Compressed (zipped) folder** → `stats.zip`.
   (The upload page takes files up to ~100 MB; zipped it's much smaller.)

On PythonAnywhere: **Files** tab → `spotify-stats` → **Upload a file** → `stats.zip`. Then in the Bash console:
```
cd ~/spotify-stats && unzip stats.zip && rm stats.zip && ls -lh stats.db
```

## 7. Create the web app
**Web** tab → **Add a new web app** → Next → **Manual configuration** (not "Flask") → **Python 3.13** → Next.

On the page that appears:
- **Source code**: `/home/USERNAME/spotify-stats`
- **Virtualenv**: `/home/USERNAME/spotify-stats/.venv`
- **Static files**: URL `/static/` → Directory `/home/USERNAME/spotify-stats/static`
- **Force HTTPS**: Enabled
- Click the **WSGI configuration file** link, delete everything in it, paste this, **Save**:
```python
import sys
from pathlib import Path

project = Path("/home/USERNAME/spotify-stats")
sys.path.insert(0, str(project))

from dotenv import load_dotenv
load_dotenv(project / ".env")

from app import app as application   # PythonAnywhere looks for a variable called "application"
```
Then press the big green **Reload** button at the top.

## 8. Tell Spotify about the new address
developer.spotify.com/dashboard → your app → **Settings** → **Redirect URIs** → add
`https://USERNAME.pythonanywhere.com/callback` (exactly, no slash at the end). Keep the 127.0.0.1 one too, so your
PC copy still works. **Save**.

While you're there: **User Management** → add Ethan (his name + the email of his Spotify account).
Ask him first: logging in means his plays get collected.

## 9. Try it
Open `https://USERNAME.pythonanywhere.com` → **Log in with Spotify**. Click through the pages.
- Error page? **Web** tab → **Error log** (bottom of the list of logs). The newest error is at the end.

## 10. The alarm clock (fetching new plays every 30 minutes)
The free plan has no timers, so your PC (or Pi) nudges the site.

On your PC, add two lines to your PC's `.env`:
```
LIVE_SITE=https://USERNAME.pythonanywhere.com
POLL_KEY=(the same POLL_KEY as in step 5)
```
Test it (venv active): `python ping_site.py` → should print `200 {"your-id": 3, ...}`.

Then in **Task Scheduler**: open your old poller task → **Actions** → **Edit** → change the argument from
`poller.py` to `ping_site.py` → OK → **Enable** the task. It now nudges the live site instead of filling your local database.

**Raspberry Pi instead?** `crontab -e` and add one line (all on one line):
```
*/30 * * * * curl -fsS -X POST -H "X-Poll-Key: YOURPOLLKEY" https://USERNAME.pythonanywhere.com/internal/poll
```

## 11. Keeping it running
- **Once a month**: **Web** tab → **Run until 1 month from today**. (Free web apps switch off otherwise; PythonAnywhere emails a reminder.)
- **Updating the code**: push from your PC as usual, then in the Bash console `cd ~/spotify-stats && git pull`,
  then **Reload** on the Web tab. Run `pip install -r requirements.txt` too if requirements changed.
- **The live database is now the real one.** Never upload your PC's `stats.db` over it: that would wipe every play
  collected since. To get a fresh copy on your PC (e.g. for make_snapshot.py), zip it on PythonAnywhere
  (`zip stats.zip stats.db`) and download it from the Files tab.
- **Space**: the Files tab shows how much of the 512 MB you're using. Two people's histories fit comfortably;
  several more long histories would need the database slimmed down or the paid plan.
