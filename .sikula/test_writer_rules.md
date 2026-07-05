# Test Writer Rules

Additional TestWriter focus for Sikula self-hosting.

- Add focused, deterministic tests for behavior changed by the contract.
- Use existing pytest infrastructure, `FakeLLMClient`, temporary repositories,
  temporary state dirs, and explicit config files for command-path coverage.
- Do not call real provider-backed `sikula run`, `sikula review --fix`,
  `task refine`, or `contract prepare` commands in tests.
- Cover changed state transitions, migrations, resume/reset behavior,
  validation coverage, terminal summaries, status/show output, strict JSON
  projections, sandbox blockers, provider diagnostics, worktree cleanup/delete,
  and review-fix delivery when those surfaces are touched.
- Do not edit production code from the test writer role. If safe testing
  requires missing project seams or fixtures, make no risky test changes and
  report:

```text
TESTABILITY GAP:
target: <behavior or contract that remains untested>
reason: <why existing test seams or fixtures are insufficient>
recommended_action: <what project-level test support is needed>
risk: low | medium | high
```
