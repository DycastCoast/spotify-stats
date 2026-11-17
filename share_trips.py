"""Turn "trips together" on or off for someone who can't log in to do it themselves yet.

ONLY run this for someone else after they've told you they agree: it lets their
friends see the trips they were on at the same time, in the same country.
Everyone can also switch it on or off themselves at the bottom of their Trips page.

Usage:  python share_trips.py USER_ID on
        python share_trips.py USER_ID off
        python share_trips.py            (lists everyone's setting)
"""
import sys

from db import connect, init_db, set_share_trips

init_db()   # makes sure the share_trips column exists
if len(sys.argv) == 1:
    with connect() as conn:
        for row in conn.execute("SELECT id, display_name, share_trips FROM users ORDER BY display_name"):
            print(f"{row['display_name'] or row['id']:<20} {row['id']:<32} {'on' if row['share_trips'] else 'off'}")
    sys.exit()

user, state = sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else ""
if state not in ("on", "off"):
    sys.exit("Say on or off, e.g.  python share_trips.py USER_ID on")
with connect() as conn:
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (user,)).fetchone():
        sys.exit(f"No user called {user}. Run with no arguments to list everyone.")
set_share_trips(user, state == "on")
print(f"Trips together is now {state} for {user}.")
