## Intent

- Run `apm init` in this repo. The plan repo gets its own private remote (to be created by you). This PR commits only `.apm-link` and the `.gitignore` entry to the code repo.
- Enter the remaining phases (P10+) as nodes through an interactive `claude` session with the plugin, with milestones as the top-level nodes, phases as their children and child phases below those.
- Fix the problems found while dogfooding, and list them in the PR.

## Acceptance criteria

- [ ] the APM plan is maintained in APM
- [ ] `apm ready` shows the next phase

## Notes

Plan: plans.md §12.3, P09.
