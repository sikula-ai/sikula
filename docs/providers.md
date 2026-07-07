# Providers

Sikula has built-in CLI integrations for Codex, Claude, Gemini, OpenCode, and Antigravity.

| Provider value | CLI | Notes |
|---|---|---|
| `codex` | `codex exec` | Default in generated configs. |
| `claude` | `claude -p` | Uses Claude Code Agent SDK behavior. |
| `gemini` | `gemini` | Uses Gemini CLI. |
| `opencode` | `opencode run` | Model must usually be in `provider/model` format. |
| `antigravity` | `agy --print -` | Uses Antigravity CLI 1.0.13 or newer. Model names are passed directly to `agy --model`; use the exact display names from `agy models`, for example `"Gemini 3.5 Flash (High)"`. |

Provider CLIs and model names change over time. Keep model examples in your project config current with the provider documentation you use.

Antigravity model values are not normalized by Sikula. If the display name contains spaces,
quote it in YAML:

```yaml
llm:
  provider: antigravity
  model: "Gemini 3.5 Flash (High)"
  agent_timeout: 1800
```

## Configure The Default Provider

```yaml
llm:
  provider: codex
  model: gpt-5.3-codex
  agent_timeout: 1800
```

## Override Individual Agents

```yaml
agents:
  analyst:
    llm:
      model: stronger-model
  reviewer:
    llm:
      model: stronger-model
  security_reviewer:
    llm:
      model: stronger-model
  delivery_preparer:
    llm:
      model: gpt-5.5
      agent_timeout: 1800
```

CLI overrides:

```bash
sikula run .sikula/tasks/my-task.md \
  --agent-provider implementer=claude \
  --agent-model reviewer=stronger-model \
  --agent-timeout implementer=2400
```

The analyst, reviewer, and security reviewer usually benefit most from stronger reasoning. The implementer, test writer, and fixer often need enough timeout for large codebases.

Valid `run` and `review` agent names are `analyst`, `planner`, `implementer`, `reviewer`, `security_reviewer`, `test_writer`, and `fixer`. `sikula task refine --auto` and `sikula contract prepare --auto` also accept `task_preparer` overrides. `sikula delivery prepare` accepts `delivery_preparer` overrides, distinct from `task_preparer`. `agents.delivery_preparer.llm` falls back to the top-level `llm` settings for omitted fields. Delivery prepare uses command-free read-only authoring; providers that cannot enforce that mode fail closed.

## Authentication

Authenticate with the provider CLI before running Sikula:

```bash
codex login
claude login
gemini
opencode auth login
agy
```

Provider-specific API-key or enterprise authentication should be configured according to that provider's documentation. For Antigravity, run `agy --version` and `agy models` to verify the CLI is version 1.0.13 or newer, installed, and authenticated before using it through Sikula. Antigravity CLI is still evolving quickly, so Sikula may raise this minimum as the provider interface stabilizes. Sikula loads `.env` from the project root at startup; existing shell environment variables take precedence.

## Data Boundary

Sikula runs locally in your repository. The configured provider determines what task, prompt, source, and diff context may be sent outside your machine. Choose a provider and authentication mode that matches your organization's data policy.

Antigravity calls that attach a project first reject absolute symlinks and relative symlinks that resolve outside the project root on paths Sikula keeps under its workspace policy. Untracked ignored local artifacts such as `.venv` and `node_modules` are pruned so ordinary dependency/runtime directories do not block runs; tracked or preserved paths inside soft-ignored directories are still checked. Internal project-relative symlinks are allowed. Antigravity CLI does not currently expose a verified non-interactive read-only permission mode, so read-only agents then run against a disposable temporary copy and the result is rejected if that copy changes. Write-capable agents run against the task worktree with `agy --new-project --add-dir <project> --sandbox --dangerously-skip-permissions --print -`; Sikula also prepends an Antigravity-specific workspace-boundary instruction so the provider uses that task worktree rather than searching for another checkout. OS-level prevention of writes outside these workspaces is governed by Antigravity CLI sandbox behavior. Antigravity may still keep provider-owned conversation state under its user profile directory; Sikula does not copy those files into task state.

## Adding A Provider

Provider integrations implement `LLMClient` in `core/llm_client.py`:

- `generate(system, user) -> str`
- `run_readonly_agent(prompt, cwd, allow_commands=True) -> str`
- `run_agent(prompt, cwd) -> tuple[list[str], str]`

Register the new provider in `create_llm_client()`. See [ARCHITECTURE.md](../ARCHITECTURE.md) for the full interface contract.
