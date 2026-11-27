"""Writes APM's logo, icons and banner as SVG, from plain shapes: no fonts in the marks, so they
look the same everywhere. Run it after changing a shape or a colour:

    python docs/brand/build.py

It writes the files next to it, the favicon into ui/public/ and the mark into ui/src/assets/. The
PNGs (app icons, the social preview) are renders of these: `node docs/brand/render-pngs.cjs`.

The idea: a schedule. Bars that start later as they go down, in the colours of the states work
moves through in APM (done, review, building, ready, waiting), and a blocky 1970s wordmark whose
feet are cut into stripes that widen as they fall.
"""

from pathlib import Path

HERE = Path(__file__).resolve().parent
PUBLIC = HERE.parent.parent / "ui" / "public"
UI_ASSETS = HERE.parent.parent / "ui" / "src" / "assets"

INK = "#17181c"
CREAM = "#f1eadb"
STATES = ["#8c6cf2", "#f26b2e", "#2f8fe8", "#29b46e", "#f2b33d"]
"""Done, review, building, ready, waiting: the order work moves through, top to bottom."""

# --- the wordmark: cap height 200, letters built from paths ---

H = 200
STEM = 58  # vertical strokes
BAR = 50  # horizontal strokes
GAPS = [(116, 4), (134, 6), (154, 8), (176, 10)]
"""The stripes cut through the letters' feet: (top, height), widening towards the baseline."""
TRACK = 24


def _a(x: float) -> tuple[str, float]:
    w, r = 196, 74
    outer = (
        f"M{x},{H} V{r} A{r},{r} 0 0 1 {x + r},0 H{x + w - r} A{r},{r} 0 0 1 {x + w},{r} V{H}"
        f" H{x + w - STEM} V{GAPS[1][0]} H{x + STEM} V{H} Z"
    )
    counter = f"M{x + STEM},{BAR} H{x + w - STEM} V96 H{x + STEM} Z"
    return f"{outer} {counter}", w


def _p(x: float) -> tuple[str, float]:
    w, r, bowl = 186, 72, 132
    outer = (
        f"M{x},{H} V0 H{x + w - r} A{r},{r} 0 0 1 {x + w},{r} V{bowl - r}"
        f" A{r},{r} 0 0 1 {x + w - r},{bowl} H{x + STEM} V{H} Z"
    )
    counter = f"M{x + STEM},{BAR} H{x + w - STEM - 6} V{bowl - BAR + 4} H{x + STEM} Z"
    return f"{outer} {counter}", w


def _m(x: float) -> tuple[str, float]:
    w, notch = 256, 84
    gap = (w - 3 * STEM) / 2
    shoulder = notch + 34
    inner = shoulder + gap * 0.62
    d = (
        f"M{x},{H} V0 H{x + STEM + gap / 2} L{x + w / 2},{notch}"
        f" L{x + w - STEM - gap / 2},0 H{x + w} V{H} H{x + w - STEM} V{shoulder}"
        f" L{x + w / 2 + STEM / 2},{inner} V{H}"
        f" H{x + w / 2 - STEM / 2} V{inner} L{x + STEM},{shoulder} V{H} Z"
    )
    return d, w


def wordmark_path() -> tuple[str, float]:
    parts, x = [], 0.0
    for letter in (_a, _p, _m):
        d, w = letter(x)
        parts.append(d)
        x += w + TRACK
    return " ".join(parts), x - TRACK


WORD_D, WORD_W = wordmark_path()


def wordmark(color: str, ident: str, *, striped: bool = True) -> str:
    """The letters at the origin, `WORD_W` wide and `H` tall. Striped feet are cut out through a
    mask, so whatever is behind shows through.
    """
    if not striped:
        return f'<path fill="{color}" fill-rule="evenodd" d="{WORD_D}"/>'
    cuts = "".join(
        f'<rect x="-1" y="{y}" width="{WORD_W + 2}" height="{h}" fill="#000"/>' for y, h in GAPS
    )
    box = f'x="-1" y="-1" width="{WORD_W + 2}" height="{H + 2}"'
    return (
        f'<defs><mask id="{ident}" maskUnits="userSpaceOnUse" {box}>'
        f'<rect {box} fill="#fff"/>{cuts}</mask></defs>'
        f'<path fill="{color}" fill-rule="evenodd" mask="url(#{ident})" d="{WORD_D}"/>'
    )


# --- the mark: a schedule of five bars ---


def cascade(x: float, y: float, w: float, h: float, *, rows: int = 5, length: float = 0.56) -> str:
    """Bars that start later as they go down, in the states' colours, filling the box."""
    thick = h / (rows * 1.3)
    gap = (h - rows * thick) / (rows - 1)
    long = w * length
    shift = (w - long) / (rows - 1)
    return "".join(
        f'<rect x="{x + i * shift:.2f}" y="{y + i * (thick + gap):.2f}" width="{long:.2f}"'
        f' height="{thick:.2f}" fill="{STATES[i]}"/>'
        for i in range(rows)
    )


def tile(size: float, *, square: bool = False) -> str:
    """The mark: the schedule on a rounded ink square. `square` is for app icons, which the
    system rounds or crops itself: full bleed, with the bars inside the safe zone.
    """
    pad = size * (0.24 if square else 0.18)
    inner = size - 2 * pad
    corner = 0 if square else size * 0.22
    return f'<rect width="{size}" height="{size}" rx="{corner:.2f}" fill="{INK}"/>' + cascade(
        pad, pad, inner, inner
    )


def favicon() -> str:
    """Three bars on 32 px: five turn to mush at 16. Done, building, ready."""
    bars = "".join(
        f'<rect x="{6 + i * 5}" y="{7 + i * 7}" width="15" height="5" fill="{c}"/>'
        for i, c in enumerate([STATES[0], STATES[2], STATES[3]])
    )
    return f'<rect width="32" height="32" rx="7" fill="{INK}"/>{bars}'


# --- compositions ---


def svg(w: float, h: float, body: str, title: str) -> str:
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w:g} {h:g}" width="{w:g}"'
        f' height="{h:g}" role="img" aria-label="{title}"><title>{title}</title>{body}</svg>\n'
    )


def lockup(word: str, ident: str) -> tuple[str, float, float]:
    """The mark beside the wordmark, the mark as tall as the letters with their stripes."""
    size = H * 1.24
    scale = 1.0
    gap = 56
    body = (
        f"<g>{tile(size)}</g>"
        f'<g transform="translate({size + gap},{(size - H) / 2:.2f}) scale({scale})">'
        f"{wordmark(word, ident)}</g>"
    )
    return body, size + gap + WORD_W, size


def stacked() -> str:
    """A poster: the schedule wide across the top, the wordmark under it."""
    w = h = 1080
    margin = 120
    word_scale = (w - 2 * margin) / WORD_W
    word_h = H * word_scale
    stripes = cascade(margin, 150, w - 2 * margin, 430, length=0.62)
    word = (
        f'<g transform="translate({margin},{h - margin - word_h:.2f}) scale({word_scale:.4f})">'
        f"{wordmark(CREAM, 'stacked')}</g>"
    )
    return svg(w, h, f'<rect width="{w}" height="{h}" fill="{INK}"/>{stripes}{word}', "APM")


def banner() -> str:
    """The README and social preview: wordmark and line on the left, the schedule running off the
    right edge.
    """
    w, h = 1280, 640
    left, word_scale = 96, 0.9
    word_right = left + WORD_W * word_scale
    bars = "".join(
        f'<rect x="{word_right + 90 + i * 44:.0f}" y="{150 + i * 72}" width="{w}" height="50"'
        f' fill="{c}"/>'
        for i, c in enumerate(STATES)
    )
    word = (
        f'<g transform="translate({left},150) scale({word_scale})">{wordmark(CREAM, "banner")}</g>'
    )
    font = "Futura, 'Avenir Next', 'Helvetica Neue', Arial, sans-serif"
    line = "".join(
        f'<text x="{left}" y="{y}" fill="{CREAM}" fill-opacity="0.78" font-family="{font}"'
        f' font-size="34" font-weight="600" letter-spacing="0.5">{text}</text>'
        for y, text in ((420, "The project manager for building"), (464, "with AI agents."))
    )
    return svg(w, h, f'<rect width="{w}" height="{h}" fill="{INK}"/>{bars}{word}{line}', "APM")


def write() -> list[Path]:
    files = {
        HERE / "apm-mark.svg": svg(512, 512, tile(512), "APM"),
        HERE / "apm-mark-square.svg": svg(512, 512, tile(512, square=True), "APM"),
        UI_ASSETS / "apm-mark.svg": svg(512, 512, tile(512), "APM"),
        HERE / "apm-favicon.svg": svg(32, 32, favicon(), "APM"),
        HERE / "apm-wordmark-cream.svg": svg(WORD_W, H, wordmark(CREAM, "wm"), "APM"),
        HERE / "apm-wordmark-ink.svg": svg(WORD_W, H, wordmark(INK, "wm"), "APM"),
        HERE / "apm-logo-stacked.svg": stacked(),
        HERE / "apm-banner.svg": banner(),
        PUBLIC / "favicon.svg": svg(32, 32, favicon(), "APM"),
    }
    for name, word in (("apm-logo-on-dark.svg", CREAM), ("apm-logo-on-light.svg", INK)):
        body, lw, lh = lockup(word, "logo")
        files[HERE / name] = svg(lw, lh, body, "APM")
    for path, text in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return list(files)


if __name__ == "__main__":
    for path in write():
        print(path.relative_to(HERE.parent.parent))
