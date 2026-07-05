# Delivery Plans

Delivery plans are the planned parent layer for large work that should be split
into small Sikula delivery units. The current MVP validates a tracked plan file,
reports privacy-safe parent progress, and can run one eligible unit at a time
through the normal `sikula run` pipeline.

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

Preview the next eligible unit without changing delivery progress:

```bash
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --dry-run --json
```

Run the next eligible unit:

```bash
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml
sikula delivery run-next .sikula/delivery/<slug>/plan.yaml --json
```

`run-next` without `--dry-run` acquires a parent delivery progress lock, marks
the selected unit as `running`, starts one ordinary child `sikula run` for that
unit task file, then records the terminal unit status as `done` or `failed`.
Child task prompts, provider output, diffs, logs, and full task state remain in
the normal child task state and are not embedded in delivery progress JSON.

For dependent units, `run-next` also checks that each completed dependency's
recorded result commit, when present, is already applied to the current checkout.
A completed dependency with no result commit is treated as a no-op prerequisite.
The current execution MVP does not assemble an accumulated delivery branch, so
dependent units are blocked until the operator has merged or otherwise applied
prerequisite unit branches locally.

Unlike `check` and `status`, `run-next` loads project runtime config because it
uses the same project settings as `sikula run`.

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

The current execution MVP does not create or update the plan's `final_branch`.
Final delivery branch assembly and multi-unit orchestration are reserved for a
later delivery-plan phase.

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

`delivery check --json`, `delivery status --json`, and `delivery run-next
--json` return plan metadata, validation issues, unit paths, compact progress
fields, selected child task IDs, and branch/commit pointers when available. They
do not embed unit task file bodies, prompts, provider output, diffs, logs, or
task state.
