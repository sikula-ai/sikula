# Sandbox, Isolation, And Privacy

Sikula combines git isolation, provider-level restrictions where available, prompt constraints, and audit records. It does not replace human review or your organization's security controls.

## Git Worktree Isolation

By default, `sikula run TASK_FILE` creates a git worktree under `.sikula/worktrees/<task-id>/` and a branch named `sikula/<task-stem>-<task-id>`.

Successful runs are committed to that branch and the worktree is removed. Interrupted or failed runs preserve the worktree for inspection.

```bash
# Resume interrupted work
sikula run --task-id <task-id>

# Retry terminal failed work
sikula run --task-id <task-id> --reset-failed

# Preview cleanup/delete actions without removing anything
sikula cleanup <task-id>
sikula delete <task-id>

# Remove clean preserved worktrees when you are done inspecting them
sikula cleanup <task-id> --force
sikula delete <task-id> --force

# Remove dirty preserved worktrees and discard uncommitted agent changes
sikula cleanup <task-id> --force --discard
sikula delete <task-id> --force --discard
```

Use `--no-isolate` only for local experiments where you want changes directly in the current working tree.

## Config Files Must Be Committed

Isolated task worktrees start from `HEAD`. Before the first isolated run, commit `.sikula/config.yaml`, any files listed under `guidelines.context_files`, and any `extra_rules` files used by enabled agent phases. Those prompt-context paths must be files in the worktree start ref. When a delivery child starts from an assembled commit instead of `HEAD`, its config blob must exactly match the committed config loaded for the run; otherwise Sikula stops before creating child state or a worktree. Review worktrees start from the reviewed branch or captured start commit, so review prompt context must also be present there as files.

```bash
git add .sikula/
git commit -m "Add Sikula project setup"
```

## Filesystem Scope

`.sikula/config.yaml` defines:

- `sandbox.allowed_write_paths`
- `sandbox.allowed_test_write_paths`
- `sandbox.allowed_read_paths`

These paths are passed to agents as constraints. Provider-specific hard enforcement varies. Sikula audits changed files after write-capable agent calls and records write-scope warnings when changes fall outside the active scope.

## Read-Only Agents

Reviewer and security reviewer are read-only by design. They call `run_readonly_agent()` and should not write files.

The analyst is also read-only. The implementer, fixer, and test writer are write-capable.
Every `run_readonly_agent()` prompt includes a shared instruction not to use tools or commands
to create, modify, delete, move, rename, format, or write files. The model may still return
requested generated content in its final response; Sikula CLI code decides whether to write
that output. The same instruction asks read-only agents to reference project files with
project-relative paths instead of absolute local paths or `file://` URIs. Provider-level
enforcement is still provider-specific.

## Provider Enforcement

Provider enforcement depends on the CLI. This table summarizes the controls Sikula
configures; exact guarantees remain provider-specific.

| Provider | Read-only calls | Write-capable calls |
|---|---|---|
| Codex | Uses Codex read-only sandbox. Shell filtering is prompt-level. | Workspace-write sandbox; command restrictions are prompt-level. |
| Claude | Uses generated Claude settings and allowed tools. | Bash restrictions are technically enforced via allowed tools. |
| Gemini | Read-only settings exclude write and shell tools. | Workspace write tool checks path; shell restriction is prompt-level. |
| OpenCode | Read-only agents deny bash in generated config. | Workspace boundary depends on OpenCode; write restrictions are prompt-level. |
| Antigravity | Disposable copy; rejects copy changes and external symlinks on kept paths. | Rejects external symlinks on kept paths; runs `agy` on the task worktree with a workspace instruction. |

For Antigravity, Sikula protects the task checkout by using a disposable copy for read-only calls and kept-path symlink validation before write-capable calls; OS-level prevention of writes outside those workspaces depends on Antigravity CLI sandbox behavior.

See [ARCHITECTURE.md](../ARCHITECTURE.md) for exact implementation details.

## Network Access

Agents are instructed not to make network requests or access external services. Provider-specific tool restrictions may reduce network-capable access, but Sikula does not rely on a provider-independent network-deny setting.

Build tools may run dependency sync commands such as package installs when configured. Those commands happen outside agent control.

## State Privacy

Task state is stored under `.sikula/state/` and can be inspected with:

```bash
sikula show <task-id>
```

State may contain:

- task descriptions
- implementation contracts
- prompts and LLM outputs
- source excerpts
- diffs and changed file paths
- build/test/check output
- provider errors and retry records
- config snapshots

Review and redact state before sharing it publicly or attaching it to an issue.
