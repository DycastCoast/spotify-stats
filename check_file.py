"""Search a Spotify export zip for a song or artist, straight from the file.

Usage:  python check_file.py data\\ethans_data.zip "song or artist name"
"""
import json
import sys
import zipfile
from collections import Counter

path, query = sys.argv[1], sys.argv[2].lower()
per_year = Counter()

with zipfile.ZipFile(path) as z:
    for name in z.namelist():
        if "Streaming_History_Audio" in name and name.endswith(".json"):
            for e in json.load(z.open(name)):
                track = (e.get("master_metadata_track_name") or "").lower()
                artist = (e.get("master_metadata_album_artist_name") or "").lower()
                if query in track or query in artist:
                    per_year[e["ts"][:4]] += 1

print(f'"{sys.argv[2]}" appears {sum(per_year.values()):,} times in {path}')
for year in sorted(per_year):
    print(f"   {year}: {per_year[year]:,}")
