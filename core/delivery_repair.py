"""Bounded integration recovery through one appended, normally executed delivery unit."""

from __future__ import annotations

from collections.abc import Callable
import copy
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, NoReturn

import yaml

from agents.delivery_repair_agent import (
    DeliveryRepairAgent,
    DeliveryRepairAuthoringError,
    build_delivery_repair_prompt,
)
from core.contract_check import check_contract
from core.delivery_asset_assignment import DeliveryAssetAssignmentError, render_inherited_delivery_assets
from core.delivery_handoff import load_delivery_dependency_handoffs
from core.delivery_amendment import (
    DeliveryAmendmentError,
    _atomic_replace_if_unchanged,
    _atomic_write_new,
    _configured_private_artifact_roots,
    _ensure_parent,
    _read_assembly_contract,
    _validate_amendment_plan_destination,
    _validate_new_task_target,
)
from core.delivery_assembly import (
    DeliveryAssemblyArtifact,
    assemble_delivery_artifacts,
    delivery_branch_commit,
    find_delivery_artifact_commit,
    preview_delivery_artifacts,
)
from core.delivery_plan import DeliveryPlanIssue, check_delivery_plan_data
from core.delivery_progress import (
    DeliveryProgressLockError,
    DeliveryStatusResult,
    acquire_delivery_progress_lock,
    delivery_progress_path,
    get_delivery_status,
    mark_delivery_assembly,
    read_delivery_progress,
    write_delivery_progress,
)
from core.delivery_repair_input import load_repair_input, repair_content_fingerprint, repair_input_policy_changed
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification import (
    build_delivery_verification_identity,
    check_delivery_verification_readiness,
)
from core.delivery_verification_model import parse_delivery_verification_record
from core.delivery_verification_review import DeliveryIntegrationAssessment
from core.delivery_verify import (
    _append_audit,
    _candidate_config_matches_capture,
    _candidate_is_clean,
    _review_snapshot,
    _review_workspace_unchanged,
)
from core.delivery_write_scope import apply_delivery_write_scope_to_config, resolve_delivery_write_scope
from core.llm_client import LLMReadOnlyViolation
from core.state import StateStore, TaskState
from core.validation_coverage import validation_coverage_gaps
from core.worktree import detached_delivery_verification_worktree


_MAX_AUTHORING_ATTEMPTS = 2
_MAX_CONTRACT_BYTES = 128 * 1024
_TERMINAL_STOPS = frozenset(
    {
        "delivery_repair.external_dependency_gap",
        "delivery_repair.scope_amendment_required",
        "delivery_repair.human_review_required",
        "delivery_repair.evidence_unavailable",
        "delivery_repair.security_stop",
        "delivery_repair.readonly_mutation",
        "delivery_repair.audit_unavailable",
    }
)


@dataclass(frozen=True)
class DeliveryRepairResult:
    ready: bool = False
    unit_id: str | None = None
    issue: DeliveryPlanIssue | None = None


class DeliveryRepairError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.issue = DeliveryPlanIssue("error", code, message)


def _stop(code: str, message: str) -> NoReturn:
    raise DeliveryRepairError("delivery_repair." + code, message)


def _state_path(root: Path, plan_id: str) -> Path:
    return delivery_progress_path(root, plan_id).parent / "integration-repair.json"


def _read_state(root: Path, plan_id: str) -> dict[str, Any] | None:
    state = read_repair_state(root, _state_path(root, plan_id))
    if state is None:
        return None
    if (
        type(state.get("schema_version")) is not int
        or state.get("schema_version") != 1
        or state.get("plan_id") != plan_id
        or state.get("phase") not in {"authoring", "prepared", "published", "blocked"}
        or type(state.get("attempts")) is not int
        or not 0 <= state["attempts"] <= _MAX_AUTHORING_ATTEMPTS
    ):
        _stop("state_invalid", "Integration repair control state is invalid.")
    parse_delivery_verification_record(state.get("verification"))
    if any(
        not isinstance(state.get(key), str) or not state[key] for key in ("config_fingerprint", "unit_id", "created_at")
    ):
        _stop("state_invalid", "Integration repair control identity is invalid.")
    if state["phase"] in {"prepared", "published"}:
        if (
            any(not isinstance(state.get(key), str) for key in ("source_plan", "task_markdown", "authored_markdown"))
            or any(
                not isinstance(state.get(key), dict)
                for key in ("unit", "contracts", "completed_progress", "child_evidence")
            )
            or not isinstance(state.get("obligation_ids"), list)
            or any(not isinstance(item, str) for item in state["obligation_ids"])
            or any(not isinstance(value, str) for value in state["contracts"].values())
            or any(not isinstance(value, str) for value in state["child_evidence"].values())
        ):
            _stop("state_invalid", "Prepared integration repair control state is invalid.")
    if state["phase"] == "blocked" and state.get("stop_code") not in _TERMINAL_STOPS:
        _stop("state_invalid", "Integration repair terminal state is invalid.")
    return state


def delivery_repair_pending(path: str | Path, *, project_root: Path | None) -> bool:
    """Inspect only typed control state, without authoring or publication."""
    status = get_delivery_status(path, project_root=project_root)
    if status.plan is None or status.project_root is None or not getattr(status.plan, "plan_id", None):
        return False
    try:
        state = _read_state(Path(status.project_root), status.plan.plan_id)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        # Route malformed control state through the coordinator's fail-closed result.
        return True
    return bool(state and state["phase"] in {"authoring", "prepared", "blocked"})


def delivery_repair_input_needs_refresh(status: DeliveryStatusResult, cfg: dict[str, Any]) -> bool:
    """Refresh changed repair policy through a new gate only before preparation starts.

    The caller must first establish that the gate identity is current. Existing
    control state, including terminal stops and consumed budgets, remains binding.
    """
    record = status.verification
    if (
        record is None
        or record.status != "failed"
        or record.stop_code != "delivery_verification.repair_required"
        or not record.repair_input_fingerprint
        or status.plan is None
        or status.project_root is None
    ):
        return False
    root = Path(status.project_root)
    try:
        return _read_state(root, status.plan.plan_id) is None and repair_input_policy_changed(
            root, delivery_progress_path(root, status.plan.plan_id).parent, record, status.plan, cfg
        )
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        # Corrupt/missing evidence must retain its blocker, not trigger another gate.
        return False


def _current_input(status: DeliveryStatusResult, cfg: dict[str, Any]) -> DeliveryIntegrationAssessment:
    if not status.valid or status.status != "done" or status.plan is None or status.project_root is None:
        _stop("plan_not_complete", "Integration repair requires a valid completed delivery candidate.")
    plan = status.plan
    if not plan.requires_final_verification or not plan.obligations or not plan.source_accounting:
        _stop("authority_required", "Automatic integration repair requires source-bound obligations and accounting.")
    if any(constraint.kind == "stop_and_follow_up" for constraint in plan.constraints):
        _stop("prerequisite_stop", "A required external input remains unresolved; repair cannot proceed.")
    readiness = check_delivery_verification_readiness(status, cfg)
    if not readiness.ready:
        _stop("readiness_blocked", "Integration repair requires current verification readiness.")
    record = status.verification
    if (
        record is None
        or record.status != "failed"
        or record.stop_code != "delivery_verification.repair_required"
        or record.semantic_status != "rejected"
        or record.security_status not in {"not_run", "approved"}
        or not record.repair_input_fingerprint
    ):
        _stop("input_unavailable", "No eligible structured semantic repair input is available.")
    root = Path(status.project_root)
    identity = build_delivery_verification_identity(status, cfg, candidate_commit=record.candidate_commit)
    if (
        any(getattr(record, key) != value for key, value in asdict(identity).items())
        or status.assembled_commit != record.candidate_commit
        or status.assembly_status != "ready"
        or delivery_branch_commit(root, plan.final_branch) != record.candidate_commit
    ):
        _stop("stale", "The candidate or its authority changed after integration review.")
    assessment = load_repair_input(root, delivery_progress_path(root, plan.plan_id).parent, record, plan, cfg)
    gaps = {item.id for item in assessment.obligation_results if item.outcome != "satisfied"}
    if (
        assessment.disposition != "repair_required"
        or not gaps
        or any(
            not finding.obligation_ids or not set(finding.obligation_ids).issubset(gaps)
            for finding in assessment.findings
        )
    ):
        _stop("unsupported_findings", "Integration findings do not identify a supported source-bound repair.")
    return assessment


def coordinate_delivery_repair(
    path: str | Path,
    cfg: dict[str, Any],
    *,
    project_root: Path | None,
    agent_factory: Callable[[], DeliveryRepairAgent] | None,
    state_store: StateStore | None = None,
    dry_run: bool = False,
) -> DeliveryRepairResult:
    """Prepare/publish at most one repair; all implementation stays in run-next.

    The delivery lock spans the bounded authoring operation. Recovery never replays
    audit records, and a durable reservation precedes each provider attempt.
    """
    status = get_delivery_status(path, project_root=project_root)
    if not status.plan or not status.project_root:
        return DeliveryRepairResult(
            issue=DeliveryPlanIssue("error", "delivery_repair.plan_invalid", "Delivery repair requires a valid plan.")
        )
    root = Path(status.project_root)
    # Preserve the validated identity through preparation, publication, and resume.
    plan_path = Path(status.plan_path)
    try:
        if dry_run:
            return _coordinate(
                plan_path, root, status.plan.plan_id, cfg, agent_factory=None, state_store=state_store, dry_run=True
            )
        with acquire_delivery_progress_lock(root, status.plan.plan_id, owner="delivery.repair"):
            return _coordinate(
                plan_path,
                root,
                status.plan.plan_id,
                cfg,
                agent_factory=agent_factory,
                state_store=state_store,
                dry_run=False,
            )
    except DeliveryProgressLockError:
        return DeliveryRepairResult(
            issue=DeliveryPlanIssue("error", "delivery.locked", "Delivery progress is locked by another process.")
        )
    except DeliveryRepairError as exc:
        return DeliveryRepairResult(issue=exc.issue)
    except DeliveryAmendmentError as exc:
        return DeliveryRepairResult(issue=exc.issue)
    except (OSError, RuntimeError, ValueError, TypeError, KeyError):
        return DeliveryRepairResult(
            issue=DeliveryPlanIssue(
                "error",
                "delivery_repair.evidence_unavailable",
                "Integration repair could not validate or persist required local evidence.",
            )
        )


def _coordinate(
    path: Path,
    root: Path,
    plan_id: str,
    cfg: dict[str, Any],
    *,
    agent_factory: Callable[[], DeliveryRepairAgent] | None,
    state_store: StateStore | None,
    dry_run: bool,
) -> DeliveryRepairResult:
    state_path = _state_path(root, plan_id)
    audit_path = state_path.with_name("integration-repair.jsonl")
    state = _read_state(root, plan_id)
    status = get_delivery_status(path, project_root=root)
    if state is not None:
        if state["phase"] == "blocked":
            raise DeliveryRepairError(
                state["stop_code"],
                "Integration repair retains a terminal blocker; resolve its recorded boundary before further work.",
            )
        if state["phase"] == "published":
            _stop("budget_exhausted", "The plan's single automatic integration repair has already been published.")
        if state["config_fingerprint"] != repair_content_fingerprint(cfg):
            _stop("stale", "Effective integration repair policy changed after preparation started.")
        if state["phase"] == "prepared":
            _assert_child_evidence(state.get("child_evidence", {}), state_store)
            return _publish(path, root, cfg, state, state_store=state_store, dry_run=dry_run)

    assessment = _current_input(status, cfg)
    assert status.plan is not None and status.verification is not None
    child_evidence = _capture_child_evidence(status, state_store)
    if state is not None and state["verification"] != status.verification.to_dict():
        _stop("stale", "Integration repair input changed after preparation started.")
    if state is not None and state["attempts"] >= _MAX_AUTHORING_ATTEMPTS:
        _stop("authoring_budget_exhausted", "Integration repair exhausted its persistent authoring budget.")
    if not dry_run and agent_factory is None:
        _stop("context_unavailable", "Integration repair authoring context is unavailable.")
    if state is None:
        state = {
            "schema_version": 1,
            "plan_id": plan_id,
            "phase": "authoring",
            "attempts": 0,
            "verification": status.verification.to_dict(),
            "config_fingerprint": repair_content_fingerprint(cfg),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "unit_id": "integration-repair-" + status.verification.gate_id[-16:],
        }
    with detached_delivery_verification_worktree(
        root, status.verification.candidate_commit, preview=dry_run
    ) as worktree:
        if not _candidate_config_matches_capture(root, worktree, cfg):
            _stop("config_changed", "Repair candidate configuration differs from the loaded policy.")
        packet, unit, contracts = _repair_packet(status, root, worktree, cfg, assessment, state["unit_id"], state_store)
        _assert_dependency_handoffs(status, unit["depends_on"], root)
        _assert_child_evidence(child_evidence, state_store)
        _check_repair_prompt(packet)
        source_plan = Path(status.plan_path).read_bytes().decode("utf-8")
        if "sha256:" + sha256(source_plan.encode("utf-8")).hexdigest() != status.verification.plan_fingerprint:
            _stop("stale", "The delivery plan changed before repair preflight.")
        _appended_plan(
            yaml.safe_load(source_plan),
            unit,
            [item.id for item in assessment.obligation_results if item.outcome != "satisfied"],
            root,
            cfg,
            status.plan_path,
        )
        if dry_run:
            return DeliveryRepairResult(ready=True)
        assert agent_factory is not None
        agent = agent_factory()
        try:
            agent.prepare_workspace(worktree)
        except LLMReadOnlyViolation:
            state.update(phase="blocked", stop_code="delivery_repair.readonly_mutation")
            write_repair_state(root, state_path, state)
            _stop("readonly_mutation", "Repair workspace preparation reported a read-only boundary violation.")
        finally:
            if not _candidate_is_clean(worktree, status.verification.candidate_commit):
                state.update(phase="blocked", stop_code="delivery_repair.readonly_mutation")
                write_repair_state(root, state_path, state)
                _stop("readonly_mutation", "Repair workspace preparation changed its candidate.")
        before = _review_snapshot(worktree, cfg)
        while state["attempts"] < _MAX_AUTHORING_ATTEMPTS:
            current = get_delivery_status(path, project_root=root)
            _current_input(current, cfg)
            if current.verification != status.verification:
                _stop("stale", "Integration repair input changed before an authoring attempt.")
            _assert_child_evidence(child_evidence, state_store)
            _assert_dependency_handoffs(current, unit["depends_on"], root)
            _assert_contracts_current(root, contracts, _configured_private_artifact_roots(root, cfg))
            _check_repair_prompt(packet)
            # Freeze policy with the first reserved attempt, after all prerequisites
            # pass. Boundary violations above still persist terminal state.
            state["attempts"] += 1
            write_repair_state(root, state_path, state)

            def record(value: dict[str, Any]) -> None:
                audit_record = {**value, "gate_id": status.verification.gate_id, "attempt": state["attempts"]}
                try:
                    _append_audit(audit_path, audit_record, project_root=root)
                except OSError:
                    state.update(phase="blocked", stop_code="delivery_repair.audit_unavailable")
                    try:
                        write_repair_state(root, state_path, state)
                    finally:
                        # Preserve the failed append separately from control data.
                        # It is diagnostic evidence, never an input to recovery.
                        write_repair_state(
                            root,
                            state_path.with_name("integration-repair-pending-audit.json"),
                            {"schema_version": 1, "record": audit_record},
                        )
                    _stop("audit_unavailable", "Integration repair stopped because its audit could not be appended.")

            try:
                try:
                    draft = agent.author(cwd=worktree, packet=packet, audit_recorder=record)
                finally:
                    _assert_readonly(
                        worktree, cfg, before, status.verification.candidate_commit, root, state_path, state
                    )
            except DeliveryRepairAuthoringError as exc:
                if exc.code in _TERMINAL_STOPS:
                    state.update(phase="blocked", stop_code=exc.code)
                    write_repair_state(root, state_path, state)
                if exc.code != "delivery_repair.output_invalid" or state["attempts"] == _MAX_AUTHORING_ATTEMPTS:
                    raise DeliveryRepairError(
                        exc.code, "Integration repair authoring failed; its bounded evidence is retained."
                    ) from None
                packet["previous_error"] = exc.code
                continue
            if draft.disposition != "repair":
                state.update(phase="blocked", stop_code="delivery_repair." + draft.disposition)
                write_repair_state(root, state_path, state)
                _stop(
                    draft.disposition,
                    "Integration repair identified a required external, scope, evidence, or security boundary.",
                )
            assert draft.task_markdown is not None
            try:
                markdown = _inherit_assets(
                    draft.task_markdown, list(packet["unit_contracts"].values()), worktree, unit["id"]
                )
                _validate_contract(markdown, unit["task_path"], cfg, asset_root=worktree)
            except DeliveryRepairError as exc:
                if state["attempts"] == _MAX_AUTHORING_ATTEMPTS:
                    raise
                packet["previous_error"] = exc.issue.message
                continue
            current = get_delivery_status(path, project_root=root)
            _current_input(current, cfg)
            if current.verification != status.verification:
                _stop("stale", "Integration repair input changed during authoring.")
            state.update(
                phase="prepared",
                source_plan=Path(status.plan_path).read_bytes().decode("utf-8"),
                authored_markdown=draft.task_markdown,
                task_markdown=markdown,
                unit=unit,
                contracts=contracts,
                obligation_ids=[item.id for item in assessment.obligation_results if item.outcome != "satisfied"],
                completed_progress=_progress_snapshot(root, plan_id),
                child_evidence=child_evidence,
            )
            _assert_child_evidence(child_evidence, state_store)
            _prepared_plan(state, root, cfg, status.plan_path, asset_root=worktree)
            write_repair_state(root, state_path, state)
            break
    return _publish(path, root, cfg, state, state_store=state_store, dry_run=False)


def _assert_dependency_handoffs(status: DeliveryStatusResult, depends_on: list[str], root: Path) -> None:
    if status.plan is None:
        _stop("evidence_unavailable", "Required dependency handoff evidence could not be validated.")
    _, errors = load_delivery_dependency_handoffs(status, depends_on, root)
    if errors:
        raise DeliveryRepairError(errors[0].code, errors[0].message)


def _check_repair_prompt(packet: dict[str, Any]) -> None:
    try:
        build_delivery_repair_prompt(packet)
    except DeliveryRepairAuthoringError as exc:
        raise DeliveryRepairError(
            exc.code, "The integration repair prompt exceeds the bounded authoring context."
        ) from None


def _assert_readonly(
    worktree: Path, cfg: dict[str, Any], before: Any, commit: str, root: Path, path: Path, state: dict[str, Any]
) -> None:
    try:
        unchanged = _review_workspace_unchanged(worktree, cfg, before) and _candidate_is_clean(worktree, commit)
    except (OSError, RuntimeError, ValueError):
        state.update(phase="blocked", stop_code="delivery_repair.evidence_unavailable")
        write_repair_state(root, path, state)
        _stop("evidence_unavailable", "Read-only authoring could not preserve required workspace evidence.")
    if not unchanged:
        state.update(phase="blocked", stop_code="delivery_repair.readonly_mutation")
        write_repair_state(root, path, state)
        _stop("readonly_mutation", "Read-only integration repair authoring modified its candidate workspace.")


def _repair_packet(
    status: DeliveryStatusResult,
    root: Path,
    worktree: Path,
    cfg: dict[str, Any],
    assessment: DeliveryIntegrationAssessment,
    unit_id: str,
    state_store: StateStore | None,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    plan = status.plan
    assert plan is not None and plan.source_task is not None and status.verification is not None
    verification_scope = DeliveryVerificationScope.from_plan(plan)
    plan_context = verification_scope.plan_context()
    if any(unit.id == unit_id for unit in plan.units):
        _stop(
            "unit_conflict", "The deterministic integration repair unit already exists without matching control state."
        )
    affected_obligations = {item.id for item in assessment.obligation_results if item.outcome != "satisfied"}
    owners = {unit_id for item in plan.obligations if item.id in affected_obligations for unit_id in item.unit_ids}
    scope_paths: list[str] = []
    for unit in plan.units:
        if unit.id in owners:
            owner_config = copy.deepcopy(cfg)
            owner_config["project"]["root_path"] = str(worktree)
            completed = next(item for item in status.units if item.id == unit.id)
            if completed.child_task_id:
                if state_store is None:
                    _stop("child_evidence_unavailable", "Completed child scope evidence is unavailable.")
                child = state_store.load(completed.child_task_id)
                if child is None:
                    _stop("child_evidence_unavailable", "Completed child scope evidence is unavailable.")
                apply_delivery_write_scope_to_config(owner_config, child)
            scope = resolve_delivery_write_scope(
                project_root=worktree,
                configured_write_paths=owner_config.get("sandbox", {}).get("allowed_write_paths", []),
                unit_scope_paths=unit.scope_paths,
            )
            scope_paths.extend(scope.effective_paths)
    if not scope_paths:
        _stop("scope_unavailable", "No existing authorized production scope is available for the repair.")
    private_roots = _configured_private_artifact_roots(root, cfg)
    contracts: dict[str, str] = {}
    total_bytes = 0
    for item in plan.units:
        content = _read_assembly_contract(root, item.task_path, private_artifact_roots=private_roots)
        total_bytes += len(content)
        if len(content) > _MAX_CONTRACT_BYTES or total_bytes > 512 * 1024:
            _stop("hierarchy_required", "A required unit contract exceeds the bounded recovery context.")
        contracts[item.task_path] = content.decode("utf-8")
        completed = next(unit for unit in status.units if unit.id == item.id)
        if completed.status == "done":
            if completed.child_task_id:
                child = state_store.load(completed.child_task_id) if state_store is not None else None
                if child is None or not isinstance(child.task_description, str) or not child.task_description.strip():
                    _stop("child_evidence_unavailable", "Completed child contract evidence is unavailable.")
                # Run captures read_text().strip(), including universal newline normalization.
                executed = child.task_description.replace("\r\n", "\n").replace("\r", "\n").strip()
                current = contracts[item.task_path].replace("\r\n", "\n").replace("\r", "\n").strip()
                if current != executed:
                    _stop("contract_evidence_changed", "A completed unit contract differs from its executed task.")
            elif not (worktree / item.task_path).is_file():
                _stop("child_evidence_unavailable", "Completed unit contract authority is unavailable.")
    task_path = (Path(status.plan_path).parent / "units" / (unit_id + ".md")).relative_to(root).as_posix()
    _validate_amendment_plan_destination(Path(status.plan_path), root, private_artifact_roots=private_roots)
    _validate_new_task_target(Path(status.plan_path), root, private_artifact_roots=private_roots)
    _validate_new_task_target(root / task_path, root, private_artifact_roots=private_roots)
    if (root / task_path).exists():
        _stop("task_conflict", "The integration repair task path is already occupied.")
    source_artifacts = [
        DeliveryAssemblyArtifact(relative, text.encode("utf-8"), expected_content=text.encode("utf-8"))
        for relative, text in contracts.items()
    ]
    plan_bytes = Path(status.plan_path).read_bytes()
    source_artifacts.append(
        DeliveryAssemblyArtifact(
            Path(status.plan_path).relative_to(root).as_posix(),
            plan_bytes,
            expected_content=plan_bytes,
        )
    )
    preview = preview_delivery_artifacts(
        root, branch=plan.final_branch, parent_commit=status.verification.candidate_commit, artifacts=source_artifacts
    )
    if not preview.success:
        _stop("contract_evidence_changed", "Required delivery contracts differ from the reviewed assembly.")
    unit = {
        "id": unit_id,
        "title": "Repair delivery integration",
        "task_path": task_path,
        "depends_on": list(verification_scope.unit_ids),
        "scope_paths": list(dict.fromkeys(scope_paths)),
        "estimated_size": "small",
        "budget": {"max_planner_steps": 1},
    }
    source = (root / plan.source_task.path).read_text(encoding="utf-8")
    owner_contracts = {item.id: contracts[item.task_path] for item in plan.units if item.id in owners}
    # Validate inherited declarations and candidate availability before authoring.
    inherited = _inherit_assets("", list(owner_contracts.values()), worktree, unit_id)
    asset_check = check_contract(
        inherited,
        source_path=task_path,
        project_config=cfg,
        asset_project_config={"project": {"root_path": str(worktree)}},
        document_kind="implementation_contract",
    )
    if any(gap.severity == "blocking" and gap.id.startswith("gap.assets.") for gap in asset_check.gaps):
        _stop("contract_not_ready", "Inherited repair assets are not ready in the reviewed candidate.")
    packet = {
        "source_task": source,
        "plan": plan_context,
        "findings": assessment.to_dict(),
        "repair_unit": unit,
        "unit_contracts": owner_contracts,
        "inherited_constraints": plan_context["constraints"],
        "validation_policy": {
            key: cfg.get(key) for key in ("build", "run_presync", "run_build", "run_tests", "run_checks")
        },
        "allowed_read_paths": cfg.get("sandbox", {}).get("allowed_read_paths", ["."]),
    }
    return packet, unit, contracts


def _inherit_assets(markdown: str, contracts: list[str], root: Path, unit_id: str) -> str:
    try:
        return render_inherited_delivery_assets(markdown, inherited_tasks=contracts, project_root=root, unit_id=unit_id)
    except DeliveryAssetAssignmentError:
        _stop("contract_not_ready", "Repair assets must preserve inherited declarations without authored replacements.")


def _validate_contract(markdown: str, task_path: str, cfg: dict[str, Any], *, asset_root: Path) -> None:
    from core.delivery_authoring import DeliveryAuthoringParseError, _validate_unit_task_markdown

    try:
        _validate_unit_task_markdown(markdown, allow_asset_manifest=True)
    except DeliveryAuthoringParseError as exc:
        _stop("contract_not_ready", exc.message)
    result = check_contract(
        markdown,
        source_path=task_path,
        project_config=cfg,
        asset_project_config={"project": {"root_path": str(asset_root)}},
        document_kind="implementation_contract",
    )
    if validation_coverage_gaps(cfg, TaskState(task_id="delivery-repair-readiness", task_description=markdown)):
        _stop(
            "contract_not_ready",
            "Repair verification commands must match enabled validation phases, not disabled configuration.",
        )
    if any(gap.severity == "blocking" for gap in result.gaps):
        gaps = ", ".join(gap.id for gap in result.gaps if gap.severity == "blocking")
        _stop("contract_not_ready", "The integration repair contract has blocking readiness gaps: " + gaps)


def _progress_snapshot(root: Path, plan_id: str) -> dict[str, Any]:
    progress, errors = read_delivery_progress(delivery_progress_path(root, plan_id), plan_id=plan_id)
    if progress is None or errors:
        _stop("progress_invalid", "Integration repair requires readable delivery progress.")
    return progress.to_dict()


def _assert_contracts_current(root: Path, contracts: dict[str, str], private_roots: tuple[Path, ...]) -> None:
    for relative, text in contracts.items():
        if _read_assembly_contract(root, relative, private_artifact_roots=private_roots) != text.encode("utf-8"):
            _stop("stale", "A completed unit contract changed after repair preparation.")


def _child_fingerprint(child: TaskState) -> str:
    if not child.done or child.failed or child.delivery_stop_code or child.delivery_scope_audit_pending:
        _stop("child_boundary_stop", "A completed child retains a terminal boundary or unresolved execution state.")
    return repair_content_fingerprint(
        {
            "task_description": child.task_description,
            "commit": child.result_commit,
            "plan_id": child.delivery_plan_id,
            "unit_id": child.delivery_unit_id,
            "plan_path": child.delivery_plan_path,
            "scope_schema": child.delivery_write_scope_schema_version,
            "scope_mode": child.delivery_write_scope_mode,
            "declared": child.delivery_declared_write_paths,
            "declared_exact": child.delivery_declared_write_exact_file_paths,
            "effective": child.delivery_effective_write_paths,
            "effective_exact": child.delivery_effective_write_exact_file_paths,
            "binding": child.delivery_runtime_write_scope_binding,
        }
    )


def _capture_child_evidence(status: DeliveryStatusResult, store: StateStore | None) -> dict[str, str]:
    evidence: dict[str, str] = {}
    for unit in status.units:
        if unit.status != "done" or not unit.child_task_id:
            continue
        child = store.load(unit.child_task_id) if store is not None else None
        if (
            child is None
            or child.result_commit != unit.commit
            or child.delivery_plan_id != status.plan.plan_id
            or child.delivery_unit_id != unit.id
        ):
            _stop("child_evidence_unavailable", "Completed child identity or result evidence is unavailable.")
        evidence[unit.child_task_id] = _child_fingerprint(child)
    return evidence


def _assert_child_evidence(evidence: dict[str, str], store: StateStore | None) -> None:
    for task_id, fingerprint in evidence.items():
        child = store.load(task_id) if store is not None else None
        if child is None or _child_fingerprint(child) != fingerprint:
            _stop("child_evidence_unavailable", "Completed child authority changed during repair preparation.")


def _prepared_plan(
    state: dict[str, Any], root: Path, cfg: dict[str, Any], plan_path: str, *, asset_root: Path
) -> bytes:
    data = yaml.safe_load(state["source_plan"])
    if not isinstance(data, dict) or not isinstance(data.get("units"), list):
        _stop("state_invalid", "Stored integration repair plan is invalid.")
    record = parse_delivery_verification_record(state["verification"])
    if "sha256:" + sha256(state["source_plan"].encode("utf-8")).hexdigest() != record.plan_fingerprint:
        _stop("state_invalid", "Stored integration repair authority does not match its gate.")
    unit = state["unit"]
    unit_id = state["unit_id"]
    expected_path = (Path(plan_path).parent / "units" / (unit_id + ".md")).relative_to(root).as_posix()
    if (
        unit_id != "integration-repair-" + record.gate_id[-16:]
        or unit.get("id") != unit_id
        or unit.get("task_path") != expected_path
    ):
        _stop("state_invalid", "Stored integration repair identity is invalid.")
    owners = {
        owner
        for obligation in data.get("obligations", [])
        if obligation["id"] in state["obligation_ids"]
        for owner in obligation["unit_ids"]
    }
    contracts = [state["contracts"][item["task_path"]] for item in data["units"] if item["id"] in owners]
    if _inherit_assets(state["authored_markdown"], contracts, asset_root, unit_id) != state["task_markdown"]:
        _stop("state_invalid", "Prepared repair assets differ from inherited authority.")
    _validate_contract(state["task_markdown"], unit["task_path"], cfg, asset_root=asset_root)
    return _appended_plan(data, unit, state["obligation_ids"], root, cfg, plan_path)


def _appended_plan(
    data: dict[str, Any],
    unit: dict[str, Any],
    obligation_ids: list[str],
    root: Path,
    cfg: dict[str, Any],
    plan_path: str,
) -> bytes:
    """Build and preflight the exact enlarged plan independently of authored Markdown."""
    unit_id = unit["id"]
    # Private JSON snapshots sort object keys; publication bytes must be identical
    # both before and after loading a prepared snapshot on resume.
    data["units"].append({key: unit[key] for key in sorted(unit)})
    # YAML aliases can share ownership lists; extend each field independently.
    for constraint in data.get("constraints") or []:
        constraint["unit_ids"] = [*constraint["unit_ids"], unit_id]
    for obligation in data.get("obligations", []):
        if obligation["id"] in obligation_ids:
            obligation["unit_ids"] = [*obligation["unit_ids"], unit_id]
    check = check_delivery_plan_data(
        data, project_root=root, plan_path=plan_path, virtual_task_paths={unit["task_path"]}
    )
    if not check.valid:
        _stop("plan_invalid", "The appended repair does not form a valid delivery plan.")
    plan_bytes = yaml.safe_dump(data, sort_keys=False, allow_unicode=True).encode("utf-8")
    future = replace(check, plan_bytes=len(plan_bytes), plan_fingerprint="sha256:" + sha256(plan_bytes).hexdigest())
    if not check_delivery_verification_readiness(future, cfg).ready:
        _stop("hierarchy_required", "The repaired plan cannot fit its final verification gate.")
    return plan_bytes


def _publish(
    plan_path: Path,
    root: Path,
    cfg: dict[str, Any],
    state: dict[str, Any],
    *,
    state_store: StateStore | None,
    dry_run: bool,
) -> DeliveryRepairResult:
    private_roots = _configured_private_artifact_roots(root, cfg)
    _validate_amendment_plan_destination(plan_path, root, private_artifact_roots=private_roots)
    _validate_new_task_target(plan_path, root, private_artifact_roots=private_roots)
    _assert_dependency_handoffs(get_delivery_status(plan_path, project_root=root), state["unit"]["depends_on"], root)
    record = parse_delivery_verification_record(state["verification"])
    # Partial publication may already have moved the assembly ref. Asset authority
    # still belongs to the exact candidate captured by the original gate.
    with detached_delivery_verification_worktree(root, record.candidate_commit, preview=True) as worktree:
        if not _candidate_config_matches_capture(root, worktree, cfg):
            _stop("config_changed", "Repair candidate configuration differs from the loaded policy.")
        content = _prepared_plan(state, root, cfg, str(plan_path), asset_root=worktree)
    unit_id = state["unit_id"]
    task_path = root / state["unit"]["task_path"]
    _validate_new_task_target(task_path, root, private_artifact_roots=private_roots)
    plan_before = state["source_plan"].encode("utf-8")
    current_plan = plan_path.read_bytes()
    if current_plan not in (plan_before, content):
        _stop("stale", "The delivery plan changed after repair preparation.")
    _assert_contracts_current(root, state["contracts"], private_roots)
    markdown = state["task_markdown"].encode("utf-8")
    if task_path.exists() and task_path.read_bytes() != markdown:
        _stop("task_conflict", "The integration repair task path contains different content.")
    old_progress = state["completed_progress"]
    parent = record.candidate_commit
    plan_data = yaml.safe_load(state["source_plan"])
    branch = plan_data["final_branch"]
    artifacts = [
        DeliveryAssemblyArtifact(plan_path.relative_to(root).as_posix(), content, expected_content=plan_before)
    ]
    artifacts.extend(
        DeliveryAssemblyArtifact(relative, text.encode("utf-8"), expected_content=text.encode("utf-8"))
        for relative, text in state["contracts"].items()
    )
    artifacts.append(DeliveryAssemblyArtifact(state["unit"]["task_path"], markdown, must_not_exist=True))
    commit, current_branch = find_delivery_artifact_commit(
        root, branch=branch, parent_commit=parent, proposal_id=unit_id, artifacts=artifacts
    )
    if current_branch not in {parent, commit} or (current_branch is None):
        _stop("stale", "The delivery assembly branch changed during repair publication.")
    progress, errors = read_delivery_progress(delivery_progress_path(root, state["plan_id"]), plan_id=state["plan_id"])
    if progress is None or errors:
        _stop("progress_invalid", "Integration repair progress is unavailable.")
    if progress.to_dict() != old_progress:
        if (
            not commit
            or progress.assembled_commit != commit
            or progress.to_dict().get("units") != old_progress["units"]
        ):
            _stop("stale", "Delivery unit progress changed during repair publication.")
        expected = dict(old_progress)
        for field in (
            "assembled_commit",
            "assembly_updated_at",
            "verification",
            "final_branch",
            "final_commit",
            "finalized_at",
        ):
            expected.pop(field, None)
        observed = progress.to_dict()
        for field in (
            "assembled_commit",
            "assembly_updated_at",
            "verification",
            "final_branch",
            "final_commit",
            "finalized_at",
        ):
            observed.pop(field, None)
        if observed != expected:
            _stop("stale", "Delivery progress changed during repair publication.")
    if current_plan == plan_before and commit is None:
        _current_input(get_delivery_status(plan_path, project_root=root), cfg)
    else:
        source = plan_data["source_task"]
        if (
            "sha256:" + sha256((root / source["path"]).read_text(encoding="utf-8").encode("utf-8")).hexdigest()
            != record.source_fingerprint
        ):
            _stop("stale", "Source authority changed during repair publication.")
    if commit is None:
        preview = preview_delivery_artifacts(root, branch=branch, parent_commit=parent, artifacts=artifacts)
        if not preview.success:
            raise DeliveryRepairError(preview.error.code, preview.error.message)
    if dry_run:
        return DeliveryRepairResult(ready=True, unit_id=unit_id)
    audit_path = _state_path(root, state["plan_id"]).with_name("integration-repair.jsonl")
    _append_audit(
        audit_path, {"event": "publication_started", "unit_id": unit_id, "gate_id": record.gate_id}, project_root=root
    )
    if not task_path.exists():
        _ensure_parent(task_path.parent, root)
        _atomic_write_new(task_path, markdown, mode=0o644, root=root)
    if current_plan == plan_before:
        _atomic_replace_if_unchanged(plan_path, content, expected_fingerprint=sha256(plan_before).hexdigest())
    if commit is None:
        result = assemble_delivery_artifacts(
            root,
            plan_id=state["plan_id"],
            proposal_id=unit_id,
            branch=branch,
            parent_commit=parent,
            artifacts=artifacts,
            created_at=state["created_at"],
        )
        if not result.success or not result.assembled_commit:
            raise DeliveryRepairError(result.error.code, result.error.message)
        commit = result.assembled_commit
    if (
        delivery_branch_commit(root, branch) != commit
        or plan_path.read_bytes() != content
        or task_path.read_bytes() != markdown
    ):
        _stop("stale", "Repair artifacts changed during publication; durable recovery evidence is retained.")
    # Revalidate authority after assembly as well as before writes. Prepared state
    # remains pending if any input raced publication; a child cannot start from it.
    current = get_delivery_status(plan_path, project_root=root)
    if not current.valid or _progress_snapshot(root, state["plan_id"]) != progress.to_dict():
        _stop("stale", "Source or progress changed during integration repair publication.")
    _assert_contracts_current(root, state["contracts"], private_roots)
    _assert_child_evidence(state["child_evidence"], state_store)
    _assert_dependency_handoffs(current, state["unit"]["depends_on"], root)
    repaired_progress = mark_delivery_assembly(
        progress, base_commit=old_progress["assembly_base_commit"], assembled_commit=commit, status="ready"
    )
    write_delivery_progress(delivery_progress_path(root, state["plan_id"]), repaired_progress)
    _append_audit(audit_path, {"event": "published", "unit_id": unit_id, "commit": commit}, project_root=root)
    state.update(phase="published", publication_commit=commit)
    write_repair_state(root, _state_path(root, state["plan_id"]), state)
    return DeliveryRepairResult(ready=True, unit_id=unit_id)
