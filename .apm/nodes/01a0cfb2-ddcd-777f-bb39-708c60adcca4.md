## Intent

- Vite + React. `apm serve` serves the built UI; in development, Vite proxies to the server. The token is read from the URL and kept in `sessionStorage`. A typed API client.
- A Playwright job in CI that starts `apm serve` on a fixture project.

## Acceptance criteria

- [ ] the Playwright job loads the page and shows the project name

## Notes

Plan: plans.md §12.3, P13.1.
