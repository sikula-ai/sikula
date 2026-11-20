# Delivery recovery acceptance

These deterministic tests check which delivery failures Sikula can recover from
within the configured budgets, and which must stop. They use synthetic tasks,
temporary Git repositories and fake providers. They exercise coordination and
validation, not model judgment or the frequency of failures in production.

## Small CLI lifecycle cases

```bash
python -m pytest tests/e2e/test_delivery_autonomy.py -q
```

Each case runs a two-unit plan through real isolated execution, build/tests,
unit review, final verification and finalization. Only provider calls are faked.
The tests distinguish these outcomes:

| Injected fault | Expected recovery |
| --- | --- |
| The second unit breaks an existing test | Test triage identifies a production defect; the Fixer corrects it and validation passes within the same `delivery run`. No operator reset. |
| The second unit's provider call fails | The plan stops. Ordinary replay cannot restart the failed child; an explicit `delivery run --reset-failed` completes it without repeating the completed first unit. |
| Final semantic review returns an invalid response format | One protocol correction produces a valid assessment without repeating implementation. |

All three cases require finalization and a subsequent replay with no provider
calls or unit execution. The operator's HEAD and index remain unchanged.
The provider-failure case tests child recovery after a surfaced failure; it does
not test provider-specific transport retries.

## Existing coverage for the remaining boundaries

The matrix also uses these existing tests rather than adding another scenario
engine or duplicating their fixtures. Names below are pytest test selectors.

| Behavior | Coverage |
| --- | --- |
| Checkpoint repair releases consumers and reaches the final gate | `tests/e2e/test_delivery_plan.py::test_delivery_prepares_checkpoint_repairs_and_releases_consumer` |
| Final-gate repair succeeds, or exhausts its budget and stays stopped on replay | `tests/e2e/test_delivery_plan.py::test_delivery_run_repairs_source_obligation_and_reverifies` |
| Late regression, interrupted verification, repair and no-call final replay | `tests/e2e/test_delivery_capacity.py` |
| Historical evidence cannot admit an unreconciled candidate; missing/corrupt evidence blocks readiness | `tests/test_delivery_full_review_recovery.py::test_conditional_readiness_defers_only_intact_reconciled_child_admissions` |
| Completed parent rejection survives child review and resume; security/read-only boundaries remain terminal | `tests/test_delivery_full_review_recovery.py::test_full_subgroup_rejection_requests_child_without_discarding_parent` |
| Scope violations stop execution and cannot be cleared by reset | `tests/e2e/test_delivery_plan.py::test_delivery_run_next_scope_violation_stops_before_later_phases_and_blocks_reset` |
| External ownership requires a follow-up rather than automatic scope expansion | `tests/e2e/test_delivery_plan.py::test_delivery_external_dependency_stop_projects_follow_up_without_proposal` |
| Preparation protocol correction retains its inputs and finite budget | `tests/test_delivery_hierarchical_preparation.py::test_local_protocol_correction_keeps_inputs_and_finite_budget` |
| Malformed responses cannot hide readable authority mismatches or prerequisite stops | `tests/test_delivery_hierarchical_preparation.py::test_draft_correction_never_masks_unknown_authority_or_prerequisite_stop` |
| Prerequisite/read-only stops survive audit failure and resume | `tests/test_delivery_hierarchical_preparation.py::test_terminal_boundary_survives_audit_failure_and_resume` |
| Preparation rejection cannot refill its budget on resume | `tests/test_delivery_hierarchical_preparation.py::test_rejection_budget_is_not_refilled_by_resume` |

Explicit resume/reset commands count as operator intervention. Passing a resume
test does not establish unattended recovery. Exhausted repair budgets and
security, authority or prerequisite boundaries remain stops; these tests must
not raise limits or clear evidence to make a scenario finish.

Larger hierarchy measurements remain in the opt-in
[capacity experiment](hierarchical-delivery-capacity.md). Routine acceptance
uses small cases and has no machine-dependent wall-time thresholds. Generated
state, audits and experiment reports stay in temporary directories, outside the
tracked acceptance documentation.
