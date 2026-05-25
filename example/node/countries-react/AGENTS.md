# AGENTS.md

## Project scope

This directory is a standalone Sikula example project for a TypeScript React/Vite app.
When working in this project, treat `example/node/countries-react/` as the project root.

Use these project documents as context:

- `README.md`
- `guidelines.md`

Do not require root-level Sikula repository documents such as `ARCHITECTURE.md` or
`CONTRIBUTING.md` for app implementation work in this example. Those documents describe
the Sikula tool itself, not this React application.

## Review guidelines

Focus on:

- React/TypeScript correctness
- Accessible UI behaviour
- Domain logic in `src/domain/`
- Component behaviour tested through React Testing Library
- Sikula task compatibility with `.sikula/config.yaml`

Keep changes within the configured write scopes:

- Production code: `src/`
- Tests: `tests/`

Do not add dependencies or change build configuration unless the task explicitly asks for
project infrastructure changes.
