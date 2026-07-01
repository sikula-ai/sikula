# Delivery Plans

Delivery plans are the planned parent layer for large work that should be split
into small Sikula delivery units. The current MVP validates a tracked plan file
and reports privacy-safe parent progress. It does not run units yet.

Use delivery plans when a request is too large for one implementation contract or
when the work spans multiple streams such as backend, web, Android, iOS, docs, or
release tasks.

```text
large request
  -> delivery plan
  -> delivery units
  -> one normal Sikula run per unit
  -> final delivery branch
```

## Current MVP Commands

Validate a plan file:

```bash
sikula delivery check .sikula/delivery/<slug>/plan.yaml
sikula delivery check .sikula/delivery/<slug>/plan.yaml --json
```

Show parent progress for a plan:

```bash
sikula delivery status .sikula/delivery/<slug>/plan.yaml
sikula delivery status .sikula/delivery/<slug>/plan.yaml --json
```

These commands do not create worktrees, run agents, prepare contracts, write task
state, or update branches.

The MVP validator checks:

- `schema_version: 1`,
- required plan metadata such as `plan_id`, `title`, and `final_branch`,
- delivery unit IDs,
- unit task paths,
- unit dependency references and cycles,
- optional stream references,
- single-repository scope.

`delivery status` first runs the same plan validation, then reads ignored parent
progress from `.sikula/state/delivery/<plan-id>/progress.json` when present. If
that progress file does not exist yet, all delivery units are reported from the
plan as `pending`, with dependency blockers derived from `depends_on`.

## Plan Shape

Example:

```yaml
schema_version: 1
plan_id: checkout-redesign
title: Checkout redesign
planning_mode: fixed_window
final_branch: sikula/delivery/checkout-redesign
streams:
  - id: backend
    label: Backend
units:
  - id: 01-domain-model
    title: Add checkout domain model
    stream: backend
    platform: shared
    task_path: .sikula/delivery/checkout-redesign/units/01-domain-model.md
    depends_on: []
  - id: 02-api
    title: Add checkout API endpoints
    stream: backend
    platform: shared
    task_path: .sikula/delivery/checkout-redesign/units/02-api.md
    depends_on:
      - 01-domain-model
```

Unit task files are ordinary Markdown task descriptions for future Sikula runs.
The parent plan stores structure and ordering; it should not duplicate raw task
content.

## Repository Scope

The MVP supports one Git repository. If `repositories` is omitted, Sikula treats
the plan as a single implicit repository:

```yaml
repositories:
  - id: main
    root: .
```

Multi-repo plans are reserved for a later delivery-plan phase. Until then,
`delivery check` rejects multiple repositories instead of pretending that Sikula
can coordinate cross-repo branches, locks, validation, and result sets.

## Privacy

`delivery check --json` and `delivery status --json` return plan metadata,
validation issues, unit paths, and compact progress fields. They do not embed unit
task file bodies, prompts, provider output, diffs, logs, or task state.
