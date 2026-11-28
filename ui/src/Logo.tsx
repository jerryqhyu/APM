// APM's logo, drawn inline from the shapes docs/brand/build.py writes into brand.ts, so it follows
// the theme: on no background, with white letters in the dark and black letters in the light.

import { LOGO } from "./brand";

export function Logo({ height = 22 }: { height?: number }) {
  return (
    <svg
      className="apm-logo"
      viewBox={`0 0 ${LOGO.width} ${LOGO.height}`}
      height={height}
      width={(height * LOGO.width) / LOGO.height}
      role="img"
      aria-label="APM"
    >
      {LOGO.stripes.map((stripe) => (
        <path
          key={stripe.fill}
          d={stripe.d}
          fill={stripe.fill}
          className={stripe.fill === LOGO.white ? "apm-logo-white" : undefined}
        />
      ))}
      <path className="apm-logo-word" d={LOGO.word} fillRule="evenodd" />
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
