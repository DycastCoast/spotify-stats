"""
Smooth lines for the site's charts (instead of rows of bars).

The curve is a *monotone* cubic (Fritsch–Carlson): it bends smoothly through every point
but never overshoots between two of them. An ordinary smooth curve can swing below zero
or above a peak that never happened; this one can't, so the line stays honest.

Used from templates as filters (registered in app.py):
    {% set L = c.bars | smooth_bars(c.bottom) %}          bars -> line through their tops
    {% set L = d.bars | smooth_bars(d.bottom, "sy", "sh") %}   ...or through another set of tops
Each gives {"line": path, "area": path, "last": (x, y), "peak": (x, y)}.
"""


def _get(b, key):
    return b[key] if isinstance(b, dict) else getattr(b, key)


def smooth_path(points):
    """SVG path through (x, y) points (x increasing), smooth but never overshooting."""
    pts = [(float(x), float(y)) for x, y in points]
    n = len(pts)
    if n == 0:
        return ""
    if n == 1:
        return f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"
    if n == 2:
        return f"M{pts[0][0]:.1f},{pts[0][1]:.1f} L{pts[1][0]:.1f},{pts[1][1]:.1f}"
    dx = [pts[i + 1][0] - pts[i][0] or 1e-9 for i in range(n - 1)]
    slope = [(pts[i + 1][1] - pts[i][1]) / dx[i] for i in range(n - 1)]
    # tangent at each point: 0 at a peak/valley (so it doesn't overshoot), else a weighted mean
    m = [slope[0]] + [0.0] * (n - 2) + [slope[-1]]
    for i in range(1, n - 1):
        if slope[i - 1] * slope[i] <= 0:
            m[i] = 0.0
        else:
            w1, w2 = 2 * dx[i] + dx[i - 1], dx[i] + 2 * dx[i - 1]
            m[i] = (w1 + w2) / (w1 / slope[i - 1] + w2 / slope[i])
    d = [f"M{pts[0][0]:.1f},{pts[0][1]:.1f}"]
    for i in range(n - 1):
        (x0, y0), (x1, y1) = pts[i], pts[i + 1]
        h = dx[i] / 3
        d.append(f"C{x0 + h:.1f},{y0 + m[i] * h:.1f} {x1 - h:.1f},{y1 - m[i + 1] * h:.1f} {x1:.1f},{y1:.1f}")
    return " ".join(d)


def smooth_points(points, bottom):
    """Line + filled area under it, plus the last point and the highest one (for dots/labels)."""
    if not points:
        return {"line": "", "area": "", "last": None, "peak": None}
    line = smooth_path(points)
    area = f"{line} L{points[-1][0]:.1f},{bottom:.1f} L{points[0][0]:.1f},{bottom:.1f} Z"
    peak = min(points, key=lambda p: p[1])            # smallest y = highest on screen
    return {"line": line, "area": area, "last": points[-1], "peak": peak}


def smooth_bars(bars, bottom, y_key="y", h_key="h"):
    """Turn a bar chart's bars into a smooth line through the middle of each bar's top."""
    points = []
    for b in bars:
        x = _get(b, "x") + _get(b, "w") / 2
        y = _get(b, y_key) if _get(b, h_key) else bottom
        points.append((x, y))
    return smooth_points(points, bottom)


def hour_lines(profile, w=960, h=220, top=14, left=14, right=14, bottom_pad=26):
    """The Right now page's 24 hours: last 30 days and the year before, as two smooth lines.
    profile items have hour, h_now and h_usual (0-100, already scaled to the busiest hour)."""
    bottom = h - bottom_pad
    span = w - left - right
    x = lambda hour: left + span * hour / 23
    y = lambda pct: bottom - (bottom - top) * pct / 100
    now = [(x(p["hour"]), y(p["h_now"])) for p in profile]
    usual = [(x(p["hour"]), y(p["h_usual"])) for p in profile]
    cols = [{"x": round(x(p["hour"]) - span / 46, 1), "w": round(span / 23, 1), "hour": p["hour"]} for p in profile]
    return {"w": w, "h": h, "bottom": bottom, "top": top, "now": smooth_points(now, bottom),
            "usual": smooth_points(usual, bottom), "cols": cols,
            "ticks": [(f"{hr:02d}", round(x(hr), 1)) for hr in (0, 3, 6, 9, 12, 15, 18, 21)]}
