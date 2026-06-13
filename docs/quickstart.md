# Quickstart

This guide gets Sikula running on an existing git repository.

## Prerequisites

- Python 3.10+
- `git`
- `pipx`
- One authenticated LLM CLI provider, such as Codex, Claude, Gemini, or OpenCode
- A target project that is already a git repository

Install `pipx` from the official guide if it is not available on your system: <https://pipx.pypa.io/stable/installation/>.

## Install

```bash
pipx install sikula
```

For local development on Sikula itself, use the editable install documented in [CONTRIBUTING.md](../CONTRIBUTING.md).

## Authenticate A Provider

Generated configs use `provider: codex` by default:

```bash
codex login
```

Other built-in provider values are `claude`, `gemini`, and `opencode`. See [Providers](providers.md).

## Initialize Your Project

Run from the project root:

```bash
cd my-project/
sikula init
```

`sikula init` scans the project and writes `.sikula/config.yaml`. It detects the build tool, language/platform, source/test write paths, Node package manager and scripts when applicable, shared Xcode scheme when present, and existing guidance files such as `AGENTS.md`, `README.md`, `ARCHITECTURE.md`, and `CONTRIBUTING.md`.

Review any `TODO` comments in `.sikula/config.yaml`.

## Optional: Generate Guidelines

Guidelines are a strong lever for output quality because they tell every agent what architecture, conventions, and constraints to follow.

```bash
sikula init --guidelines --provider codex --model <model-your-provider-supports>
```

If `.sikula/config.yaml` already contains `llm.provider` and `llm.model`, you can omit `--provider` and `--model`.

## Commit Config Before The First Isolated Run

Default Sikula runs create a git worktree from `HEAD`. Config and guideline files must be tracked and clean, otherwise the task worktree cannot see them.

```bash
git add .sikula/config.yaml .sikula/.gitignore
git add .sikula/guidelines.md  # if generated
git commit -m "Add Sikula config"
```

For local experiments without a worktree branch, use `sikula run --no-isolate`.

## Write A Task

```bash
mkdir -p .sikula/tasks
$EDITOR .sikula/tasks/my-task.md
```

A good task states the user-visible goal, expected behavior, important constraints, out-of-scope items, and any required validation beyond the configured build/test/check pipeline.

## Check The Implementation Contract

```bash
sikula contract check .sikula/tasks/my-task.md
```

If the task needs clarification:

```bash
sikula contract check .sikula/tasks/my-task.md --write-report
# edit .sikula/contracts/my-task.answers.yaml
sikula contract improve .sikula/tasks/my-task.md \
  --answers .sikula/contracts/my-task.answers.yaml \
  --output .sikula/tasks/my-task.v2.md
```

## Run

```bash
sikula run .sikula/tasks/my-task.md
```

Sikula creates a branch named `sikula/<task-stem>-<task-id>`, runs the configured delivery pipeline, commits successful changes to that branch, and removes the temporary worktree.

## Inspect Results

```bash
sikula status
sikula status --verbose
sikula show <task-id>
git diff <base-branch>...sikula/<task-stem>-<task-id>
```

Task state may contain source excerpts, prompts, provider output, and build logs. Review and redact it before sharing.
