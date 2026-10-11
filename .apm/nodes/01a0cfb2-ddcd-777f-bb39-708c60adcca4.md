## Intent

- `ui/`: the UI's own pnpm workspace (Vite + React + `@xyflow/react` + elkjs, Biome, Vitest). `apm serve` serves the built UI, which ships inside the wheel; in development, Vite proxies to the server. The token is read from the URL and kept in `sessionStorage`. A typed API client.
- CI gains a UI job (lint, typecheck, tests, build) and a Playwright job (pytest-playwright) that starts `apm serve` on a fixture project.

## Acceptance criteria

- [ ] the Playwright job loads the page and shows the project name

## Notes

Plan: plans.md §12.3, P13.1.
