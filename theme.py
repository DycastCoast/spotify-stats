"""
Pick a page colour from an album cover.

The hard part isn't finding a colour, it's finding a USABLE one: covers are
often mostly black, white, grey or muddy brown, and the colour also has to be
readable as text on the dark background. So:
  1. shrink the image and reduce it to its 8 main colours,
  2. score each one on "how much of the cover" x "how colourful",
     ignoring near-black and near-white,
  3. take the best hue, and adjust its lightness until it passes a contrast
     check against the page background,
  4. if the cover has no real colour at all (black and white photo), give up
     and keep the site's default blue.
"""
import colorsys
import io

import requests
from PIL import Image

BACKGROUND = (0x0F, 0x10, 0x12)     # the site's --bg colour
MIN_CONTRAST = 4.5                   # WCAG AA for normal-sized text

HUE_NAMES = [  # (upper hue in degrees, name)
    (15, "red"), (40, "orange"), (55, "amber"), (70, "yellow"), (95, "lime"),
    (160, "green"), (185, "teal"), (200, "cyan"), (240, "blue"), (265, "indigo"),
    (290, "purple"), (330, "pink"), (360, "red"),
]


def luminance(rgb):
    """How bright a colour looks to the eye (WCAG's formula)."""
    def channel(c):
        c = c / 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    """Contrast ratio between two colours: 1 (identical) up to 21 (black on white)."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def to_rgb(h, l, s):
    return tuple(round(c * 255) for c in colorsys.hls_to_rgb(h, l, s))


def readable(h, s):
    """The lightest-needed version of this hue that reads well on the background."""
    s = min(0.85, max(s, 0.5))       # boost dull colours a bit, tame neon ones
    l = 0.55
    while l < 0.85 and contrast(to_rgb(h, l, s), BACKGROUND) < MIN_CONTRAST:
        l += 0.02
    return to_rgb(h, l, s)


def name_of(h):
    degrees = h * 360
    return next(name for limit, name in HUE_NAMES if degrees < limit)


def from_image_bytes(data):
    """Return {"hex", "rgb", "name"} for an image, or None if it has no usable colour."""
    img = Image.open(io.BytesIO(data)).convert("RGB").resize((48, 48))
    small = img.quantize(colors=8, method=Image.Quantize.MEDIANCUT)
    palette = small.getpalette()
    best = None
    for count, index in small.getcolors():
        r, g, b = palette[index * 3: index * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        if l < 0.12 or l > 0.92 or s < 0.18:       # near black, near white, or grey
            continue
        score = count * (0.25 + s)                  # common AND colourful
        if best is None or score > best[0]:
            best = (score, h, s)
    if best is None:
        return None
    _, h, s = best
    rgb = readable(h, s)
    return {"hex": "#%02x%02x%02x" % rgb, "rgb": "%d, %d, %d" % rgb, "name": name_of(h)}


def from_url(url):
    """Download a cover (Spotify's image server only) and pick its colour."""
    if not url.startswith("https://i.scdn.co/"):
        return None                                 # only fetch images from Spotify
    response = requests.get(url, timeout=10)
    response.raise_for_status()
    return from_image_bytes(response.content)
