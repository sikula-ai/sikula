# Add read-only delivery plan authoring assistance

## Goal

Add the read-only authoring step that can decompose a higher-level task
description into a structured delivery plan draft and unit task drafts without
writing repository files.

## Current behavior

Sikula has read-only preparation assistance for task refinement and contract
preparation, but delivery plans do not have an equivalent authoring assistant.
The parent plan YAML and unit task Markdown are currently human-authored.

## Desired behavior

- Add a read-only delivery plan authoring assistant that receives the source
  task description, project context, configured guidelines, and delivery plan
  constraints.
- The assistant uses a distinct `delivery_preparer` agent role. Its default LLM
  settings come from `agents.delivery_preparer.llm`, falling back to the global
  `llm` config like other agents.
- `sikula delivery prepare` forwards `--agent-model`,
  `--agent-provider`, and `--agent-timeout` overrides for `delivery_preparer`
  using the same `AGENT=value` pattern as `task refine` and
  `contract prepare`.
- The assistant returns exactly one strict structured draft, suitable for a
  deterministic writer to consume. The draft contains parent plan metadata,
  unit IDs, dependencies, titles, optional stream/component/scope metadata, and
  complete unit task Markdown.
- The parser rejects malformed, ambiguous, duplicate, cyclic, or incomplete
  output without crashing the CLI or orchestrator.
- Generated unit task drafts are product/behavior descriptions with acceptance
  criteria and verification expectations, not file-by-file implementation
  scripts.
- The assistant must be read-only and must not create files, run commands,
  inspect external services, or start nested Sikula runs.

## Draft handoff contract

The parsed authoring draft is the only handoff from the LLM assistant to later
writer code. It should contain:

- plan metadata: `plan_id`, `title`, and optional `planning_mode`;
- units: stable unit `id`, `title`, `depends_on`, optional `stream`,
  `component`, `phase`, `kind`, `platform`, and `scope_paths`;
- `task_markdown` for each unit, with goal, current behavior, desired behavior,
  acceptance criteria, security/privacy notes, reviewer focus, out-of-scope
  boundaries, and verification commands.

The draft should not be treated as approved implementation. It is a proposal for
reviewable source artifacts, and the artifact writer must derive safe file paths
from the output directory and unit IDs instead of trusting LLM-supplied paths.

## Compatibility and safety

- The authoring prompt and parsed draft must remain platform-agnostic.
- The assistant must not infer security, privacy, validation, or product
  requirements that are not supported by the source task or checked-in project
  context.
- The parsed draft must avoid embedding raw source excerpts, prompts, provider
  output, task state, secrets, or personal data into ordinary CLI output.
- Invalid LLM output must degrade to a clear authoring failure, not a partial
  or silently accepted delivery plan.

## Security and privacy

- Do not log, print, or expose raw prompts, raw LLM output, raw source task
  text, source excerpts, task state, API keys, tokens, local absolute paths, or
  personal data through ordinary diagnostics or JSON output.
- Keep raw provider responses and authoring diagnostics in local audit artifacts
  only, under the configured preparation/report directory.
- Treat malformed or ambiguous provider output as blocking. Do not silently
  accept partial plans, unsafe task paths, duplicate unit IDs, unknown
  dependencies, or unit task bodies that omit acceptance criteria.

## Reviewer focus

- Inspect prompt boundaries, parser failure modes, and privacy-safe diagnostic
  handling.
- Confirm `delivery_preparer` does not reuse `task_preparer` config, even if it
  reuses shared helper code and read-only execution patterns.
- Confirm the read-only assistant cannot write files, start nested Sikula runs,
  access the network, or introduce provider-specific behavior.
- Check that generated unit task drafts remain product/behavior descriptions
  and do not become brittle file-by-file implementation scripts.

## Acceptance criteria

- The structured draft format is tested with valid output, malformed JSON,
  missing plan metadata, duplicate unit IDs, unknown dependencies, unsafe task
  paths, and unit Markdown omissions.
- Tests prove that writer-facing paths are derived deterministically after
  parsing, not trusted from raw LLM output.
- Read-only authoring records enough local audit information to debug provider
  failures without exposing raw task bodies in normal terminal or JSON output.
- The delivery authoring logic reuses existing provider, timeout, guideline,
  and read-only prompt conventions where practical.
- Tests cover default `agents.delivery_preparer.llm` config, fallback to global
  `llm`, and explicit `--agent-* delivery_preparer=...` overrides.
- The generated unit task Markdown is suitable for normal `sikula run` after the
  artifact writer persists it.

## Out of scope

- Do not write delivery plan files or unit task files in this unit.
- Do not start implementation runs for generated units.
- Do not introduce provider-specific prompt behavior.

## Verification

- `python3 -m compileall -q agents/ core/ sikula_cli/ tools/ sikula.py`
- `python3 -m pytest tests/ -v`
- `python3 -m ruff check .`
- `python3 -m ruff format --check .`
- `python3 tools/check_whitespace.py`
