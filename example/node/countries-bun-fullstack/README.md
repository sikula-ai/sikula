# Countries

A small full-stack countries browser built with TypeScript and Bun, used as an example
project for [Sikula](https://github.com/sikula-ai/sikula).

The backend uses `Bun.serve`, the frontend is a browser TypeScript app bundled by Bun,
and tests use `bun:test`.

## Stack

| Tool | Purpose |
|---|---|
| Bun | Runtime, package manager, test runner, and bundler |
| TypeScript | Shared frontend/backend language |
| `Bun.serve` | HTTP server |
| `bun:test` | Unit and API tests |

## Usage

```bash
# Install Bun first: https://bun.com/docs/installation

# Check the installed Bun version
bun --version

# Install dependencies from the lockfile
bun install --frozen-lockfile

# Type-check TypeScript
bun run typecheck

# Run tests
bun run test

# Build browser assets
bun run build

# Validate fixture data
bun run check:fixtures

# Run the app
bun run dev
```

Open `http://localhost:3000` after `bun run dev`.

## Architecture

```
src/
  server.ts                 - Bun.serve entry point
  server/
    http.ts                 - API routes and static asset serving
  client/
    main.ts                 - browser entry point
    viewModel.ts            - pure frontend presentation model
    styles.css              - app styles copied during build
  data/
    countries.ts            - static country data
  domain/
    countryService.ts       - filtering, lookup, and formatting
  shared/
    country.ts              - Country, Region, and API-facing types
public/
  index.html                - browser shell copied during build
scripts/
  build.ts                  - Bun browser bundle
  checkFixtures.ts          - deterministic fixture validation check
tests/
  countryService.test.ts    - domain tests
  server.test.ts            - API route tests
  clientViewModel.test.ts   - frontend presentation tests
tsconfig.json               - strict TypeScript type checking
```

Dependency direction is strict:

`server/client -> domain -> data`, with shared types in `src/shared/`.

## API

| Route | Description |
|---|---|
| `GET /api/countries` | List countries |
| `GET /api/countries?region=Europe` | Filter countries by region |
| `GET /api/countries/:code` | Fetch one country by alpha-3 code |
| `GET /api/regions` | List supported regions |
| `GET /api/stats` | Aggregate population statistics |

## Data

Static country data in `src/data/countries.ts`, adapted from
[REST Countries](https://restcountries.com) (MPL 2.0).

## Sikula tasks

Ready-to-run tasks in `.sikula/tasks/`:

| Task file | Description |
|---|---|
| `add-country-detail-view.md` | Country Detail View |
| `add-search-by-name.md` | Add Name Search across API and UI |
| `format-population.md` | Format Population as Compact String |

Run from this directory: `sikula run .sikula/tasks/<task-file>`

Sikula validation for these tasks is defined in `.sikula/config.yaml`.
