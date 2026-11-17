"""The "alarm clock" for the live site: asks it to fetch everyone's new plays.

The free PythonAnywhere plan can't run anything on a timer, so something else has to
nudge it. Run this every 30 minutes from Windows Task Scheduler (the same way the
poller runs now), or use the one-line curl version on the Raspberry Pi (see DEPLOY.md).

Needs two lines in your PC's .env:
    LIVE_SITE=https://yourname.pythonanywhere.com
    POLL_KEY=the same key as in the live site's .env
"""
import logging
import os
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(Path(__file__).parent / "ping.log", encoding="utf-8"), logging.StreamHandler()],
)

site, key = os.environ.get("LIVE_SITE"), os.environ.get("POLL_KEY")
if not site or not key:
    raise SystemExit("Add LIVE_SITE and POLL_KEY to .env first (see DEPLOY.md).")
try:
    r = requests.post(site.rstrip("/") + "/internal/poll", headers={"X-Poll-Key": key}, timeout=600)   # polling + working out the pages in advance can take a few minutes
    logging.info("%s %s", r.status_code, r.text.strip()[:300])
except requests.RequestException as err:      # the site or your internet is down: try again next time
    logging.warning("couldn't reach the site: %s", err)
