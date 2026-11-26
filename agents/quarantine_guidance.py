"""Shared instructions for the orchestrator-authorized reversible file operation."""

QUARANTINE_CONSTRAINT = """\
- If the only correct fix is removing one untracked file that this run created in the current process, do not
  use shell deletion. Request Sikula's reversible quarantine by emitting exactly one JSON object:
  {"sikula_file_operation_schema_version":1,"operation":"quarantine_untracked","path":"project/relative/file"}
- The request is accepted only for a verified agent-created ordinary file inside this exact write
  scope. Sikula moves it outside the worktree and retains its contents until explicit cleanup.
- Do not empty or stub an obsolete file merely to emulate its removal.
- Request at most one quarantine operation in this response. Do not request it for tracked files,
  directories, links, pre-existing files, or files outside the active write scope.
"""
