"""
The world map for the Trips page, drawn here on the server from static/geo/countries-50m.json
(Natural Earth country outlines, via the world-atlas package: public domain).

- Projection: Natural Earth I (a flat world map that still looks like a globe's proportions).
- Detail: points closer together than a fraction of a pixel are dropped (Douglas–Peucker), so
  the whole map is a few hundred KB, sharp enough to zoom into Europe.
- Built once per process and kept in memory; the browser caches /map/world.svg for a week.
- pin(code): where a country's pin goes: the middle of its biggest piece of land, so France's
  pin is in France, not between France and French Guiana.

Trips store Spotify's two-letter country codes (GB, FR...); the map file uses ISO numeric codes.
ISO_NUM links them (each entry checked against the country names in the file).
"""
import json
import math
from functools import lru_cache
from pathlib import Path

WIDTH = 1000
DETAIL_PX = 0.18        # drop points that move the outline less than this (at 1× zoom)
SOURCE = Path(__file__).parent / "static" / "geo" / "countries-50m.json"

ISO_NUM = {"GB": "826", "US": "840", "FR": "250", "DE": "276", "ES": "724", "IT": "380", "PT": "620", "NL": "528",
           "BE": "056", "IE": "372", "CH": "756", "AT": "040", "PL": "616", "CZ": "203", "HU": "348", "HR": "191",
           "RO": "642", "BG": "100", "DK": "208", "SE": "752", "NO": "578", "FI": "246", "IS": "352", "GR": "300",
           "TR": "792", "CN": "156", "HK": "344", "MO": "446", "JP": "392", "KR": "410", "TW": "158", "TH": "764",
           "VN": "704", "ID": "360", "MY": "458", "SG": "702", "PH": "608", "IN": "356", "AU": "036", "NZ": "554",
           "CA": "124", "MX": "484", "BR": "076", "AR": "032", "AE": "784", "QA": "634", "SA": "682", "IL": "376",
           "JO": "400", "EG": "818", "MA": "504", "TN": "788", "ZA": "710", "CV": "132", "CY": "196", "MT": "470",
           "SI": "705", "SK": "703", "ME": "499", "AL": "008", "LU": "442", "EE": "233", "LV": "428", "LT": "440",
           "RS": "688", "BA": "070", "MK": "807", "UA": "804", "BY": "112", "RU": "643", "MD": "498", "GE": "268",
           "AM": "051", "AZ": "031", "KZ": "398", "CL": "152", "CO": "170", "PE": "604", "CU": "192", "DO": "214",
           "JM": "388", "KE": "404", "NG": "566", "GH": "288", "LK": "144", "NP": "524", "MV": "462", "KH": "116",
           "LA": "418", "MM": "104", "PK": "586", "BD": "050", "OM": "512", "KW": "414", "BH": "048", "LB": "422",
           "AD": "020", "MC": "492", "LI": "438", "SM": "674", "VA": "336", "EC": "218", "UY": "858", "PY": "600",
           "BO": "068", "VE": "862", "CR": "188", "PA": "591", "GT": "320", "PR": "630", "BS": "044", "BB": "052",
           "TT": "780", "SN": "686", "TZ": "834", "UG": "800", "ET": "231", "RW": "646", "MU": "480", "SC": "690",
           "MG": "450", "NA": "516", "BW": "072", "ZW": "716", "ZM": "894", "MZ": "508", "DZ": "012", "LY": "434",
           "IQ": "368", "IR": "364", "UZ": "860", "MN": "496", "FJ": "242", "FO": "234", "GL": "304", "IM": "833",
           "JE": "832", "GG": "831"}
BY_NAME = {"XK": "Kosovo"}                        # in the file without a numeric code
EXTRA_PINS = {"GI": (-5.35, 36.14)}               # too small for the file: (longitude, latitude)


def project(lon, lat):
    """Natural Earth I projection -> unscaled x, y (y up)."""
    lam, phi = math.radians(lon), math.radians(lat)
    p2 = phi * phi
    p4 = p2 * p2
    x = lam * (0.8707 - 0.131979 * p2 + p4 * (-0.013791 + p4 * (0.003971 * p2 - 0.001529 * p4)))
    y = phi * (1.007226 + p2 * (0.015085 + p4 * (-0.044475 + 0.028874 * p2 - 0.005916 * p4)))
    return x, y


SCALE = WIDTH / (2 * project(180, 0)[0])
TOP_LAT, BOTTOM_LAT = 84, -58                     # Antarctica is left off
Y_TOP = project(0, TOP_LAT)[1] * SCALE
HEIGHT = round(Y_TOP - project(0, BOTTOM_LAT)[1] * SCALE)


def to_svg(lon, lat):
    x, y = project(lon, lat)
    return x * SCALE + WIDTH / 2, Y_TOP - y * SCALE


def simplify(points, eps):
    """Douglas–Peucker: keep only the points that matter more than eps pixels."""
    if len(points) < 4:
        return points
    keep = [False] * len(points)
    keep[0] = keep[-1] = True
    stack = [(0, len(points) - 1)]
    while stack:
        a, b = stack.pop()
        (ax, ay), (bx, by) = points[a], points[b]
        dx, dy = bx - ax, by - ay
        norm = math.hypot(dx, dy) or 1e-9
        best, idx = 0.0, None
        for i in range(a + 1, b):
            px, py = points[i]
            d = abs(dy * px - dx * py + bx * ay - by * ax) / norm
            if d > best:
                best, idx = d, i
        if idx is not None and best > eps:
            keep[idx] = True
            stack += [(a, idx), (idx, b)]
    return [p for p, k in zip(points, keep) if k]


def simplify_ring(points, eps):
    """A closed outline starts and ends on the same point, which Douglas–Peucker can't
    measure from; so split it at the point farthest from the start and simplify each half."""
    if len(points) < 5:
        return points
    x0, y0 = points[0]
    far = max(range(len(points)), key=lambda i: (points[i][0] - x0) ** 2 + (points[i][1] - y0) ** 2)
    a = simplify(points[:far + 1], eps)
    b = simplify(points[far:], eps)
    return a + b[1:]


def ring_area_centroid(pts):
    a = cx = cy = 0.0
    for (x0, y0), (x1, y1) in zip(pts, pts[1:] + pts[:1]):
        cross = x0 * y1 - x1 * y0
        a += cross
        cx += (x0 + x1) * cross
        cy += (y0 + y1) * cross
    if abs(a) < 1e-9:
        return 0.0, pts[0]
    return abs(a) / 2, (cx / (3 * a), cy / (3 * a))


@lru_cache(maxsize=1)
def world():
    """Every country as SVG path data, plus each one's pin position. Built once."""
    topo = json.loads(SOURCE.read_text(encoding="utf-8"))
    (kx, ky), (tx, ty) = topo["transform"]["scale"], topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:                       # arcs are stored as deltas from the previous point
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append((x * kx + tx, y * ky + ty))   # longitude, latitude
        arcs.append(pts)

    def ring(indexes):
        lonlat = []
        for i in indexes:
            seg = arcs[i] if i >= 0 else arcs[~i][::-1]
            lonlat.extend(seg if not lonlat else seg[1:])
        # Rings that cross the 180° line (Russia's far east, Fiji) jump from +180 to -180:
        # keep longitudes continuous instead, and let the far bit hang off the map's edge.
        out, shift, prev = [], 0.0, None
        for lon, lat in lonlat:
            if prev is not None and abs(lon + shift - prev) > 180:
                shift += 360 if prev > lon + shift else -360
            prev = lon + shift
            out.append(to_svg(max(-200, min(200, lon + shift)), lat))
        return out

    countries = {}
    for g in topo["objects"]["countries"]["geometries"]:
        if g.get("id") == "010" or g["type"] not in ("Polygon", "MultiPolygon"):
            continue                                # Antarctica, or empty
        polys = [g["arcs"]] if g["type"] == "Polygon" else g["arcs"]
        parts, best = [], (0.0, None)
        for poly in polys:
            for n, r in enumerate(poly):
                pts = ring(r)
                area, centre = ring_area_centroid(pts)
                if n == 0 and area > best[0]:
                    best = (area, centre)          # pin goes in the biggest piece of land
                pts = simplify_ring(pts, DETAIL_PX)
                if len(pts) >= 3 and (area > 0.02 or len(polys) == 1):
                    parts.append("M" + "L".join(f"{x:.1f},{y:.1f}" for x, y in pts) + "Z")
        key = g.get("id") or g["properties"]["name"]
        prev = countries.get(key)
        if prev and prev["area"] >= best[0]:
            prev["d"] += "".join(parts)            # e.g. Ashmore Is. shares Australia's code
            continue
        countries[key] = {"name": g["properties"]["name"], "d": (prev["d"] if prev else "") + "".join(parts),
                          "area": best[0], "pin": best[1]}
    return countries


def _entry(code):
    w = world()
    if code in ISO_NUM:
        return w.get(ISO_NUM[code])
    if code in BY_NAME:
        return w.get(BY_NAME[code])
    return None


def pin(code):
    """(x, y) on the map for a two-letter country code, or None if it isn't known."""
    if code in EXTRA_PINS:
        x, y = to_svg(*EXTRA_PINS[code])
        return round(x, 1), round(y, 1)
    e = _entry(code)
    return (round(e["pin"][0], 1), round(e["pin"][1], 1)) if e and e["pin"] else None


def outline(code):
    """The country's own outline (to highlight countries you've been to)."""
    e = _entry(code)
    return e["d"] if e else ""


@lru_cache(maxsize=1)
def world_svg():
    """The whole map as one SVG file (served at /map/world.svg, cached by the browser)."""
    paths = "".join(f'<path d="{c["d"]}"/>' for c in world().values())
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {WIDTH} {HEIGHT}">'
            f'<g fill="#23252b" stroke="#0f1012" stroke-width="0.35" stroke-linejoin="round">{paths}</g></svg>')
