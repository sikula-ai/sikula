# Hierarchical preparation smoke

This opt-in smoke uses a real provider through the current checkout's
`sikula.py delivery prepare`. It exercises prompt/schema agreement, independent
local review, publication/readiness, and reuse on a second preparation command.
It is not part of CI and does not execute the generated delivery plan.

Use an environment with Sikula's development dependencies installed. Provider
authentication must already work. Select the provider and model explicitly;
the script does not copy the Sikula repository's provider configuration.

```bash
# From the Sikula checkout; this step makes no provider calls.
# Substitute your chosen provider and model for PROVIDER and MODEL.
TMPDIR=/tmp .venv/bin/python tools/smoke_hierarchical_preparation.py init \
  --provider PROVIDER --model MODEL

# Substitute the directory printed by init. This invokes the real provider.
.venv/bin/python tools/smoke_hierarchical_preparation.py run /tmp/PRINTED-DIRECTORY

# Read aggregate sizes and stages later, without provider calls.
.venv/bin/python tools/smoke_hierarchical_preparation.py inspect /tmp/PRINTED-DIRECTORY
```

`init` creates a new repository, source task, minimal Python package, passing
standard-library unittest baseline, and configuration. It never stages or commits
files. `--directory` can select a new destination; existing directories are refused.
On Windows, use the environment's Python executable and temporary directory in
place of the POSIX command prefixes above. No application dependency installation
is needed in the fixture.

The task covers stock reservations, atomic multi-line order creation, idempotent
cancellation/completion, and a downstream facade. It requests hierarchical
preparation while leaving decomposition to the model. Shared stock rules and exact
error codes provide concrete authority to inspect across group boundaries.

The smoke passes only when:

- preparation reports both `prepared` and `ready`;
- durable hierarchical exchanges exist and the plan has intermediate checkpoints;
- repeating preparation with `--force` reuses the same reservations and emits no
  additional provider-exchange audit records;
- the generated plan and unit files remain byte-identical after replay.

The script stops after failed preparation or flat routing; it does not blindly
retry a failed real-provider run. Repeating `run` on the same fixture uses Sikula's
normal preparation-resume rules. A fresh experiment requires a fresh fixture.
Use `--timeout` on `init` to change the per-provider-call timeout (default 600
seconds). This is not an overall spending or time cap: preparation retains its
production attempt limits. Review provider/model and scope before running it.

`.smoke/report.json` records success/failure, preparation and replay durations,
unit/checkpoint counts, and per-exchange prompt/response byte sizes and stages.
It does not estimate tokens or monetary costs. Raw CLI output stays in `.smoke/`;
full prompt/response audits stay in `.sikula/contract-reports/`, and durable
preparation control stays in `.sikula/state/delivery-preparation/`. Keep these
private; the summary does not include source or generated contract contents.

A passing smoke is not proof of semantic correctness. Manually review the generated
contracts and graph: shared rules and exact error codes must reach affected units;
rollback/idempotency must have clear ownership; consumers must wait for the relevant
checkpoint. Verify that parent completion prompts contain only local work and
immediate-child references. One small run also does not establish scaling capacity.

Executing the resulting plan through checkpoint repair and finalization is a
separate real-provider experiment. Commit the fixture's source/configuration and
prepared plan yourself first, then use the normal delivery commands. This script
does not stage artifacts or start implementation automatically. Automated E2E tests
already cover those transitions with fake providers.

The smoke harness itself is tested offline:

```bash
TMPDIR=/tmp .venv/bin/python -m pytest tests/test_smoke_hierarchical_preparation.py -q
```
