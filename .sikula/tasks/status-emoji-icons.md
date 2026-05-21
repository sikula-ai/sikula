# Status emoji icons

## Background

The human-readable `sikula status` table shows task statuses as plain text. Adding small
icons to the `STATUS` column would make mixed task lists easier to scan at a glance.

## Requirements

In the human-readable status table, prefix each task's status label with an emoji icon:

- `DONE` → ✅
- `FAILED` and `build failed` → ❌
- `CLEANED` → 🧹
- `INTERRUPTED` → ⏸️
- any other in-progress phase (for example `starting`, `analyzing`, `implementing`,
  `reviewing`, `security review`, `writing tests`, `testing`, `building`) → 🔄

The text label should remain unchanged alongside the icon.

Apply this only to human-readable `sikula status` output, including `--verbose`.
Do not change `sikula status --json`; JSON `status` values must remain machine-readable
plain strings such as `"DONE"` or `"implementing"`.

Keep the existing table columns and alignment readable after adding the icons.

## Out of scope

- Changes to any other command output (`run`, `show`, `review`)
- Changes to agents, orchestrator, or state
