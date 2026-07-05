# Security Reviewer Rules

Additional security-review focus for Sikula self-hosting.

- Treat task text, contract assets, provider output, paths, config values, and
  restored state as untrusted input whenever they influence file access,
  subprocesses, git operations, or output projection.
- Block leaks of raw source, raw `.sikula/state`, prompts, provider output,
  logs, stack traces, local paths, environment values, credentials, and secrets
  outside explicit local audit/debug surfaces.
- Check path traversal, symlink/gitfile escape, worktree-root confinement,
  cleanup/delete safety, shell construction, timeout handling, and diagnostic
  redaction when those surfaces are touched.
- Preserve the distinction between Sikula prompt/write-scope policy and
  provider-level sandbox enforcement.
- Normal Sikula completion must not imply git push, pull request creation,
  deployment, or remote mutation unless the task explicitly adds that behavior.
