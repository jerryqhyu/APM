# The APM look

Three racing stripes, red, white and blue, run into an italic **APM**. Their right ends are cut
on the same slant as the letters, as on a 1970s sports logo. The stripes alone are the icon.

Everything is drawn from plain shapes, so it looks the same everywhere, with no fonts to install.
The one exception is the banner's tagline, which is real text.

## The files

| File | Use it for |
|---|---|
| [`apm-logo-on-dark.svg`](apm-logo-on-dark.svg) | The logo: stripes and white wordmark, for dark backgrounds. |
| [`apm-logo-on-light.svg`](apm-logo-on-light.svg) | Black wordmark, for white backgrounds: the white stripe is the page itself, between red and blue. |
| [`apm-logo-badge-white.svg`](apm-logo-badge-white.svg) | The light logo on a white plate, for light backgrounds that aren't white. |
| [`apm-logo-badge-navy.svg`](apm-logo-badge-navy.svg) | The logo on a navy plate, where it has to stand out. |
| [`apm-mark.svg`](apm-mark.svg) | The icon: the stripes on a rounded navy square. Avatars, anywhere square. |
| [`apm-mark-square.svg`](apm-mark-square.svg) | The icon, full bleed, for app icons the system rounds itself. |
| [`apm-favicon.svg`](apm-favicon.svg) | The favicon: the stripes larger, so they read at 16 px. Also `ui/public/favicon.svg`. |
| [`apm-wordmark-white.svg`](apm-wordmark-white.svg), [`apm-wordmark-navy.svg`](apm-wordmark-navy.svg) | The wordmark alone. |
| [`apm-banner.svg`](apm-banner.svg) | The README header. [`apm-banner.png`](apm-banner.png) is the same at 1280 × 640: upload it as the repository's social preview (Settings, General, Social preview). |

The web UI draws the logo inline in the top bar and on its start screens, from the shapes in
`ui/src/brand.ts`, so it follows the theme: white letters in the dark theme; black letters on a
white plate in the light one. It uses `ui/public/favicon.svg`, `apple-touch-icon.png` (180 px), and `icon-192.png`
and `icon-512.png` (in `site.webmanifest`, so the UI can be installed as an app).

## Colours

| | Hex |
|---|---|
| Red | `#e23b2e` |
| White | `#f5f1e8` |
| Blue | `#2763d1` |
| Navy | `#13203d` (the icon's square, the banner, the navy plate) |
| Black | `#111318` (the wordmark on light) |

## A few rules

- Red on top, then white, then blue. Keep the stripes' slant the letters' slant.
- On a light background, use the black wordmark: on white as it is, on another light colour on its white plate.
- Leave space around the logo at least as tall as one stripe.
- Below about 90 px wide, use the icon: the letters' counters close up.

## Changing it

The shapes and colours live in [`build.py`](build.py). It writes every SVG here, the favicon,
and `ui/src/brand.ts`. Then render the PNGs (this needs Playwright with Chromium):

```bash
python docs/brand/build.py
```

```bash
node docs/brand/render-pngs.cjs
```
