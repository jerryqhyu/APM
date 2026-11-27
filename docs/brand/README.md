# The APM look

APM's logo is a schedule: five bars that start later as they go down, like work in a plan. They
are in the colours of the states work moves through in APM, top to bottom: done, review,
building, ready, waiting. The wordmark is blocky 1970s lettering whose feet are cut into stripes
that widen as they fall.

Everything is drawn from plain shapes, so it looks the same everywhere, with no fonts to install.
The one exception is the banner's tagline, which is real text.

## The files

| File | Use it for |
|---|---|
| [`apm-mark.svg`](apm-mark.svg) | The mark: the schedule on a rounded ink square. Avatars, the app, anywhere square. |
| [`apm-mark-square.svg`](apm-mark-square.svg) | The mark, full bleed, for app icons the system rounds itself. |
| [`apm-favicon.svg`](apm-favicon.svg) | The favicon: three bars, which still read at 16 px. Also `ui/public/favicon.svg`. |
| [`apm-logo-on-dark.svg`](apm-logo-on-dark.svg) | Mark and wordmark side by side, for dark backgrounds. |
| [`apm-logo-on-light.svg`](apm-logo-on-light.svg) | The same, for light backgrounds. |
| [`apm-logo-stacked.svg`](apm-logo-stacked.svg) | A square poster: the schedule wide over the wordmark. |
| [`apm-wordmark-cream.svg`](apm-wordmark-cream.svg), [`apm-wordmark-ink.svg`](apm-wordmark-ink.svg) | The wordmark alone. |
| [`apm-banner.svg`](apm-banner.svg) | The README header. [`apm-banner.png`](apm-banner.png) is the same at 1280 × 640: upload it as the repository's social preview (Settings, General, Social preview). |

The web UI uses `ui/public/favicon.svg`, `apple-touch-icon.png` (180 px), `icon-192.png` and
`icon-512.png` (in `site.webmanifest`, so the UI can be installed as an app), and
`ui/src/assets/apm-mark.svg` in the top bar.

## Colours

| | Hex | |
|---|---|---|
| Ink | `#17181c` | backgrounds, the mark's square |
| Cream | `#f1eadb` | the wordmark on ink |
| Done | `#8c6cf2` | first bar |
| Review | `#f26b2e` | second |
| Building | `#2f8fe8` | third |
| Ready | `#29b46e` | fourth |
| Waiting | `#f2b33d` | fifth |

## A few rules

- Keep the bars in that order and their colours as they are: they are APM's states.
- Leave space around the logo at least as wide as one bar is tall.
- Below about 120 px wide, use the mark alone: the wordmark's stripes close up.
- Don't put the wordmark on the state colours, or set it in another typeface.

## Changing it

The shapes and colours live in [`build.py`](build.py). Run it, then render the PNGs (this needs
Playwright with Chromium):

```bash
python docs/brand/build.py
```

```bash
node docs/brand/render-pngs.cjs
```
