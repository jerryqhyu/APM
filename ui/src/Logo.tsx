// APM's logo, drawn inline from the shapes docs/brand/build.py writes into brand.ts, so it follows
// the theme: where the page is light it sits on a navy plate, as the white stripe would vanish.

import { LOGO } from "./brand";

const PAD = 22;

export function Logo({ height = 28 }: { height?: number }) {
  const width = LOGO.width + 2 * PAD;
  const tall = LOGO.height + 2 * PAD;
  return (
    <svg
      className="apm-logo"
      viewBox={`${-PAD} ${-PAD} ${width} ${tall}`}
      height={height}
      width={(height * width) / tall}
      role="img"
      aria-label="APM"
    >
      <rect className="apm-logo-plate" x={-PAD} y={-PAD} width={width} height={tall} rx={PAD * 0.9} />
      {LOGO.stripes.map((stripe) => (
        <path key={stripe.fill} d={stripe.d} fill={stripe.fill} />
      ))}
      <path d={LOGO.word} fill={LOGO.white} fillRule="evenodd" />
    </svg>
  );
}

export function GearIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 16 16" aria-hidden fill="none" stroke="currentColor" strokeWidth="1.4">
      <circle cx="8" cy="8" r="2.2" />
      <path
        d="M8 1.5v1.6M8 12.9v1.6M1.5 8h1.6M12.9 8h1.6M3.4 3.4l1.1 1.1M11.5 11.5l1.1 1.1M3.4 12.6l1.1-1.1M11.5 4.5l1.1-1.1"
        strokeLinecap="round"
      />
      <circle cx="8" cy="8" r="4.6" />
    </svg>
  );
}
