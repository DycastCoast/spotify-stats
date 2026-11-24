"""Move every play from one user to another, then remove the first user.

Use when two "people" in the database turn out to be the same person, e.g. an
export imported under the wrong account.

Usage:  python merge_users.py FROM_ID TO_ID

Back up stats.db first (just copy the file). This can't be undone from inside
the app, though re-importing the original export under FROM_ID would restore it.
"""
import sys

from db import connect

src, dst = sys.argv[1], sys.argv[2]
if src == dst:
    sys.exit("FROM and TO are the same user.")

with connect() as conn:
    for uid in (src, dst):
        if not conn.execute("SELECT 1 FROM users WHERE id = ?", (uid,)).fetchone():
            sys.exit(f"No user called {uid}")

    before = conn.execute("SELECT COUNT(*) FROM plays WHERE user_id = ?", (src,)).fetchone()[0]
    # UPDATE OR IGNORE: if the destination already has an identical play (same
    # source, track and time), leave that one alone instead of crashing on the
    # UNIQUE rule. Any such leftovers are true duplicates and get deleted below.
    moved = conn.execute("UPDATE OR IGNORE plays SET user_id = ? WHERE user_id = ?", (dst, src)).rowcount
    duplicates = conn.execute("DELETE FROM plays WHERE user_id = ?", (src,)).rowcount
    conn.execute("DELETE FROM users WHERE id = ?", (src,))
    conn.execute("UPDATE users SET data_version = data_version + 1 WHERE id = ?", (dst,))   # pages redo their sums
    # All in one transaction: if anything above failed, none of it is saved.

print(f"Moved {moved:,} of {before:,} plays from {src} to {dst}"
      + (f" ({duplicates:,} were already there and were dropped)" if duplicates else ""))
print(f"Removed user {src}")
