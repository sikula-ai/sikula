# Development Guidelines

This document defines architectural rules and coding conventions for the Countries React example.
It is loaded as context by AI agents — follow every rule precisely.

---

## Architecture

Four-layer architecture with a strict, unidirectional dependency rule.

### Layers

Dependency direction is strict: **App → Feature UI → Domain ← Data**.
The domain layer has no knowledge of React, Vite, the browser, or test libraries.

| Layer | Path | Responsibilities |
|-------|------|------------------|
| App | `src/App.tsx`, `src/main.tsx` | Application entry point and top-level composition |
| Feature UI | `src/features/` | React components, screen state, user events |
| Domain | `src/domain/` | Types, filtering, formatting, and business logic |
| Data | `src/data/` | Static country data and future data adapters |
| Tests | `tests/` | Vitest and React Testing Library tests |

### Rules

- Feature components may import domain functions and data adapters.
- Domain files must not import React, browser APIs, test libraries, or CSS.
- Data files must not contain UI formatting or filtering logic.
- Keep reusable business logic in `src/domain/` and test it directly.
- Keep component state local unless a task explicitly requires routing or shared state.

---

## Data Model

`Country` is a plain TypeScript type in `src/domain/country.ts`.

```ts
export type Country = {
  code: string;
  name: string;
  capital: string;
  region: Region;
  population: number;
  area: number;
  flagEmoji: string;
};
```

- Country codes use ISO 3166-1 alpha-3 uppercase values.
- The data set lives in `src/data/countries.ts`.
- Do not fetch live network data unless the task explicitly asks for it.

---

## React UI

- Components are function components.
- Use explicit prop types near the component.
- Keep event handlers simple and named when they contain branching logic.
- Use semantic HTML first: `main`, `section`, `label`, `select`, `ul`, `li`, `button`.
- Use accessible labels for controls. Tests should query by role, label, or visible text.
- Avoid component tests that inspect implementation details, private state, CSS class names,
  or source files as text.

---

## Testing

### Unit Tests

- Unit tests live in `tests/` and use Vitest.
- Test pure domain functions directly.
- Use table tests when multiple inputs exercise the same branch.
- Cover empty, missing, and no-match cases for new filters or formatters.

### Component Tests

- Use React Testing Library and `@testing-library/user-event`.
- Test behaviour through rendered UI, roles, labels, and visible text.
- Do not assert on CSS classes unless the class itself is the public contract.
- Prefer one focused interaction per test.

### Test Infrastructure

- `tests/setup.ts` loads `@testing-library/jest-dom/vitest`.
- Do not add new test dependencies or change build configuration unless the task explicitly
  asks for project infrastructure changes.
- Do not add new end-to-end UI automation, screenshot, or browser infrastructure unless the task
  explicitly asks for it.
- If an acceptance contract within the existing test surface cannot be meaningfully tested through
  Vitest/Testing Library, report a `TESTABILITY GAP` instead of writing source-inspection tests.

---

## Code Style

- TypeScript `strict` mode must stay enabled.
- Prefer immutable data and derived values with `useMemo` for filtered lists.
- Keep display formatting in small named functions.
- Keep CSS responsive and readable; avoid layout shifts when filter state changes.
- User-visible strings should stay in components until a task introduces localization.
