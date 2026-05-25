# Countries

A demonstration web app built with TypeScript, React, and Vite, used as an example project for [Sikula](https://github.com/sikula-ai/sikula).

## Stack

| Library | Purpose |
|---|---|
| React 18 | UI |
| TypeScript | Language and static type checking |
| Vite | Development server and production build |
| Vitest | Unit and component tests |
| React Testing Library | Behaviour-focused component tests |
| ESLint | Static analysis |

## Usage

```bash
# Install dependencies
npm ci

# Run the app
npm run dev

# Type-check
npm run typecheck

# Run tests
npm test

# Run lint
npm run lint

# Build production assets
npm run build
```

## Architecture

```
src/
  main.tsx                  — React entry point
  App.tsx                   — top-level application component
  data/
    countries.ts            — static country data
  domain/
    country.ts              — Country and Region types, population formatting
    countryFilters.ts       — region filtering and region list logic
  features/
    countries/
      CountriesPage.tsx     — countries screen state and composition
      CountryList.tsx       — list rendering
      RegionFilter.tsx      — region select control
tests/
  setup.ts                  — Testing Library setup
  countryFilters.test.ts    — domain tests
  CountriesPage.test.tsx    — component tests
```

## Data

Static country data in `src/data/countries.ts`, adapted from [REST Countries](https://restcountries.com) (MPL 2.0).

Dependency direction: `App` → `features` → `domain` ← `data`.
The domain layer has no knowledge of React, Vite, or browser APIs.

## Sikula tasks

Ready-to-run tasks in `.sikula/tasks/`:

| Task file | Description |
|---|---|
| `add-country-detail-view.md` | Country Detail View |
| `add-search-by-name.md` | Add Name Search to Countries List |
| `format-population.md` | Format Population in Countries List |

Run from this directory: `sikula run .sikula/tasks/<task-file>`
