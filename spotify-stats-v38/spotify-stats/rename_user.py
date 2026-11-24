"""Change a user's ID everywhere, e.g. after importing with the wrong --user value.

Usage:  python rename_user.py OLD_ID NEW_ID
"""
import sys

from db import connect

old, new = sys.argv[1], sys.argv[2]

with connect() as conn:
    if not conn.execute("SELECT 1 FROM users WHERE id = ?", (old,)).fetchone():
        sys.exit(f"No user called {old}")
    if conn.execute("SELECT 1 FROM users WHERE id = ?", (new,)).fetchone():
        sys.exit(f"{new} already exists")
    # Plays point at the users table, so the database won't let us simply change
    # the ID in place. Instead: copy the user under the new ID, move the plays
    # across, then delete the old row. All in one transaction.
    conn.execute(
        "INSERT INTO users (id, display_name, token_info, created_at, share_trips) "
        "SELECT ?, display_name, token_info, created_at, share_trips FROM users WHERE id = ?",
        (new, old),
    )
    moved = conn.execute("UPDATE plays SET user_id = ? WHERE user_id = ?", (new, old)).rowcount
    conn.execute("DELETE FROM users WHERE id = ?", (old,))

print(f"Renamed {old} -> {new}: {moved:,} plays moved")
