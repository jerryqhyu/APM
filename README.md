# APM

A local-first project manager built for working with AI agents. See [plans.md](plans.md) for the design and the phased implementation plan (§12).

## Development

Requires Node 24+ and pnpm (enable it with `corepack enable pnpm`; the version is pinned in `package.json`).

```bash
pnpm install
pnpm apm --version   # run the CLI straight from source, no build needed
pnpm test            # vitest
pnpm typecheck       # tsc, no emit
pnpm lint            # biome; `pnpm format` fixes formatting
pnpm build           # emit packages/*/dist
```

| Package | Contents |
|---|---|
| `packages/core` | graph model, invariants, lifting, git I/O, index, `mutate()` |
| `packages/app` | the `apm` binary: CLI, `serve`, `mcp` |
