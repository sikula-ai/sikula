from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.delivery_constraint_context import DeliveryConstraintContextError, parse_delivery_constraint_context
from core.delivery_handoff import load_delivery_dependency_handoffs
from core.delivery_progress import DeliveryStatusUnit
from core.delivery_verification_review import DeliveryIntegrationReviewParseError, parse_delivery_integration_review
from core.delivery_write_scope import DeliveryWriteScopeError, apply_delivery_write_scope_to_config
from core.state import JsonStateStore, TaskState
from sikula import cmd_run
from tests.test_sikula_run import _run_args, _run_cfg
from tests.delivery_fixtures import bind_delivery_child, link_delivery_parent


def test_released_030_standalone_state_loads_and_resumes(tmp_path):
    # Fixture serialized by TaskState from git tag v0.3.0 (core/state.py),
    # with stable timestamps and a pending standalone implementation prompt.
    raw = json.loads((Path(__file__).parent / "fixtures/state-v0.3.0.json").read_text())
    assert "delivery_plan_id" not in raw
    store = JsonStateStore(tmp_path / ".sikula/state")
    store_path = tmp_path / ".sikula/state/released030.json"
    store_path.parent.mkdir(parents=True)
    store_path.write_text(json.dumps(raw))
    state = store.load("released030")
    assert state.implementation_prompt == raw["implementation_prompt"]
    assert parse_delivery_constraint_context(state) is None
    assert apply_delivery_write_scope_to_config(_run_cfg(tmp_path), state) is None

    def complete(task_id, **kwargs):
        loaded = store.load(task_id)
        loaded.done = True
        store.save(loaded)
        return loaded

    with patch("sikula.build_orchestrator") as factory, patch("sikula._print_task_audit_report", return_value=0):
        factory.return_value.run.side_effect = complete
        with pytest.raises(SystemExit) as result:
            cmd_run(_run_args(task_id="released030", no_isolate=True), _run_cfg(tmp_path))
    assert result.value.code == 0
    factory.return_value.run.assert_called_once()
    assert store.load("released030").done


def _remove_authority(state, missing):
    if missing == "parent":
        state.delivery_plan_path = None
    elif missing == "constraints":
        state.delivery_constraint_context_schema_version = None
        state.delivery_source_task = None
        state.delivery_constraint_context_fingerprint = None
    elif missing == "scope":
        state.delivery_write_scope_schema_version = None
        state.delivery_write_scope_mode = None
        state.delivery_declared_write_paths = []
        state.delivery_declared_write_exact_file_paths = None
        state.delivery_effective_write_paths = []
        state.delivery_effective_write_exact_file_paths = None
    else:
        state.delivery_handoff_schema_version = None


@pytest.mark.parametrize("missing", ["parent", "constraints", "scope", "handoff"])
@pytest.mark.parametrize("reset_failed", [False, True])
def test_incomplete_delivery_child_stops_before_reset_or_runtime(tmp_path, missing, reset_failed):
    state = TaskState(
        task_id="child",
        task_description="Incomplete authority",
        delivery_plan_id="demo",
        delivery_unit_id="unit",
        delivery_plan_path="plan.yaml",
        failed=reset_failed,
        worktree_path=str(tmp_path),
    )
    bind_delivery_child(state, tmp_path)
    link_delivery_parent(state, tmp_path)
    _remove_authority(state, missing)
    store = JsonStateStore(tmp_path / ".sikula/state")
    store.save(state)
    target = tmp_path / ".sikula/state/child.json"
    before = target.read_bytes()
    with patch("sikula.build_orchestrator") as factory, patch("sikula._reset_failed_state") as reset:
        with pytest.raises(SystemExit) as result:
            cmd_run(_run_args(task_id="child", reset_failed=reset_failed), _run_cfg(tmp_path))
    assert result.value.code == 1
    factory.assert_not_called()
    reset.assert_not_called()
    assert target.read_bytes() == before


def test_missing_delivery_snapshots_cannot_use_standalone_defaults(tmp_path):
    state = TaskState(task_id="child", task_description="Unit", delivery_plan_id="demo", delivery_unit_id="unit")
    with pytest.raises(DeliveryConstraintContextError):
        parse_delivery_constraint_context(state)
    with pytest.raises(DeliveryWriteScopeError, match="captured"):
        apply_delivery_write_scope_to_config(_run_cfg(tmp_path), state)


def test_completed_child_requires_handoff_but_completion_without_child_does_not(tmp_path):
    unit = DeliveryStatusUnit(
        id="unit", title="Unit", depends_on=[], status="done", task_path="unit.md", child_task_id="child"
    )
    status = SimpleNamespace(plan=SimpleNamespace(plan_id="demo"), units=[unit])
    _, errors = load_delivery_dependency_handoffs(status, ["unit"], tmp_path)
    assert [item.code for item in errors] == ["delivery.dependency_handoff_missing"]
    status.units = [DeliveryStatusUnit(id="unit", title="Unit", depends_on=[], status="done", task_path="unit.md")]
    assert load_delivery_dependency_handoffs(status, ["unit"], tmp_path) == ([], [])


def test_unreleased_review_protocol_is_rejected_even_without_obligations():
    output = json.dumps({"schema_version": 1, "disposition": "approved", "summary": "Approved.", "findings": []})
    with pytest.raises(DeliveryIntegrationReviewParseError) as error:
        parse_delivery_integration_review(output, known_unit_ids={"unit"})
    assert error.value.code == "delivery_verification.review_schema_unsupported"


@pytest.mark.parametrize("missing", ["parent", "constraints", "scope", "handoff"])
def test_delivery_resume_preview_rejects_missing_authority(tmp_path, capsys, missing):
    from tests.test_delivery_run_next import (
        _git_init,
        _write_plan,
        _write_progress,
        _resume_child_state,
        _record_resume_worktree,
        _run_next_args,
        _run_next_cfg,
        _run_next_context,
    )
    from sikula_cli.delivery import cmd_delivery_run_next

    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    state = _resume_child_state()
    _record_resume_worktree(state, tmp_path)
    _remove_authority(state, missing)
    store = JsonStateStore(tmp_path / ".sikula/state")
    store.save(state)
    _write_progress(tmp_path, [{"unit_id": "01-foundation", "status": "running", "child_task_id": state.task_id}])
    before = (tmp_path / ".sikula/state" / (state.task_id + ".json")).read_bytes()

    def forbidden(*args):
        pytest.fail("Dry-run must not resume the child")

    with pytest.raises(SystemExit) as result:
        cmd_delivery_run_next(
            _run_next_args(plan_path, dry_run=True, json_output=True),
            _run_next_cfg(tmp_path),
            _run_next_context(tmp_path, forbidden),
        )
    assert result.value.code == 1
    assert json.loads(capsys.readouterr().out)["ready"] is False
    assert (tmp_path / ".sikula/state" / (state.task_id + ".json")).read_bytes() == before
