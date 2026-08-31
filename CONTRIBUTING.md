# Contributing to Sikula

Sikula is a maintainer-led project. Feedback, bug reports, task-result reports,
documentation fixes, and small corrections are especially helpful.

For code changes beyond small fixes — including new platforms, LLM providers,
agents, or pipeline changes — please open an issue or discussion before starting
so we can align on scope and keep the project focused.

When reporting a bug, attaching the task state JSON (`sikula show <task-id>`) is very helpful for diagnosis. **Review it before attaching** — it contains full LLM prompts and outputs, which may include your source code, task description, and inlined guidelines content. Redact any proprietary or sensitive content before sharing.

## CLA

Pull requests require a signed Contributor License Agreement — see [CLA.md](CLA.md).
The maintainer will provide signing instructions before a contribution is merged.

## AI-assisted contributions

AI-assisted tools such as Codex, ChatGPT, Claude, GitHub Copilot, or similar tools are allowed when preparing contributions.

You are responsible for what you submit. Review AI-assisted changes before opening a PR, and do not submit third-party code, confidential information, secrets, or material you do not have the rights to contribute under the Sikula CLA.

Maintainers may also use AI-assisted tools to review pull requests, suggest fixes, and prepare patches. Human maintainers make the final merge decision.

## Setup

```bash
git clone https://github.com/sikula-ai/sikula
cd sikula/

# Install pipx first if needed: https://pipx.pypa.io/stable/installation/
pipx install --editable .
pipx inject sikula pytest pytest-cov ruff
```

Editable installs run directly from the checkout. `sikula --version` shows the packaged
version plus a development suffix when the checkout is inside git, for example
`sikula 0.2.0-dev+feature.example.abc1234`.

After pulling or making changes that add an importable package, change package
discovery, update console entry points, or change dependencies, refresh the
editable install before testing CLI entry points. With `pipx`, use:

```bash
pipx reinstall sikula
```

If you develop inside a regular virtual environment instead of `pipx`, use:

```bash
python3 -m pip install -e . --force-reinstall
```

## Running tests

```bash
# All tests (unit + e2e)
python3 -m pytest tests/

# Unit tests only
python3 -m pytest tests/ --ignore=tests/e2e

# E2E tests only
python3 -m pytest tests/e2e/ -v

# Focused real-Git delivery amendment and artifact assembly checks
python3 -m pytest tests/ -m delivery_amendment_git -q

# Coverage, for larger PRs or pipeline/state changes
python3 -m pytest tests/ --cov=agents --cov=core --cov=tools --cov=sikula --cov-report=term-missing
```

## Code style

```bash
ruff check .
ruff format .
```

## Architecture principles

Before opening a PR, read [ARCHITECTURE.md](ARCHITECTURE.md) and [guidelines.md](guidelines.md). `ARCHITECTURE.md` covers the execution model, state fields, and component map. `guidelines.md` defines coding conventions, agent rules, and state invariants — it is also loaded as context by AI agents, so keeping it accurate is important. Key rules:

- **Platform-agnostic pipeline** — agent prompts and orchestration flow must not contain platform-specific implementation logic (`if platform == "Android"` is always wrong in an agent prompt). Platform context reaches agents only via `project.platform` injected as tech stack at runtime. Platform-specific build commands belong in `BuildTool` subclasses (`tools/`). Adding a new platform should require a new `BuildTool` subclass plus registration/config-generation updates, not changes to agent behaviour or pipeline phase semantics.
- **StateStore abstraction** — orchestrator and agents must depend only on the `StateStore` interface, never import `JsonStateStore` directly. `JsonStateStore` is instantiated only in `sikula.py`.
- **LLM prompts in state** — key prompts must be stored in dedicated `TaskState` fields (e.g. `state.implementation_prompt`); all agent actions and outcomes must be logged via `state.record()`. Nothing that an LLM received or produced may be lost between runs.
- **Structured observability records** — every agent that invokes an LLM must append one record per invocation to the appropriate `TaskState` list (`implement_cycle_records`, `fix_cycle_records`, `review_cycle_records`, `security_review_cycle_records`, `test_write_records`, or a new list for a new agent type). Orchestrator validation phases append to `validation_cycle_records`. Each record must include the correlation context (`step`, plus any iteration counters) needed to locate it within the pipeline. Agent records must also include the prompt, LLM output (`None` on exception), and timestamp. Records are append-only and must not drive pipeline control flow — stop/continue decisions belong in dedicated state fields (`review_approved`, `security_approved`, `failed`, etc.). The one permitted exception: reviewer agents may read their own prior records to pass LLM history for consistency across iterations.
- **TaskState migrations** — removing, renaming, or changing the type of an existing state field requires a `schema_version` migration in `core/state.py`; omitting it silently breaks resume for existing tasks.
- **Reviewer and security reviewer are read-only** — both must use `run_readonly_agent()`, never `run_agent()`. They must never write files.
- **Analyst must not suggest test file changes** — test changes are exclusively the domain of `TestWriterAgent`.
- **TestWriterAgent must only write to test directories** — it must be constrained to `sandbox.allowed_test_write_paths`; production source files are off-limits.
- **Security reviewer fail-safe must not be weakened** — unexpected or ambiguous output (no `APPROVED` signal, no `## Warnings`, no `## Security Issues`) must always be treated as blocking.
- **Provider sandbox** — document the workspace boundary enforcement level (OS-level vs prompt-level) in the sandbox notes for the new provider. See `core/llm_client.py` for existing implementations.
- **Keep documentation in sync** — any PR that changes documented behaviour must update `ARCHITECTURE.md`, the relevant sections of `README.md`, and topic docs such as `docs/writing-tasks.md` for task or contract syntax changes in the same PR.
- **English comments only** — all code comments must be in English.

## Scope

This repository contains the open-source Sikula core: the CLI, orchestration,
task state, built-in agents, built-in providers, documentation, and examples.

## Writing tests

The test suite has two layers with different purposes and conventions.

### Unit tests

Unit tests live in `tests/test_<module>.py`. New agents and tools should have a corresponding test file.

**What to test:** prompt construction, output parsing, state transitions, orchestrator logic, build tool commands. **What not to test:** actual LLM behaviour — LLM calls must always be mocked via `unittest.mock`.

Aim for 90% line and branch coverage on new or changed code.

### E2E tests

E2E tests live in `tests/e2e/` and exercise full command paths (`cmd_run`, `cmd_review`) using a `FakeLLMClient` — no API keys or network required. Use them to cover multi-agent scenarios, state transitions across phases, and error/retry cycles that are awkward to unit-test against the orchestrator directly.

**Available fixtures** (from `tests/e2e/conftest.py`):

| Fixture | Type | Purpose |
|---|---|---|
| `fake_llm` | factory | Returns a `FakeLLMClient`; `agent_responses` is a list of `{rel_path: content}` dicts written to disk in order |
| `seq_fake_llm` | factory | Returns a `SequencedFakeLLMClient`; separate per-call queues for `generate`, `run_readonly_agent`, `run_agent` — use when the same method is called by multiple agents in sequence |
| `git_project` | `Path` | Minimal Python project inside a fresh git repo (a `src/calculator.py` + `tests_proj/`) |
| `git_review_project` | `(Path, str)` | `git_project` with a committed feature branch ready for `sikula review` |

**`FakeLLMClient` defaults:**
- `generate()` → `"SINGLE_PASS"` (planner stays in single-pass mode)
- `run_readonly_agent()` → `"APPROVED"` (analyst, reviewer, security reviewer all approve)
- `run_agent()` → pops next `{rel_path: content}` from the queue and writes files to disk; returns `[]` when the queue is empty

**Minimal example — single-pass run that writes one file:**

```python
from unittest.mock import patch
import pytest
from sikula import cmd_run
from tests.e2e.conftest import e2e_cfg, run_args

def test_happy_path(git_project, fake_llm):
    task_file = git_project / ".sikula" / "tasks" / "task.md"
    task_file.parent.mkdir(parents=True, exist_ok=True)
    task_file.write_text("Add a hello function.")

    fake = fake_llm(agent_responses=[{"src/feature.py": "def hello(): pass\n"}])
    with patch("core.llm_client.create_llm_client", return_value=fake):
        with pytest.raises(SystemExit) as exc_info:
            cmd_run(run_args(task_file=str(task_file)), e2e_cfg(git_project))
    assert exc_info.value.code == 0
```

**When to use `SequencedFakeLLMClient`:** when you need different responses from the same method across different agents. For example, `run_readonly_agent` is called by analyst, reviewer, and security reviewer in sequence — use `readonly_responses=[analyst_out, "ISSUES...", "APPROVED", "APPROVED"]` to control each call independently.

## Before submitting a PR

- [ ] `python3 -m pytest tests/` passes
- [ ] Coverage checked for new or changed code where meaningful
- [ ] `ruff check .` and `ruff format --check .` pass
- [ ] `ARCHITECTURE.md`, `README.md`, and relevant topic docs updated if the change affects documented behaviour
- [ ] CI passes on the PR

CI runs automatically on every PR. Linux runs the Python 3.10–3.13 matrix,
compile check, tests, Ruff lint/format, and Codecov diff coverage. A Python 3.12
Windows job runs the compile check and full test suite. All checks must pass
before merge.
