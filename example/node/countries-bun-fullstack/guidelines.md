# Development Guidelines

This document defines architectural rules and coding conventions for the Countries Bun
full-stack example. It is loaded as context by AI agents - follow every rule precisely.

---

## Architecture

Layering is strict. Shared types may be imported by any layer, but runtime dependencies point
in one direction:

**Server/UI -> Domain -> Data**

| Layer | Path | Responsibilities |
|-------|------|------------------|
| Server | `src/server.ts`, `src/server/` | `Bun.serve`, API routes, static asset serving |
| Client | `src/client/` | Browser UI, DOM events, rendering state |
| Domain | `src/domain/` | Filtering, lookup, formatting, aggregate calculations |
| Data | `src/data/` | Static country fixtures |
| Shared | `src/shared/` | Cross-boundary TypeScript types |
| Tests | `tests/` | `bun:test` coverage for domain, server, and client model logic |

### Rules

- API response shapes must be defined by shared/domain types, not ad hoc object literals in UI code.
- Client code calls the `/api/*` endpoints; it must not import `src/data/` directly.
- Domain files must not import Bun server APIs, browser APIs, DOM APIs, or test libraries.
- Data files must not contain UI formatting, HTTP routing, or filtering logic.
- Keep behaviour that can be tested without a browser in domain or client view-model helpers.
- Do not add a framework unless a task explicitly asks for project infrastructure changes.

---

## Data Model

`Country` is a plain TypeScript type in `src/shared/country.ts`.

```ts
export type Country = {
  code: string;
  name: string;
  capital: string;
  region: Region;
  population: number;
  area: number;
};
```

- Country codes use ISO 3166-1 alpha-3 uppercase values.
- The data set lives in `src/data/countries.ts`.
- Do not fetch live network data unless the task explicitly asks for it.
- API endpoints must use uppercase country codes in URLs and responses.

---

## Backend

- Use `Bun.serve` and standard `Request` / `Response` objects.
- Keep route parsing in `src/server/http.ts`.
- Return JSON with `application/json; charset=utf-8`.
- Invalid query values should return `400` with `{ "message": "..." }`.
- Missing resources should return `404` with `{ "message": "..." }`.
- Do not introduce global mutable state for request handling.

---

## Frontend

- Use semantic HTML and accessible labels.
- Keep rendering deterministic: all visible list state should come from API responses and
  small pure helpers in `src/client/viewModel.ts`.
- Query controls should be labelled and keyboard usable.
- Empty states are normal UI states, not errors.
- User-visible strings may stay in client code until a task introduces localization.

---

## Testing

- Tests live in `tests/` and use `bun:test`.
- Test pure domain functions directly.
- Test API routes by calling `handleRequest(new Request(...))`; do not bind real ports.
- Test frontend behaviour through pure view-model helpers unless a task adds browser test
  infrastructure.
- Do not write source-inspection tests that parse `.ts` files as text.
- If a task cannot be tested through the existing `bun:test` setup, report a `TESTABILITY GAP`
  instead of changing project infrastructure.

---

## Code Style

- Keep TypeScript explicit at module boundaries.
- Prefer immutable arrays and small pure functions.
- Use `Intl.NumberFormat("en-US")` for display number formatting.
- Keep API and UI naming aligned: `country`, `countries`, `region`, `population`.
- Avoid dependencies unless the task explicitly requires them.
