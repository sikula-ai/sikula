# Providers

Sikula has built-in CLI integrations for Codex, Claude, Gemini, and OpenCode.

| Provider value | CLI | Notes |
|---|---|---|
| `codex` | `codex exec` | Default in generated configs. |
| `claude` | `claude -p` | Uses Claude Code Agent SDK behavior. |
| `gemini` | `gemini` | Uses Gemini CLI. |
| `opencode` | `opencode run` | Model must usually be in `provider/model` format. |

Provider CLIs and model names change over time. Keep model examples in your project config current with the provider documentation you use.

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
```

CLI overrides:

```bash
sikula run .sikula/tasks/my-task.md \
  --agent-provider implementer=claude \
  --agent-model reviewer=stronger-model \
  --agent-timeout implementer=2400
```

The analyst, reviewer, and security reviewer usually benefit most from stronger reasoning. The implementer, test writer, and fixer often need enough timeout for large codebases.

Valid `run` and `review` agent names are `analyst`, `planner`, `implementer`, `reviewer`, `security_reviewer`, `test_writer`, and `fixer`. `sikula task refine --auto` and `sikula contract prepare --auto` also accept `task_preparer` overrides.

## Authentication

Authenticate with the provider CLI before running Sikula:

```bash
codex login
claude login
gemini
opencode auth login
```

Provider-specific API-key or enterprise authentication should be configured according to that provider's documentation. Sikula loads `.env` from the project root at startup; existing shell environment variables take precedence.

## Data Boundary

Sikula runs locally in your repository. The configured provider determines what task, prompt, source, and diff context may be sent outside your machine. Choose a provider and authentication mode that matches your organization's data policy.

## Adding A Provider

Provider integrations implement `LLMClient` in `core/llm_client.py`:

- `generate(system, user) -> str`
- `run_readonly_agent(prompt, cwd) -> str`
- `run_agent(prompt, cwd) -> tuple[list[str], str]`

Register the new provider in `create_llm_client()`. See [ARCHITECTURE.md](../ARCHITECTURE.md) for the full interface contract.
