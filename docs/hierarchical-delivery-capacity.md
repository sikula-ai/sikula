# Hierarchical delivery capacity experiment

This developer experiment needs no product task, API keys or external provider.
It uses a synthetic tree of Python modules in fresh temporary Git repositories.
Each node contributes one and sums its immediate children's totals. Fixed local
work and configurable depth/fan-out expose growth in preparation and verification.

Run from a checkout with the development dependencies installed:

```bash
# Complete small deliveries, including a late defect and its repair.
TMPDIR=/tmp .venv/bin/python -m tests.delivery_capacity --depths 1 2 --scenario repair

# Cheap preparation-only growth measurements; this does not execute any units.
TMPDIR=/tmp .venv/bin/python -m tests.delivery_capacity --depths 1 2 3 4 --prepare-only

# Explore a wider graph and record the first actual production limit.
TMPDIR=/tmp .venv/bin/python -m tests.delivery_capacity --depths 2 3 4 --fanout 4 --prepare-only

# Check a persistent final-gate security or read-only stop.
TMPDIR=/tmp .venv/bin/python -m tests.delivery_capacity --depths 1 --scenario security
TMPDIR=/tmp .venv/bin/python -m tests.delivery_capacity --depths 1 --scenario readonly
```

On Windows, use the environment's Python executable without the `TMPDIR` prefix.
Depth zero has one unit; binary depths one, two and three request 3, 7 and 15
units. Each non-root node becomes a checkpoint. The first unsupported shape
retains its real stop phase/code. No production limits or review policies are
patched to make a large case succeed. Use `--trace-memory` to measure peak Python
allocations, accepting its timing overhead; subprocess memory is not included.

The complete flow calls the actual CLI for hierarchical preparation, replay,
publication, isolated unit execution, assembly, checkpoints, resume and finalization.
Build/tests and both unit/gate review roles are enabled. TestWriter is disabled;
this experiment measures delivery coordination, not test generation. Only provider
responses and implementations are fake. Evidence measurement wraps the resolver's
normal exit without skipping validation or changing budgets.

In the repair scenario the last unit deliberately changes the first leaf's
contribution to zero. Unit review is scripted to accept it, representing a missed
integration regression. On the composed path, the final semantic reviewer requests
current-candidate child verification and the child reports the gap. Sikula performs
its normal repair, authority renewal where applicable, and fresh verification.
Small scopes may legitimately choose a cheaper full review and final-gate repair;
reports retain `composed` per review and `authority_renewals` to distinguish paths.
Do not count a full-review repair as evidence of recursive candidate recovery.
The experiment also interrupts the first checkpoint review and resumes it. Final
replay must make no provider calls or execute units again. Final committed modules are checked against
the expected implementation, and the fixture's operator index must remain intact.

This is a deterministic control-flow and capacity check, **not an assessment of
LLM judgment or production reliability**. Scripted approvals are not evidence that
a real model would find a defect. The small [real-provider preparation smoke](hierarchical-preparation-smoke.md)
is a separate experiment and still does not execute its generated plan.

Each command creates a new `sikula-capacity-*` directory under the system temporary
directory and prints its location. Cases contain a private `.capacity/report.json`,
raw CLI output, normal Sikula audits and runtime state. The top-level `summary.json`
contains all case reports. The experiment stages/commits only its newly created
fixture repositories; it neither stages the Sikula checkout nor deletes directories.
An existing fixture destination is refused. No provider CLI is launched.

Reports distinguish `prepared` from `completed` and include phase timings, stop
codes, prompt/response bytes by stage/node, implementation counts, evidence reads
and charged materialization, source/plan/artifact sizes, and optional Python peak
memory. These are instrumentation measurements, not token or financial estimates.
`checks_passed` separately records whether the experiment's assertions completed;
a finalized product result alone cannot hide a failed experiment assertion.
Compare maximum local prompts at equal local work across shapes and early/late
gates; total calls and storage naturally grow with delivered work. Timing includes
local machine and tracing effects, so do not use absolute wall-time CI thresholds.
Raw audit/output files remain private even though this fixture is synthetic.
Python memory measurements include the in-process fake and report instrumentation;
run cases serially on the same host when comparing timing or memory trends.

Exit zero means all requested cases completed, all preparation-only cases prepared,
or an explicitly selected terminal-stop scenario retained its expected boundary.
Unsupported capacity returns nonzero and its report remains available. Preparation
success, expected rejection and completed delivery are separate report outcomes.

Routine CI runs helper checks and one small repair E2E. Larger growth experiments
are opt-in; they must not add multi-hour runs to the default suite. Preparation
success alone does not establish execution capacity for larger deliveries.
