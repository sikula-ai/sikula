# Sandbox, Isolation, And Privacy

Sikula combines git isolation, provider-level restrictions where available, prompt constraints, and audit records. It does not replace human review or your organization's security controls.

## Git Worktree Isolation

By default, `sikula run TASK_FILE` creates a git worktree under `.sikula/worktrees/<task-id>/` and a branch named `sikula/<task-stem>-<task-id>`.

Successful runs are committed to that branch and the worktree is removed. Failed or interrupted runs preserve the worktree for inspection or resume.

```bash
sikula run --task-id <task-id>
sikula cleanup <task-id> --force
sikula delete <task-id> --force
```

Use `--no-isolate` only for local experiments where you want changes directly in the current working tree.

## Config Files Must Be Committed

Isolated worktrees start from `HEAD`. Before the first isolated run, commit `.sikula/config.yaml` and any files listed under `guidelines.context_files`.

```bash
git add .sikula/config.yaml .sikula/.gitignore
git add .sikula/guidelines.md  # if generated
git commit -m "Add Sikula config"
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

## Provider Enforcement

Provider enforcement depends on the CLI:

| Provider | Read-only calls | Write-capable calls |
|---|---|---|
| Codex | Uses Codex read-only sandbox. Shell filtering is prompt-level. | Workspace-write sandbox; command restrictions are prompt-level. |
| Claude | Uses generated Claude settings and allowed tools. | Bash restrictions are technically enforced via allowed tools. |
| Gemini | Read-only settings exclude write and shell tools. | Workspace write tool checks path; shell restriction is prompt-level. |
| OpenCode | Read-only agents deny bash in generated config. | Workspace boundary depends on OpenCode; write restrictions are prompt-level. |

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
