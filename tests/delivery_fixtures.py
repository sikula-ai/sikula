"""Current delivery authority used by tests of scheduling and recovery."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path


def delivery_source(root: Path) -> dict[str, str]:
    text = "# Delivery\n\nImplement the behavior described by the delivery unit contracts.\n"
    path = root / ".sikula/tasks/delivery-source.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return {"path": path.relative_to(root).as_posix(), "sha256": "sha256:" + sha256(text.encode()).hexdigest()}


def bind_delivery_child(state, root: Path, *, source_task=None, write_paths=("src/",), preserve_scope=False) -> None:
    """Capture current authority for an explicitly linked synthetic test child."""
    from core.delivery_constraint_context import delivery_constraint_context_fingerprint
    from core.delivery_write_scope import resolve_delivery_write_scope

    assert state.delivery_plan_id and state.delivery_unit_id and state.delivery_plan_path
    state.delivery_constraint_context_schema_version = 1
    state.delivery_source_task = source_task or delivery_source(root)
    state.delivery_inherited_constraints = []
    state.delivery_constraint_context_fingerprint = delivery_constraint_context_fingerprint(
        schema_version=1,
        plan_id=state.delivery_plan_id,
        unit_id=state.delivery_unit_id,
        plan_path=state.delivery_plan_path,
        source_task=state.delivery_source_task,
        constraints=[],
    )
    if not preserve_scope:
        scope = resolve_delivery_write_scope(
            project_root=root, configured_write_paths=list(write_paths), unit_scope_paths=None
        )
        state.delivery_write_scope_schema_version = scope.schema_version
        state.delivery_write_scope_mode = scope.mode
        state.delivery_declared_write_paths = list(scope.declared_paths)
        state.delivery_declared_write_exact_file_paths = list(scope.declared_exact_file_paths)
        state.delivery_effective_write_paths = list(scope.effective_paths)
        state.delivery_effective_write_exact_file_paths = list(scope.effective_exact_file_paths)
    state.delivery_handoff_schema_version = 1


def assemble_delivery_fixture(plan_path: Path):
    """Run the real assembly step for a synthetic completed-unit fixture."""
    from core.delivery_finalize import assemble_delivery_candidate
    from core.delivery_progress import (
        delivery_events_path,
        delivery_progress_path,
        get_delivery_status,
        read_delivery_progress,
    )

    status = get_delivery_status(plan_path)
    root = Path(status.project_root)
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    return assemble_delivery_candidate(
        root=root,
        status=status,
        progress=progress,
        progress_path=progress_path,
        events_path=delivery_events_path(root, status.plan.plan_id),
    )


def record_verified_delivery(plan_path: Path, cfg: dict) -> None:
    """Persist a synthetic passing gate bound to the actual assembled candidate.

    Tests of finalization use this instead of calling a provider; no production
    validation or finalization checks are mocked.
    """
    from dataclasses import asdict
    from core.delivery_progress import (
        delivery_progress_path,
        get_delivery_status,
        mark_delivery_verification,
        write_delivery_progress,
    )
    from core.delivery_verification import build_delivery_verification_identity
    from core.delivery_verification_model import DeliveryVerificationRecord

    progress, commit, error = assemble_delivery_fixture(plan_path)
    assert error is None, error
    status = get_delivery_status(plan_path)
    identity = build_delivery_verification_identity(status, cfg, candidate_commit=commit)
    record = DeliveryVerificationRecord(
        schema_version=1,
        **asdict(identity),
        status="passed",
        attempt=1,
        semantic_status="approved",
    )
    write_delivery_progress(
        delivery_progress_path(Path(status.project_root), status.plan.plan_id),
        mark_delivery_verification(progress, record),
    )


def link_delivery_parent(state, root: Path) -> None:
    """Create the current parent link for a synthetic interrupted child."""
    import json
    import yaml
    from core.delivery_progress import delivery_progress_path

    state.delivery_plan_path = state.delivery_plan_path or ".sikula/delivery/resume/plan.yaml"
    source = delivery_source(root)
    path = root / state.delivery_plan_path
    path.parent.mkdir(parents=True, exist_ok=True)
    task = path.parent / "unit.md"
    task.write_text(state.task_description, encoding="utf-8")
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 2,
                "plan_id": state.delivery_plan_id,
                "title": "Resume",
                "source_task": source,
                "verification": {"mode": "final_gate"},
                "final_branch": "sikula/delivery/resume",
                "units": [
                    {
                        "id": state.delivery_unit_id,
                        "title": "Resume unit",
                        "task_path": task.relative_to(root).as_posix(),
                        "depends_on": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    progress = delivery_progress_path(root, state.delivery_plan_id)
    progress.parent.mkdir(parents=True, exist_ok=True)
    progress.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plan_id": state.delivery_plan_id,
                "units": [{"unit_id": state.delivery_unit_id, "status": "running", "child_task_id": state.task_id}],
            }
        )
    )
    # Preserve the captured scope under test, including the immutable runtime binding.
    from core.delivery_constraint_context import delivery_constraint_context_fingerprint

    state.delivery_constraint_context_schema_version = 1
    state.delivery_source_task = source
    state.delivery_inherited_constraints = []
    state.delivery_constraint_context_fingerprint = delivery_constraint_context_fingerprint(
        schema_version=1,
        plan_id=state.delivery_plan_id,
        unit_id=state.delivery_unit_id,
        plan_path=state.delivery_plan_path,
        source_task=source,
        constraints=[],
    )
    state.delivery_handoff_schema_version = 1


def record_completed_child_handoffs(plan_path: Path, root: Path, store) -> None:
    """Publish current handoffs for synthetic completed children already in progress."""
    from dataclasses import replace
    from core.delivery_handoff import (
        build_delivery_unit_handoff,
        delivery_unit_handoff_path,
        write_delivery_unit_handoff,
    )
    from core.delivery_progress import (
        delivery_progress_path,
        get_delivery_status,
        read_delivery_progress,
        write_delivery_progress,
    )

    status = get_delivery_status(plan_path, project_root=root)
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    handoffs = {}
    for unit in status.units:
        if unit.status != "done" or unit.child_task_id is None:
            continue
        child = store.load(unit.child_task_id)
        handoff = build_delivery_unit_handoff(
            plan_id=status.plan.plan_id,
            selected_unit=unit,
            child_task_id=unit.child_task_id,
            child_state=child,
        )
        write_delivery_unit_handoff(delivery_unit_handoff_path(root, status.plan.plan_id, unit.id), handoff)
        handoffs[unit.id] = handoff
    write_delivery_progress(
        progress_path,
        replace(
            progress,
            units=[
                replace(unit, handoff_schema_version=1, handoff_fingerprint=handoffs[unit.unit_id].fingerprint)
                if unit.unit_id in handoffs
                else unit
                for unit in progress.units
            ],
        ),
    )
