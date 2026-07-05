# Reviewer Rules

Additional reviewer focus for Sikula self-hosting.

- Verify the implementation satisfies the prepared contract without unrelated
  workflow, provider, or state-model drift.
- Pay special attention to CLI/status/JSON contracts, `TaskState` compatibility,
  worktree/resume behavior, review-fix delivery, sandbox policy, and provider
  diagnostics.
- Reject machine-readable output that serializes raw `TaskState`, `sikula show`,
  prompt text, logs, or terminal output instead of using explicit projections.
- Flag changes that rely on editable-install module reload, nested real Sikula
  delivery commands, or an active run picking up a modified `.sikula/config.yaml`.
- If public behavior changes, verify the required documentation or config
  updates are included.
