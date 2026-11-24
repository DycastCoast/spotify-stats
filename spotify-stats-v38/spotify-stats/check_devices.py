"""List the devices recorded in the imported export, with how much each was used and when.

Usage:  python check_devices.py <spotify-user-id>
"""
import sys

from db import connect

if len(sys.argv) != 2:
    sys.exit("Usage: python check_devices.py <spotify-user-id>")
ETHAN = sys.argv[1]

with connect() as conn:
    rows = conn.execute(
        """
        SELECT platform, COUNT(*) AS plays,
               MIN(played_at) AS first, MAX(played_at) AS last
        FROM plays
        WHERE user_id = ?
        GROUP BY platform
        ORDER BY first
        """,
        (ETHAN,),
    ).fetchall()

print(f"{'plays':>8}  {'first used':10}    {'last used':10}  device")
for r in rows:
    print(f"{r['plays']:>8,}  {r['first'][:10]} -> {r['last'][:10]}  {r['platform']}")
