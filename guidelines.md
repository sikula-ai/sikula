# Development Guidelines

This document defines coding conventions and architectural rules for the Sikula project.
It is loaded as context by AI agents — follow every rule precisely.
For the execution model, state fields, and component map, see `ARCHITECTURE.md`.

---

## Architecture

Multi-agent orchestration pipeline. Each concern lives in exactly one layer:

| Layer | Package | Responsibility |
|-------|---------|---------------|
| Orchestration | `core/orchestrator.py` | Phase sequencing, loop control, state transitions |
| Agents | `agents/` | Single-concern LLM calls; mutate `TaskState` in place |
| LLM clients | `core/llm_client.py` | Subprocess wrappers for AI CLI tools |
| State | `core/state.py` | Single source of truth; persisted as JSON after every mutation |
| Tools | `tools/` | File I/O, git, build — all platform-specific logic lives here |
| Config | `.sikula/config.yaml` | Per-project settings; agents must not hardcode project-specific values |

Dependency direction: `orchestrator → agents → llm_client`, `orchestrator → tools`. Agents never import tools directly — they receive a `tools: dict[str, Any]` from the orchestrator.

---

## Python Conventions

### Imports

Every production Python file must include this import after the optional module docstring
and before all other imports:

```python
from __future__ import annotations
```

Standard library imports, then third-party, then internal — one blank line between groups.

### Type hints

All function signatures and class attributes must have type hints. Use built-in generics
(`list[str]`, `dict[str, Any]`) — not `List`, `Dict` from `typing`. Use `X | None` for
optional values in new code; `Optional[X]` is acceptable in existing code for consistency.

```python
def run(self, state: TaskState) -> AgentResult:
    ...

def _cfg(self, key: str, default: int) -> int:
    ...
```

### Dataclasses

Use `@dataclass` for all data-carrying types. Use `field(default_factory=list)` for mutable defaults.

```python
@dataclass
class AgentResult:
    success: bool
    message: str
    data: dict = field(default_factory=dict)
```

### Module-level constants

Private constants use `_SCREAMING_SNAKE_CASE`. Prompt templates are always module-level constants:

```python
_MAX_DIFF_CHARS = 40_000
_DEFAULT_TIMEOUT = 300

_SYSTEM_REVIEW = """\
You are a senior software engineer...
"""

_USER_REVIEW = """\
Task description:
{task_description}
...
"""
```

### Logging

Use a module-level logger named `log`:

```python
log = logging.getLogger(__name__)
```

Log at `INFO` for significant actions, `WARNING` for recoverable anomalies, `ERROR` only
before returning failure. Do not use `print()` in agents, tools, or core orchestration
logic; use logging there. CLI command handlers in `sikula.py` may use `print()` for
intentional user-facing terminal output.

### Error handling

Return `AgentResult(success=False, ...)` or `ToolResult(success=False, ...)` on failure.
Catch `RuntimeError` from LLM clients. Do not catch broad `Exception` unless re-raising
or wrapping:

```python
try:
    output = self.llm.run_readonly_agent(prompt, cwd=root)
except RuntimeError as e:
    msg = str(e)
    state.record(self.name, "review_failed", msg[:500])
    return AgentResult(success=False, message=msg[:200])
```

---

## Agent Conventions

### Structure

Every agent subclasses `BaseAgent`, declares a `name` class attribute, and implements a
single public method `run(state) -> AgentResult`:

```python
class ReviewerAgent(BaseAgent):
    name = "reviewer"

    def run(self, state: TaskState) -> AgentResult:
        ...
```

Agents receive tools via `self.tools` (dict), LLM via `self.llm`, and project config via
`self.project_config`. Never import tools directly.

### Config access

Read project-YAML values through `self.project_config`, always with a default:

```python
def _cfg(self, key: str, default):
    return self.project_config.get("analyst", {}).get(key, default)
```

### Project-specific rules injection

Four agents support an `extra_rules` config key that appends a project-owned Markdown file to
the agent's system prompt as a `## Project-specific rules` section with an explicit priority
statement. Use `load_extra_rules()` from `agents/base_agent.py`:

```python
from agents.base_agent import load_extra_rules as _load_extra_rules

# In the agent's run() method, after building the system prompt:
system_prompt += _load_extra_rules(self.project_config, self.name, file_tool)
```

`load_extra_rules` reads the path from `project_config.get(agent_name, {}).get("extra_rules")`,
returns an empty string if the key is absent or the file cannot be read — always safe to call.

The four supported agents and their scope boundaries:

| Agent | `extra_rules` scope | What it must NOT contain |
|-------|---------------------|--------------------------|
| `reviewer` | Correctness checks, invariants, architecture constraints | Implementation instructions — reviewer is read-only |
| `security_reviewer` | Compliance rules, data classification, threat model | Anything that changes output format (BLOCKING/WARNING sections must stay intact) |
| `test_writer` | Testing conventions, required test doubles, naming patterns | Production code rules — test writer cannot touch production files |
| `planner` | Task-splitting conventions: what to split, what to keep atomic | Instructions for individual agents — planner only decides step boundaries |

**Do not add `extra_rules` support to `analyst`, `implementer`, or `fixer`.** The analyst's
configuration point is `guidelines.md` (loaded via `gather_guidelines()`). The implementer
and fixer follow the analyst's `implementation_prompt` — giving them independent rules risks
conflicts with the analyst's output.

### Prompt templates

Prompts are module-level string constants (`_SYSTEM_*`, `_USER_*`, `_AGENT_PROMPT`).
A system prompt defines agent role and rules. A user prompt carries the per-run data.
Use `.format()` substitution — no f-strings for prompts.

### LLM invocation — readonly vs. read-write

Use `self.llm.run_readonly_agent()` for agents that must not modify the filesystem
(reviewer, security reviewer, analyst). Use `self.llm.run_agent()` only for agents that
need to write files (implementer, fixer, test writer). Mixing these up breaks the
read-only invariant and the sandbox boundary.

| Agent | Method |
|-------|--------|
| `AnalystAgent`, `ReviewerAgent`, `SecurityReviewerAgent` | `run_readonly_agent()` |
| `ImplementerAgent`, `FixerAgent`, `TestWriterAgent` | `run_agent()` |

### State recording

Call `state.record(self.name, action, result)` after every meaningful action.
The `action` string is a short verb phrase (`"analyze"`, `"review_failed"`, `"implement"`).
The `result` is a single line — path list, char count, or error excerpt:

```python
state.record(self.name, "analyze", f"prompt generated ({len(prompt)} chars)")
state.record(self.name, "review_failed", msg[:500])
```

### Structured observability records

Every agent that invokes an LLM must append one record per invocation to the appropriate
`TaskState` list before returning:

| Agent | List |
|-------|------|
| `ImplementerAgent` | `state.implement_cycle_records` |
| `FixerAgent` | `state.fix_cycle_records` |
| `ReviewerAgent` | `state.review_cycle_records` |
| `SecurityReviewerAgent` | `state.security_review_cycle_records` |
| `TestWriterAgent` | `state.test_write_records` |
| Orchestrator validation phases | `state.validation_cycle_records` |

Each agent record must include at minimum: the agent's prompt, LLM output (`None` on
exception), files written, timestamp, and the correlation keys needed to locate the record
within the pipeline: `step`, `build_iteration` (`state.build_iterations`),
`review_iteration` (`state.review_iterations`), `security_review_iteration`
(`state.security_review_iterations`), and `scope` (`"task"`, `"step"`, or
`"final_full_task"` when the agent runs after all planned steps).

Validation records are orchestrator-owned and use a smaller shape: `phase`, `status`,
`build_iteration`, `step`, `timestamp`, optional `scope`, optional `elapsed_s`, optional
`check_name`, and short `error_excerpt` on failure.

Records are append-only and must not drive pipeline control flow — stop/continue decisions
belong in dedicated state fields (`review_approved`, `security_approved`, `failed`, etc.).

```python
state.implement_cycle_records.append({
    "step": state.current_step,
    "build_iteration": state.build_iterations,
    "review_iteration": state.review_iterations,
    "security_review_iteration": state.security_review_iterations,
    "scope": state.active_scope or ("step" if state.plan else "task"),
    "implementer_prompt": prompt,
    "implementer_output": output,  # None on exception
    "files_written": changed,
    "timestamp": datetime.now(timezone.utc).isoformat(),
})
```

### State mutations

Agents mutate `state` in place. The key state fields written by agents:

| Field | Written by | Rule |
|-------|-----------|------|
| `analyst_prompt` | `AnalystAgent` | Full assembled prompt stored before the LLM call; enables post-run audit even if guidelines files change |
| `implementation_prompt` | `AnalystAgent` | Structured prompt fed to implementer; the analyst's key output |
| `review_issues` | `ReviewerAgent` | Issue list from last review; cleared on approval |
| `files_changed` | `ImplementerAgent`, `FixerAgent` | Append-only; never clear outside the orchestrator |
| `errors`, `test_errors`, `check_errors` | Orchestrator | Cleared by the fixer after a fix pass |
| `history` | `state.record()` | Append-only audit log; never clear or modify past entries |
| `done`, `failed` | Orchestrator only | Never set these in an agent |

### Fix-pass pattern

`ImplementerAgent` is called both for the initial implementation and for review fix passes.
It detects the mode by checking `state.review_issues`: if non-empty, it appends the issues
to the prompt as a `REVIEW ISSUES` section. No separate mode flag exists — the agent is
stateless with respect to pass type. Any agent that participates in a fix loop should follow
the same pattern: read the relevant error/issue list from state, include it in the prompt,
and leave control flow to the orchestrator.

### Agent scope boundaries

Each agent has a fixed scope — crossing it silently breaks the pipeline:

- **`AnalystAgent`** must not suggest or generate test file changes — test changes are exclusively the domain of `TestWriterAgent`.
- **`AnalystAgent`, `ReviewerAgent`, and `TestWriterAgent`** must preserve structured input contracts: parser, validator, expression engine, DSL, config, schema, and rule-engine changes need explicit accepted/rejected cases, materially different rejected input classes, expected-result-type handling when typed contexts exist, and a validation-vs-runtime failure-phase distinction when observable.
- **`TestWriterAgent`** must only write to paths within `sandbox.allowed_test_write_paths`; production source files are off-limits.
- **`ReviewerAgent`** must stay read-only and mode-aware for tests: in normal `sikula run`, changed tests are not standalone reviewer-owned output, but contract-bearing test deletion, relaxation, or replacement with a different invalid fixture is evidence to re-check and report the production contract issue. In `sikula review`, changed tests are branch output and may be reported directly.
- **`FixerAgent`** write paths depend on error type: when `state.errors` is non-empty (build failure), only `allowed_write_paths` (production). When only `state.test_errors` or `state.check_errors` are set, the fixer uses both `allowed_write_paths` and `allowed_test_write_paths`; test failures may require production fixes when the failing test encodes the task, implementation prompt, project guidelines, or a structured contract. Test-failure fixer output must include production-vs-test triage for audit. Production writes from test-failure fixes require explicit `production_defect` + `production_code` triage; otherwise the task must fail before the pipeline accepts the change.
- **`ReviewerAgent` and `SecurityReviewerAgent`** must never write files — use `run_readonly_agent()` only.
- **`SecurityReviewerAgent`** fail-safe: if the LLM output contains no `APPROVED` signal, no `## Warnings` section, and no `## Security Issues` section, treat it as blocking. Never relax this — ambiguous output from the security reviewer must always fail closed.

### Platform neutrality

Agent prompts and logic must be platform-neutral. References to Gradle tasks, Kotlin idioms,
Android layer names, or any other platform-specific detail do not belong in agents. Platform
specifics belong only in `BuildTool` subclasses and project config YAMLs.

---

## Tool Conventions

### BaseTool

Subclass `BaseTool` for general-purpose tools. Always call `self.sandbox.check_write(path)`
before any write operation:

```python
class FileTool(BaseTool):
    def write(self, path: str, content: str) -> ToolResult:
        self.sandbox.check_write(Path(path))
        ...
```

### BuildTool

Subclass `BuildTool` for platform build systems. Implement all five required methods:
`sync()`, `compile_check()`, `run_tests()`, `run_check()`, `is_build_config_file()`.
Override `generate_sources()` only when `sync()` is too broad for the presync phase.
Override `env_files()` only when the platform needs gitignored files copied to new worktrees.

For Gradle-based platforms, subclass `GradleBaseTool` (`tools/gradle_tool.py`) instead of
`BuildTool` directly — it provides `_run()`, `_run_shell()`, `run_check()`, and
`is_build_config_file()` so you only need to implement `sync()`, `compile_check()`, and
`run_tests()`. See `AndroidGradleTool` and `JvmGradleTool` as reference implementations.

When adding a new platform, update four files: `tools/<platform>_tool.py` (new tool),
`core/orchestrator.py` (`_build_tool()`), `sikula.py` (`_build_tool_class()`,
`_generate_config()`, `_SUPPORTED_BUILD_TOOLS`), and `tools/scanner.py` (`_SIGNATURES` +
path detection).

All methods return `ToolResult`. Treat subprocess exit code 0 as success. Platform-specific
exit codes (e.g. pytest exit 5 = "no tests collected") may also be treated as success — document
why with an inline comment.

On failure, set `ToolResult.error` to the combined stdout+stderr output (not stderr alone) —
many tools (pytest, cargo test, ruff) write diagnostics to stdout:

```python
output = r.stdout + r.stderr
if r.returncode not in (0, 5):  # pytest exit 5 = no tests collected
    return ToolResult(success=False, output=output, error=output[-4000:])
return ToolResult(success=True, output=output)
```

---

## State Conventions

`TaskState` is the single source of truth. The orchestrator persists it to JSON after every
agent operation — do not cache state values in local variables across agent calls.

### Schema migrations

Removing, renaming, or changing the type of an existing `TaskState` field requires a
`schema_version` migration in `core/state.py`. Omitting it silently breaks resume for
existing tasks — the field is either missing or has the wrong type on load.

```python
# core/state.py — in JsonStateStore.load(), before TaskState(**data):
SCHEMA_VERSION = 2  # bump when making breaking field changes

# --- schema migrations (run in version order before TaskState is constructed) ---
if data.get("schema_version", 1) < 2:
    data["new_field"] = data.pop("old_field", None)
# --- end migrations ---
```

Adding a new optional field with a default value does not require a migration.

### StateStore abstraction

Orchestrator and agents must depend only on the `StateStore` interface (`core/state.py`).
Never import `JsonStateStore` directly outside of `sikula.py` — `JsonStateStore` is
instantiated only there and injected via constructor.

| Field category | Rule |
|---|---|
| `analyst_prompt` | Written by `AnalystAgent` before the LLM call; never overwrite after first write |
| `implementation_prompt`, `review_issues` | Written by agents; read by subsequent agents |
| `files_changed` | Append-only; never clear outside the orchestrator |
| `errors`, `test_errors`, `check_errors` | Cleared by the fixer after a fix pass |
| `history` | Append-only via `state.record()`; never cleared; permanent audit log |
| `validation_cycle_records` | Append-only via the orchestrator; never used for pipeline control flow |
| `done`, `failed` | Set by the orchestrator only; never set in an agent |

---

## Naming Conventions

| Element | Pattern | Example |
|---------|---------|---------|
| Agent class | `<Role>Agent` | `ReviewerAgent`, `TestWriterAgent` |
| Tool class | `<Platform>Tool` | `AndroidGradleTool`, `PythonTool` |
| LLM client class | `<Provider>Client` | `ClaudeClient`, `GeminiClient` |
| System prompt constant | `_SYSTEM_<VERB>` | `_SYSTEM_REVIEW` |
| User prompt constant | `_USER_<VERB>` | `_USER_REVIEW` |
| Agent prompt constant | `_AGENT_PROMPT` (single) or `_AGENT_<VERB>` | `_AGENT_PROMPT` |
| Config helper method | `_cfg(key, default)` | inside agent classes |
| Module logger | `log` | `log = logging.getLogger(__name__)` |
| Private constants | `_SCREAMING_SNAKE` | `_MAX_DIFF_CHARS` |
| Private methods | `_verb_noun` | `_gather_guidelines`, `_build_tool` |
| Agent `name` attribute | `snake_case` | `name = "security_reviewer"` |

---

## Testing Conventions

Framework: **pytest**. Test files live under `tests/` (the only allowed test write path).

### Structure

One test file per source module, flat under `tests/`: `agents/analyst_agent.py` → `tests/test_analyst_agent.py`.
Do not create subdirectories inside `tests/`.

### Fixtures

Define fixtures in `tests/conftest.py` for shared setup (fake `TaskState`, stub `LLMClient`,
minimal `project_config`). Use `pytest.fixture` — not class-level `setUp`.

```python
@pytest.fixture
def state() -> TaskState:
    return TaskState(task_id="abc123", task_description="test task")

@pytest.fixture
def project_config() -> dict:
    return {"sandbox": {"allowed_write_paths": ["agents/"], "allowed_read_paths": ["."]}}
```

### Naming

Test functions: `test_<what>_<condition>` or `test_<what>_when_<condition>`:

```python
def test_run_returns_failure_when_no_implementation_prompt(state):
    ...

def test_run_sets_review_approved_on_approval(state):
    ...
```

### Agent tests

Use a stub `LLMClient` (do not call real LLMs in tests). Verify:
- `AgentResult.success` value
- Mutations on `TaskState` (`state.review_approved`, `state.history`, etc.)
- `state.record()` entries appended to `state.history`

### Coverage

Target: ≥ 90% branch and line coverage on new and changed code (configured via
`test_writer.coverage_target` in project YAML). Every `AgentResult(success=False, ...)` path
must have a dedicated test.

---

## Code Quality

All code must pass `ruff check .` (lint) and `ruff format --check .` (formatting). These run
as CI checks on every task. Key rules enforced by ruff:

- No unused imports — remove them explicitly after every refactor.
- No bare `except:` — always name the exception type.
- `from __future__ import annotations` must appear immediately after the optional module
  docstring and before all other imports.

Run locally before committing:

```
python3 -m ruff check .
python3 -m ruff format .
```

---

## Rules for AI Agents

- **Never** modify files outside the active write scope given in the agent prompt.
- **Never** put platform-specific logic (Gradle tasks, Kotlin patterns, Android layer names) in agents, orchestrator, or LLM client code — it belongs only in `BuildTool` subclasses and `.sikula/config.yaml`.
- **Never** set `state.done` or `state.failed` in an agent — these are orchestrator-only fields.
- **Never** clear or modify past entries in `state.history` — it is a permanent audit log.
- **Never** use `state.implement_cycle_records`, `state.fix_cycle_records`, `state.review_cycle_records`, `state.security_review_cycle_records`, or `state.test_write_records` to drive pipeline control flow — they are observability records only.
- **Always** add `from __future__ import annotations` immediately after the optional
  module docstring and before all other imports.
- **Always** add type hints to every function signature.
- **Always** call `state.record(self.name, action, summary)` after every meaningful agent action.
- **Always** append one record to the appropriate `*_cycle_records` / `*_write_records` list per LLM invocation, including on exception (set `output` to `None`).
- **Always** store `state.analyst_prompt` before the LLM call in `AnalystAgent` — not after.
- **Always** set `ToolResult.error` to combined stdout+stderr (`output[-4000:]`), not stderr alone.
- **Always** return `AgentResult(success=False, message=...)` on failure — never raise from `run()`.
- **Always** write all code comments in English.
- **Always** run `ruff check .` after any code change; fix all reported issues before finishing.
- **Always** follow the naming conventions table above for new agents, tools, constants, and methods.
