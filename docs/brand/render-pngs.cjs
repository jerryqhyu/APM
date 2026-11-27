// Renders the PNGs that some places need instead of SVG: the app icons (Apple touch icon, the web
// app manifest's icons) and GitHub's social preview. Needs Playwright with Chromium:
//
//   node docs/brand/render-pngs.cjs
//
// Run docs/brand/build.py first if you changed the shapes.
const path = require("path");
const { chromium } = require("playwright");

const brand = __dirname;
const ui = path.join(brand, "..", "..", "ui", "public");
const renders = [
  ["apm-mark-square.svg", 180, 180, path.join(ui, "apple-touch-icon.png")],
  ["apm-mark-square.svg", 192, 192, path.join(ui, "icon-192.png")],
  ["apm-mark-square.svg", 512, 512, path.join(ui, "icon-512.png")],
  ["apm-banner.svg", 1280, 640, path.join(brand, "apm-banner.png")],
];

(async () => {
  const browser = await chromium.launch();
  for (const [svg, width, height, out] of renders) {
    const page = await browser.newPage({ viewport: { width, height } });
    await page.goto(`file://${path.join(brand, svg)}`);
    // Draw the SVG at the size wanted: its viewBox scales the shapes.
    await page.evaluate(([w, h]) => {
      const root = document.documentElement;
      root.setAttribute("width", String(w));
      root.setAttribute("height", String(h));
    }, [width, height]);
    await page.screenshot({ path: out, omitBackground: true });
    await page.close();
    console.log(path.relative(path.join(brand, "..", ".."), out));
  }
  await browser.close();
})();
