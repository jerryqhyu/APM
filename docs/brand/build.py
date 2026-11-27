"""Writes APM's logo, icons and banner as SVG, from plain shapes: no fonts in the marks, so they
look the same everywhere. Run it after changing a shape or a colour:

    python docs/brand/build.py

It writes the files next to it, the favicon into ui/public/, and the shapes the UI draws its logo
from into ui/src/brand.ts. The PNGs (app icons, the social preview) are renders of these:
`node docs/brand/render-pngs.cjs`.

The idea: three racing stripes, red, white and blue, whose ends are cut on the slant of an italic
APM beside them, as on a 1970s sports logo. The stripes alone are the icon.
"""

import json
import math
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
PUBLIC = ROOT / "ui" / "public"

NAVY = "#13203d"
WHITE = "#f5f1e8"
RED = "#e23b2e"
BLUE = "#2763d1"
STRIPES = (RED, WHITE, BLUE)

H = 100  # cap height: the stripes fill it too
SLANT = math.tan(math.radians(14))  # the italic: x leans this far per unit of height
STEM = 24
STRIPE, STRIPE_GAP = 28, 8  # three stripes and two gaps make the cap height
A_WIDTH, A_LEG = 92, 30
LEG_SLOPE = (A_WIDTH - STEM) / H + SLANT
"""How far the A's left leg leans per unit of height, italic included. The stripes' ends are cut
on the same line, so the gap between them and the A is even all the way up."""
STRIPE_LENGTH = 84  # the bottom edge of the bottom stripe; the rest reach further on the leg's line
GAP = STRIPE_GAP / math.cos(math.atan(LEG_SLOPE))
"""Across the stripes' ends to the A: as wide, measured square to the cut, as between stripes."""


def _italic(points: list[tuple[float, float]], x0: float) -> list[tuple[float, float]]:
    """Upright points → italic, leaning right by `SLANT` per unit above the baseline."""
    return [(x + (H - y) * SLANT + x0, y) for x, y in points]


def _poly(points: Sequence[tuple[float, float]]) -> str:
    return "M" + " L".join(f"{x:.2f},{y:.2f}" for x, y in points) + " Z"


def _a(x0: float) -> tuple[str, float]:
    w, leg = A_WIDTH, A_LEG
    top = w - STEM

    def inner(y: float) -> float:  # the leg's inner edge, `leg` right of its outer edge
        return top * (1 - y / H) + leg

    outer = [(0, H), (top, 0), (w, 0), (w, H), (top, H), (top, 84), (inner(84), 84), (inner(H), H)]
    counter = [(inner(64), 64), (top, 64), (top, H * leg / top)]
    return f"{_poly(_italic(outer, x0))} {_poly(_italic(counter, x0))}", w


def _arc(cx: float, cy: float, r: float) -> list[tuple[float, float]]:
    """The right half of a circle, top to bottom."""
    return [
        (cx + r * math.sin(t), cy - r * math.cos(t)) for t in (i * math.pi / 16 for i in range(17))
    ]


def _p(x0: float) -> tuple[str, float]:
    w, bowl, bar = 86, 62, 20
    r = bowl / 2
    outer = [(0, H), (0, 0), *_arc(w - r, r, r), (STEM, bowl), (STEM, H)]
    counter = [(STEM, bar), *_arc(w - r, r, r - bar), (STEM, bowl - bar)]
    return f"{_poly(_italic(outer, x0))} {_poly(_italic(counter, x0))}", w


def _m(x0: float) -> tuple[str, float]:
    w, v = 118, 30
    mid = w / 2
    outer = [
        (0, H), (0, 0), (STEM + 6, 0), (mid, 56), (w - STEM - 6, 0), (w, 0), (w, H),
        (w - STEM, H), (w - STEM, 42), (mid + v / 2 - 4, H), (mid - v / 2 + 4, H),
        (STEM, 42), (STEM, H),
    ]  # fmt: skip
    return _poly(_italic(outer, x0)), w


def stripes(x0: float = 0, length: float = STRIPE_LENGTH) -> list[tuple[str, str]]:
    """The three stripes, filling the cap height, their right ends cut parallel to the A's left
    leg: (path, colour) each.
    """
    out = []
    for i, colour in enumerate(STRIPES):
        top = i * (STRIPE + STRIPE_GAP)
        bottom = top + STRIPE
        right = x0 + length
        pts = [
            (x0, top),
            (right + (H - top) * LEG_SLOPE, top),
            (right + (H - bottom) * LEG_SLOPE, bottom),
            (x0, bottom),
        ]
        out.append((_poly(pts), colour))
    return out


def wordmark(x0: float) -> tuple[str, float]:
    """The italic APM from `x0`: its path, and where it ends (including the lean)."""
    parts, x = [], x0
    for letter, space in ((_a, 6), (_p, 10), (_m, 0)):
        d, w = letter(x)
        parts.append(d)
        x += w + space
    return " ".join(parts), x + H * SLANT


WORD_X = STRIPE_LENGTH + GAP
WORD_D, LOGO_W = wordmark(WORD_X)
WORD_ONLY_D, WORD_ONLY_W = wordmark(0)


def svg(w: float, h: float, body: str, *, x: float = 0, y: float = 0) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x:g} {y:g} {w:g} {h:g}"'
        f' width="{w:g}" height="{h:g}" role="img" aria-label="APM">'
        f"<title>APM</title>{body}</svg>\n"
    )


def _stripes_svg(x0: float = 0, length: float = STRIPE_LENGTH) -> str:
    return "".join(f'<path d="{d}" fill="{c}"/>' for d, c in stripes(x0, length))


def logo(word: str) -> str:
    """Stripes and wordmark, `LOGO_W` wide and `H` tall, on nothing."""
    return f'{_stripes_svg()}<path fill="{word}" fill-rule="evenodd" d="{WORD_D}"/>'


def badge() -> str:
    """The logo on a navy plate: for light backgrounds, where the white stripe would vanish."""
    pad = 26
    w, h = LOGO_W + 2 * pad, H + 2 * pad
    plate = f'<rect width="{w:.2f}" height="{h}" rx="{pad * 0.9:.1f}" fill="{NAVY}"/>'
    return svg(w, h, f'{plate}<g transform="translate({pad},{pad})">{logo(WHITE)}</g>')


ICON_LENGTH = 70
"""The stripes alone, as the icon: a little shorter than in the logo, so they fill a square."""


def mark(size: float, *, square: bool = False) -> str:
    """The stripes alone on a navy square: the icon. `square` is full bleed, for app icons the
    system rounds or crops itself, with the stripes inside the safe zone.
    """
    inset = 0.24 if square else 0.17
    span = size * (1 - 2 * inset)  # the stripes' width, slant included
    scale = span / (ICON_LENGTH + H * LEG_SLOPE)
    x = (size - span) / 2
    y = (size - H * scale) / 2
    corner = 0 if square else size * 0.22
    stripes_body = _stripes_svg(0, ICON_LENGTH)
    return (
        f'<rect width="{size}" height="{size}" rx="{corner:.2f}" fill="{NAVY}"/>'
        f'<g transform="translate({x:.2f},{y:.2f}) scale({scale:.4f})">{stripes_body}</g>'
    )


def favicon() -> str:
    """The icon at 32: the stripes larger in the square, so they still read at 16."""
    scale = 24 / (ICON_LENGTH + H * LEG_SLOPE)
    body = _stripes_svg(0, ICON_LENGTH)
    return (
        f'<rect width="32" height="32" rx="7" fill="{NAVY}"/>'
        f'<g transform="translate(4,{(32 - H * scale) / 2:.2f}) scale({scale:.4f})">{body}</g>'
    )


def banner() -> str:
    """The README and social preview: navy, the stripes running in from the left edge into the
    wordmark, and the line under it.
    """
    w, h = 1280, 640
    scale = 2.1
    x, y = (w - LOGO_W * 2.1) / 2 + 40, 150
    bars = _stripes_svg(-x / scale, STRIPE_LENGTH + x / scale)
    word = f'<path fill="{WHITE}" fill-rule="evenodd" d="{WORD_D}"/>'
    font = "Futura, 'Avenir Next', 'Helvetica Neue', Arial, sans-serif"
    line = "".join(
        f'<text x="{x + WORD_X * scale:.0f}" y="{ty}" fill="{WHITE}" fill-opacity="0.82"'
        f' font-family="{font}"'
        f' font-size="36" font-style="italic" font-weight="700" letter-spacing="0.5">'
        f"{text}</text>"
        for ty, text in ((430, "The project manager for building"), (476, "with AI agents."))
    )
    art = f'<g transform="translate({x},{y}) scale({scale})">{bars}{word}</g>'
    return svg(w, h, f'<rect width="{w}" height="{h}" fill="{NAVY}"/>{art}{line}')


def ui_shapes() -> str:
    """The logo's shapes for the UI, which draws it inline so it can follow the theme."""
    data = {
        "width": round(LOGO_W, 2),
        "height": H,
        "stripes": [{"d": d, "fill": c} for d, c in stripes()],
        "word": WORD_D,
        "white": WHITE,
        "navy": NAVY,
    }
    return (
        "// Written by docs/brand/build.py: change the logo there, not here.\n\n"
        f"export const LOGO = {json.dumps(data, indent=2)} as const;\n"
    )


def write() -> list[Path]:
    files = {
        HERE / "apm-mark.svg": svg(512, 512, mark(512)),
        HERE / "apm-mark-square.svg": svg(512, 512, mark(512, square=True)),
        HERE / "apm-favicon.svg": svg(32, 32, favicon()),
        HERE / "apm-logo-on-dark.svg": svg(LOGO_W, H, logo(WHITE)),
        HERE / "apm-logo-on-light.svg": badge(),
        HERE / "apm-wordmark-white.svg": svg(
            WORD_ONLY_W, H, f'<path fill="{WHITE}" fill-rule="evenodd" d="{WORD_ONLY_D}"/>'
        ),
        HERE / "apm-wordmark-navy.svg": svg(
            WORD_ONLY_W, H, f'<path fill="{NAVY}" fill-rule="evenodd" d="{WORD_ONLY_D}"/>'
        ),
        HERE / "apm-banner.svg": banner(),
        PUBLIC / "favicon.svg": svg(32, 32, favicon()),
        ROOT / "ui" / "src" / "brand.ts": ui_shapes(),
    }
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)


if __name__ == "__main__":
    for path in write():
        print(path.relative_to(ROOT))
