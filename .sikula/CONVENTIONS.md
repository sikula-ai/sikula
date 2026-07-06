# Sikula Self-Hosting Conventions

These conventions apply when this repository is developed with Sikula's own
task, contract, and delivery-plan workflows. They are not requirements for
projects using Sikula.

## Directory Layout

- Use `.sikula/tasks/<slug>.md` for standalone source task descriptions that
  are not part of a delivery plan.
- Use `.sikula/contracts/<slug>.contract.md` for prepared runnable
  implementation contracts generated from task descriptions.
- Use `.sikula/delivery/<slug>/plan.yaml` for a tracked parent delivery plan.
- Use `.sikula/delivery/<slug>/units/<unit-slug>.md` for delivery unit task
  descriptions referenced by that parent plan.
- Keep delivery progress runtime state under
  `.sikula/state/delivery/<plan-id>/`; do not create or edit those files by
  hand as source artifacts.
- Treat `.sikula/contract-reports/`, `.sikula/state/`, and
  `.sikula/worktrees/` as generated runtime or preparation artifacts unless a
  maintainer explicitly asks to commit a specific file for audit or debugging.

## Task And Delivery Plan Identity

- Use stable English kebab-case slugs for Sikula task files, delivery
  `plan_id` values, delivery unit IDs, and related branch names.
- Do not prefix task, plan, or unit identifiers with manual sequence numbers
  such as `001-`. Parallel work and merge order make those numbers unstable.
- Use issue, pull request, or milestone numbers only as external tracking
  references or metadata, not as task or plan identity.
- Do not reuse task or delivery plan slugs that have already been committed. If
  follow-up work continues the same area, choose a more specific derived slug,
  for example `review-state-reset-coverage`.
- Do not rename a slug after work has started unless the old identity is
  intentionally abandoned.
- Keep the source task file name aligned with the slug, for example
  `.sikula/tasks/review-state-reset.md`.
- Keep `.sikula/delivery/<slug>/` aligned with the delivery `plan_id`.
- Model ordering inside delivery plans with `depends_on`, not numeric prefixes.
- Use short kebab-case delivery unit IDs scoped to the plan, for example
  `state-metadata`, `cli-output`, and `validation-tests`.

## Branches, Commits, And Pull Requests

- Name branches as `<type>/<slug>`, for example
  `feature/task-state-audit`, `fix/review-state-reset`, or
  `docs/task-conventions`.
- Use human-readable commit and pull request titles rather than slug-only
  titles, for example `Fix review state reset after changes`.
- Keep the slug stable across the task, delivery plan, branch, and review
  discussion so state, events, and history remain easy to correlate.
