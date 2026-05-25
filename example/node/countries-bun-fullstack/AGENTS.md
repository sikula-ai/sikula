# AGENTS.md

## Project scope

This directory is a standalone Sikula example project for a Bun full-stack TypeScript app.
When working in this project, treat `example/node/countries-bun-fullstack/` as the project root.

Use these project documents as context:

- `README.md`
- `guidelines.md`

Do not require root-level Sikula repository documents such as `ARCHITECTURE.md` or
`CONTRIBUTING.md` for app implementation work in this example. Those documents describe
the Sikula tool itself, not this Bun application.

## Review guidelines

Focus on:

- Bun runtime and TypeScript correctness
- API contract compatibility under `/api/*`
- Frontend behaviour through public DOM state
- Shared types in `src/shared/`
- Domain logic in `src/domain/`
- Sikula task compatibility with `.sikula/config.yaml`

This example intentionally does not use OpenAPI, Swagger, GraphQL, proto, or generated
API contract files. Treat `src/shared/` types together with `src/server/http.ts` route
handlers as the source of truth for local API response shapes; do not warn merely because
a separate formal API schema file is absent.

Keep changes within the configured write scopes:

- Production code and UI assets: `src/`, `public/`
- Tests: `tests/`

Do not add dependencies or change build configuration unless the task explicitly asks for
project infrastructure changes.
