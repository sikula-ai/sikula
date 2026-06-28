# AGENTS.md

## Review guidelines

When reviewing this repository, use these project documents as context:

- `README.md`
- `guidelines.md`
- `ARCHITECTURE.md`
- `CONTRIBUTING.md`

Focus especially on:

- Pipeline correctness: `run`, `resume`, `review`, `review --fix`, `--no-isolate`, `cleanup`, and `delete` flows must keep working.
- Task state compatibility: additive fields are fine; removing, renaming, or changing existing `TaskState` field types requires a schema migration in `core/state.py`.
- Auditability: prompts, LLM outputs, validation records, retry records, and relevant state transitions must not be lost.
- Separation of concerns: orchestration belongs in `core/orchestrator.py`; agent behavior belongs in `agents/`; provider subprocess logic belongs in `core/llm_client.py`; platform build behavior belongs in `tools/`.
- Reviewer and security reviewer agents must stay read-only.
- Agent prompts and orchestration must remain platform-agnostic.
- Tests must cover changed state transitions, output parsing, and pipeline branches.
- Do not remove copyright, license, attribution, or notice information.
- Do not log or expose secrets, tokens, API keys, private prompts, source excerpts, task state, or personal data.
- Do not accept changes that weaken security-review fail-safe behavior.
- Do not accept changes that make task state less useful for debugging, auditing, or resume.
- Treat legal, licensing, policy, release, and project-governance documents (`LICENSE`, `NOTICE`, `CLA.md`, `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `PRIVACY.md`, `SECURITY.md`, `RELEASE.md`, `CHANGELOG.md`, `THIRD_PARTY_NOTICES.md`, `AGENTS.md`) as maintainer-owned; do not suggest incidental changes unless the PR explicitly targets them.
- Keep review comments focused on material correctness, security, maintainability, and testing risks.
- Encourage PRs to reach towards 90% test coverage, but treat it as a goal rather than a strict merge blocker.
- Sandbox enforcement: Do not accept changes that bypass or weaken the `Sandbox` restrictions (`allowed_read_paths`, `allowed_write_paths`).
- Parser robustness: Agent output parsers (e.g. for structured LLM blocks) must degrade safely and not crash the orchestrator if formatting is hallucinated.
- Output decoupling: CLI text output must remain decoupled from core pipeline logic to ensure machine-readable formats (e.g. `--json`) consume the exact `TaskState` schema directly.
- CLI-provider changes: verify prompt transport, timeout handling, diagnostic redaction, retry classification, and read-only/write-mode boundaries.
- Prompt privacy: stored prompts are audit artifacts; do not expose them through ordinary diagnostics or external reports. `sikula show` is the explicit full-state audit exception.
