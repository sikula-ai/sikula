# Carry constraints into delivery children

## Goal

Create an auditable, deterministic context boundary that carries applicable parent constraints into delivery child state and agent decisions.

## Current behavior

Child agents receive their unit contract and dependency handoffs but are not guaranteed to receive the governing ownership and dependency constraints from the source task. Correct behavior can therefore depend on an agent rediscovering the parent task in the repository.

## Desired behavior

A newly created delivery child persists bounded inherited-constraint metadata and supplies the relevant information to analysis, implementation, review, and security review. The child context must distinguish authoritative constraints from advisory dependency evidence and must not allow either an agent or a handoff to expand write authority. Additive state must remain compatible with legacy children and plans.

## Repo context

Delivery children are created or resumed from a tracked delivery plan through `delivery run-next`. The parent plan, unit task description, dependency handoffs, and child task state are the existing repository and audit boundaries; inherited constraints must stay correlated with those boundaries across fresh and resumed runs.

## Acceptance criteria

- New child state retains the normalized applicable constraints and their parent-plan correlation.
- Analyst, implementer, reviewer, and security-review prompts receive the same deterministic constraint context appropriate to their role.
- Agents do not need to search for the parent source task to discover a governing constraint.
- Dependency handoffs remain evidence and cannot override source constraints or unit scope.
- Legacy state without the additive fields loads and resumes compatibly.
- Relevant prompt and invocation audit records remain complete without exposing their contents through ordinary delivery output.

## Security and privacy

Persist only bounded structured constraint data needed for execution and audit. Do not add raw parent task text, prompts, provider output, diffs, file contents, environment values, or absolute paths to public status or result projections.

## Reviewer focus

Check state compatibility, correlation with the correct parent plan and unit, consistent prompt injection across all affected agents, and separation between constraint authority and dependency-handoff evidence.

## Out of scope

Do not make observability records drive control flow, add cross-repository execution, or change existing dependency-handoff authority.

## Validation

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
