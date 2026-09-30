"""Checkpoint scheduling and historical handoff validity (never root approval)."""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from hashlib import sha256
from pathlib import Path, PurePosixPath
import posixpath
import subprocess
from typing import Any

from core.delivery_checkpoint_model import DeliveryCheckpoint, checkpoint_guarded_units
from core.delivery_plan import DeliveryPlan, DeliveryPlanIssue
from core.delivery_progress import DeliveryStatusResult
from core.delivery_verification_scope import DeliveryVerificationScope
from core.state import StateStore
from core.worktree import delivery_verification_git_env


def verification_node_status(status: DeliveryStatusResult, node_id: str = "root") -> DeliveryStatusResult:
    if node_id == "root":
        return status
    if status.plan is None or not any(item.id == node_id for item in getattr(status.plan, "checkpoints", ())):
        raise ValueError("Unknown checkpoint")
    record = status.checkpoint_verifications.get(node_id)
    return replace(
        status,
        verification_node=node_id,
        verification=record,
        verification_status=record.status if record else "pending",
    )


def reconcile_checkpoint_assembly(status: DeliveryStatusResult, *, persist: bool = False) -> DeliveryStatusResult:
    """Recover assembly before testing historical handoffs; never run providers."""
    from core.delivery_assembly import delivery_branch_commit
    from core.delivery_finalize import assemble_delivery_candidate, preview_delivery_assembly_issue
    from core.delivery_progress import (
        DeliveryProgressLockError,
        acquire_delivery_progress_lock,
        delivery_events_path,
        get_delivery_status,
        read_delivery_progress,
    )

    if (
        not status.valid
        or not status.plan
        or not getattr(status.plan, "checkpoints", ())
        or status.assembly_status != "failed"
    ):
        return status
    root = Path(status.project_root)

    def blocked(current, issue):
        return replace(current, errors=[*current.errors, issue])

    if not persist:
        issue = preview_delivery_assembly_issue(root, status, detect_conflicts=True)
        if issue is not None:
            return blocked(status, issue)
        candidate = delivery_branch_commit(root, status.plan.final_branch)
        if candidate is None:
            return blocked(
                status,
                DeliveryPlanIssue(
                    "error", "delivery.assembly_branch_missing", "Restore the assembly branch before continuing."
                ),
            )
        return with_checkpoint_barriers(
            replace(status, assembled_commit=candidate, assembly_status="ready", checkpoint_handoffs=None)
        )

    try:
        with acquire_delivery_progress_lock(root, status.plan.plan_id, owner="delivery.checkpoint.assembly"):
            current = get_delivery_status(status.plan_path, project_root=root)
            if not current.valid or current.assembly_status != "failed":
                return current
            if current.plan.plan_id != status.plan.plan_id:
                return blocked(
                    current,
                    DeliveryPlanIssue(
                        "error",
                        "delivery.assembly_plan_changed",
                        "Delivery plan identity changed before assembly recovery.",
                    ),
                )
            issue = preview_delivery_assembly_issue(root, current, detect_conflicts=True)
            if issue is not None:
                return blocked(current, issue)
            progress_path = Path(current.progress_path)
            progress, errors = read_delivery_progress(progress_path, plan_id=current.plan.plan_id)
            if progress is None or errors:
                return replace(
                    current,
                    errors=[
                        *current.errors,
                        *(
                            errors
                            or [
                                DeliveryPlanIssue(
                                    "error", "delivery.progress_missing", "Delivery progress is unavailable."
                                )
                            ]
                        ),
                    ],
                )
            _, _, issue = assemble_delivery_candidate(
                root=root,
                status=current,
                progress=progress,
                progress_path=progress_path,
                events_path=delivery_events_path(root, current.plan.plan_id),
                git_env=delivery_verification_git_env(),
            )
            current = get_delivery_status(status.plan_path, project_root=root)
            return blocked(current, issue) if issue is not None else current
    except DeliveryProgressLockError:
        return blocked(
            status, DeliveryPlanIssue("error", "delivery.locked", "Delivery progress is locked by another operation.")
        )


def verification_scope_complete(status: DeliveryStatusResult) -> bool:
    if status.plan is None:
        return False
    scope = DeliveryVerificationScope.from_plan(status.plan, status.verification_node)
    done = {unit.id for unit in status.units if unit.status == "done"}
    return bool(scope.unit_ids) and set(scope.unit_ids) <= done


def checkpoint_policy_payload(status: DeliveryStatusResult, scope: DeliveryVerificationScope) -> dict[str, Any]:
    from core.delivery_amendment import _read_assembly_contract

    assert status.project_root is not None and status.plan is not None
    root = Path(status.project_root)
    contracts = {
        unit.id: sha256(_read_assembly_contract(root, unit.task_path, private_artifact_roots=())).hexdigest()
        for unit in status.plan.units
        if unit.id in scope.unit_ids
    }
    context = scope.plan_context()
    # Ownership added to a later group cannot change this group's inherited rule.
    for constraint in context["constraints"]:
        constraint["unit_ids"] = [key for key in constraint["unit_ids"] if key in scope.unit_ids]
    return {
        "scope": context,
        "contracts": contracts,
        "security_required": scope.security_required,
        "policy": scope.policy.to_dict() if scope.policy else {},
    }


def checkpoint_pass_is_usable(
    status: DeliveryStatusResult, checkpoint: DeliveryCheckpoint, cfg: dict[str, Any] | None = None
) -> bool:
    from core.delivery_verification import build_delivery_verification_identity
    from core.delivery_verification_model import delivery_verification_covers_obligations
    from core.delivery_assembly import delivery_branch_commit

    record = status.checkpoint_verifications.get(checkpoint.id)
    if (
        not status.valid
        or record is None
        or not record.passed
        or not status.project_root
        or not status.assembled_commit
    ):
        return False
    node = verification_node_status(status, checkpoint.id)
    if not verification_scope_complete(node) or not delivery_verification_covers_obligations(
        record, len(checkpoint.obligation_ids)
    ):
        return False
    try:
        if record.review_rule_fingerprints is None:
            return False
        root = Path(status.project_root)
        for commit in (record.candidate_commit, status.assembled_commit):
            if (
                checkpoint_review_rule_fingerprints(root, commit, record.review_rule_fingerprints)
                != record.review_rule_fingerprints
            ):
                return False
        identity = build_delivery_verification_identity(node, cfg or {}, candidate_commit=record.candidate_commit)
        keys = ("candidate_tree", "source_fingerprint", "completed_scope_fingerprint", "policy_fingerprint")
        if any(getattr(record, key) != getattr(identity, key) for key in keys):
            return False
        if cfg is not None and record.config_fingerprint != identity.config_fingerprint:
            return False
        if (
            status.assembly_status != "ready"
            or delivery_branch_commit(root, status.plan.final_branch) != status.assembled_commit
        ):
            return False
        # A receipt is a historical group handoff, not approval of later commits.
        return (
            subprocess.run(
                ["git", "merge-base", "--is-ancestor", record.candidate_commit, status.assembled_commit],
                cwd=root,
                env=delivery_verification_git_env(),
                capture_output=True,
                check=False,
            ).returncode
            == 0
        )
    except (OSError, RuntimeError, ValueError):
        return False


def checkpoint_review_rule_fingerprints(root: Path, commit: str, paths: Collection[str]) -> dict[str, str]:
    """Hash candidate rule contents, resolving only bounded, project-internal links."""
    from core.delivery_verification import MAX_DELIVERY_VERIFICATION_PACKET_BYTES

    if not paths:
        return {}

    def git(*args: str) -> bytes:
        result = subprocess.run(
            ["git", *args], cwd=root, env=delivery_verification_git_env(), capture_output=True, check=False
        )
        if result.returncode:
            raise ValueError("Checkpoint review rules are unavailable")
        return result.stdout

    result = {}
    prefix = git("rev-parse", "--show-prefix").decode("utf-8").rstrip("\n")

    def blob(oid: str) -> bytes:
        if int(git("cat-file", "-s", oid)) > MAX_DELIVERY_VERIFICATION_PACKET_BYTES:
            raise ValueError("Checkpoint review rule exceeds the bounded packet")
        return git("cat-file", "blob", oid)

    def contents(path: str) -> bytes:
        pending = list(PurePosixPath(path).parts)
        resolved: list[str] = []
        links = 0
        while pending:
            resolved.append(pending.pop(0))
            name = prefix + "/".join(resolved)
            entry = git("--literal-pathspecs", "ls-tree", "--full-tree", "-z", commit, "--", name)
            metadata, separator, entry_name = entry.rstrip(b"\0").partition(b"\t")
            fields = metadata.split()
            if not separator or entry_name.decode("utf-8") != name or len(fields) != 3:
                raise ValueError("Checkpoint review rule is unavailable")
            mode, kind, oid = fields
            if mode == b"120000" and kind == b"blob":
                links += 1
                target = blob(oid.decode("ascii")).decode("utf-8")
                if links > 40 or not target or target.startswith("/") or "\\" in target or ":" in target:
                    raise ValueError("Checkpoint review rule link is unsafe")
                target = posixpath.normpath(posixpath.join(*resolved[:-1], target))
                if target == ".." or target.startswith("../"):
                    raise ValueError("Checkpoint review rule leaves the project")
                pending = list(PurePosixPath(target).parts) + pending
                resolved = []
            elif pending and mode == b"040000" and kind == b"tree":
                continue
            elif not pending and mode in {b"100644", b"100755"} and kind == b"blob":
                return blob(oid.decode("ascii"))
            else:
                raise ValueError("Checkpoint review rule is not a candidate file")
        raise ValueError("Checkpoint review rule is not a candidate file")

    for path in paths:
        if (
            not isinstance(path, str)
            or not path
            or PurePosixPath(path).is_absolute()
            or ".." in PurePosixPath(path).parts
            or "\\" in path
            or ":" in path
        ):
            raise ValueError("Checkpoint review rule path is invalid")
        result[path] = "sha256:" + sha256(contents(path)).hexdigest()
    return result


def due_delivery_checkpoint(status: DeliveryStatusResult, cfg: dict[str, Any] | None = None) -> str | None:
    if not status.valid or status.plan is None:
        return None
    for checkpoint in getattr(status.plan, "checkpoints", ()):
        if verification_scope_complete(
            verification_node_status(status, checkpoint.id)
        ) and not checkpoint_pass_is_usable(status, checkpoint, cfg):
            return checkpoint.id
    return None


def checkpoint_barrier_issue(
    status: DeliveryStatusResult, cfg: dict[str, Any] | None = None, *, unit_id: str | None = None
) -> DeliveryPlanIssue | None:
    if not status.valid or status.plan is None:
        return None
    for checkpoint in getattr(status.plan, "checkpoints", ()):
        if checkpoint_pass_is_usable(status, checkpoint, cfg):
            continue
        guarded = checkpoint_guarded_units(checkpoint, status.plan.units)
        if any(unit.id in guarded and unit.status not in {"pending", "blocked", "superseded"} for unit in status.units):
            return DeliveryPlanIssue(
                "error",
                "delivery_checkpoint.handoff_stale",
                "A checkpoint handoff changed after downstream work started; reconcile its authority before continuing.",
            )
        if unit_id in guarded or (
            unit_id is None and verification_scope_complete(verification_node_status(status, checkpoint.id))
        ):
            return DeliveryPlanIssue(
                "error",
                "delivery_checkpoint.required",
                "A checkpoint must pass before downstream work; continue with delivery run.",
            )
    return None


def checkpoint_projection(status: DeliveryStatusResult) -> list[dict[str, Any]]:
    if status.plan is None:
        return []
    result = []
    for checkpoint in getattr(status.plan, "checkpoints", ()):
        record = status.checkpoint_verifications.get(checkpoint.id)
        usable = (
            checkpoint.id in status.checkpoint_handoffs
            if status.checkpoint_handoffs is not None
            else checkpoint_pass_is_usable(status, checkpoint)
        )
        state = (
            "accepted_handoff"
            if usable
            else "stale"
            if record and record.passed
            else record.status
            if record
            else "due"
            if verification_scope_complete(verification_node_status(status, checkpoint.id))
            else "pending"
        )
        item = {
            "id": checkpoint.id,
            "status": state,
            "unit_count": len(checkpoint.unit_ids),
            "obligation_count": len(checkpoint.obligation_ids),
        }
        if record:
            item.update(attempt=record.attempt, candidate_commit=record.candidate_commit)
            if record.stop_code:
                item["stop_code"] = record.stop_code
        result.append(item)
    return result


def checkpoint_preflight_issue(
    status: DeliveryStatusResult, cfg: dict[str, Any], state_store: StateStore | None = None
) -> DeliveryPlanIssue | None:
    """Read-only prerequisites shared by dry-run, direct verification and run."""
    from core.delivery_amendment import _configured_private_artifact_roots, _read_assembly_contract
    from core.delivery_handoff import load_delivery_dependency_handoffs
    from core.delivery_verification import check_delivery_verification_readiness

    readiness = check_delivery_verification_readiness(status, cfg)
    if not readiness.ready:
        return readiness.errors[0]
    if status.plan is None or status.project_root is None:
        return DeliveryPlanIssue(
            "error", "delivery_checkpoint.evidence_unavailable", "Checkpoint authority is unavailable."
        )
    if any(item.kind == "stop_and_follow_up" for item in status.plan.constraints):
        return DeliveryPlanIssue(
            "error",
            "delivery_checkpoint.prerequisite_stop",
            "A required prerequisite must be resolved before checkpoint review.",
        )
    if any(unit.status not in {"pending", "done", "superseded"} for unit in status.units):
        return DeliveryPlanIssue(
            "error",
            "delivery_checkpoint.unit_not_complete",
            "An active child must be resolved before checkpoint review.",
        )
    barrier = checkpoint_barrier_issue(status, cfg)
    if barrier is not None and barrier.code != "delivery_checkpoint.required":
        return barrier
    from core.delivery_verification import with_delivery_verification_readiness
    from core.delivery_verification_model import delivery_verification_recovery_action

    current = with_delivery_verification_readiness(status, cfg)
    record = current.verification
    if record is not None and record.status in {"blocked", "failed"}:
        if record.security_status == "rejected":
            return DeliveryPlanIssue(
                "error",
                "delivery_verification.security_rejected",
                "Checkpoint security review retains a terminal boundary.",
            )
        if (
            record.stop_code
            and record.stop_code != "delivery_verification.repair_required"
            and delivery_verification_recovery_action(record.stop_code) != "retry_delivery_verification"
            and (
                current.verification_status != "stale"
                or delivery_verification_recovery_action(record.stop_code) != "restart_with_candidate_config"
            )
        ):
            return DeliveryPlanIssue("error", record.stop_code, "Checkpoint retains a required recovery boundary.")
    scope = DeliveryVerificationScope.from_plan(status.plan, status.verification_node)
    root = Path(status.project_root)
    _, issues = load_delivery_dependency_handoffs(status, list(scope.unit_ids), root)
    if issues:
        return issues[0]
    try:
        private_roots = _configured_private_artifact_roots(root, cfg)
        prefix = (
            subprocess.run(
                ["git", "rev-parse", "--show-prefix"],
                cwd=root,
                env=delivery_verification_git_env(),
                capture_output=True,
                check=True,
            )
            .stdout.decode("utf-8")
            .rstrip("\n")
        )
        for unit in status.units:
            if unit.id not in scope.unit_ids:
                continue
            if unit.status != "done":
                raise ValueError("Incomplete checkpoint input")
            content = _read_assembly_contract(root, unit.task_path, private_artifact_roots=private_roots).decode(
                "utf-8"
            )
            if unit.child_task_id:
                child = state_store.load(unit.child_task_id) if state_store else None
                if (
                    child is None
                    or child.result_commit != unit.commit
                    or child.delivery_plan_id != status.plan.plan_id
                    or child.delivery_unit_id != unit.id
                ):
                    raise ValueError("Missing executed contract identity")
                executed = child.task_description
            else:
                result = subprocess.run(
                    ["git", "show", f"{unit.commit or status.assembly_base_commit}:{prefix}{unit.task_path}"],
                    cwd=root,
                    env=delivery_verification_git_env(),
                    capture_output=True,
                    check=True,
                )
                executed = result.stdout.decode("utf-8")
            if (
                not isinstance(executed, str)
                or executed.replace("\r\n", "\n").replace("\r", "\n").strip()
                != content.replace("\r\n", "\n").replace("\r", "\n").strip()
            ):
                raise ValueError("Executed contract changed")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError):
        return DeliveryPlanIssue(
            "error",
            "delivery_checkpoint.evidence_unavailable",
            "Completed checkpoint contracts must match immutable execution evidence.",
        )
    return None


def with_checkpoint_barriers(status: DeliveryStatusResult, cfg: dict[str, Any] | None = None) -> DeliveryStatusResult:
    if status.plan is None or not status.plan.checkpoints:
        return status
    usable = frozenset(item.id for item in status.plan.checkpoints if checkpoint_pass_is_usable(status, item, cfg))
    guarded = {
        item.id: checkpoint_guarded_units(item, status.plan.units)
        for item in status.plan.checkpoints
        if item.id not in usable
    }
    units = [
        replace(unit, blocked_by_checkpoints=[key for key, members in guarded.items() if unit.id in members])
        for unit in status.units
    ]
    return replace(
        status,
        units=units,
        checkpoint_handoffs=usable,
        next_action="continue checkpoint verification or recovery with delivery run"
        if any(verification_scope_complete(verification_node_status(status, key)) for key in guarded)
        else status.next_action,
    )


def checkpoint_repair_control_issues(plan: DeliveryPlan, root: Path) -> list[DeliveryPlanIssue]:
    """The later-contributor exception belongs only to a published coordinator repair."""
    from core.delivery_repair import _read_state

    for unit in plan.units:
        if unit.repair_node is None:
            continue
        try:
            control = _read_state(root, plan.plan_id, unit.repair_node)
            if (
                control is None
                or control["phase"] not in {"prepared", "published"}
                or control.get("unit") != unit.to_authoring_dict()
            ):
                raise ValueError("Unbound repair contribution")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            return [
                DeliveryPlanIssue(
                    "error",
                    "delivery_checkpoint.repair_unbound",
                    "A checkpoint repair declaration must match its durable coordinator publication evidence.",
                )
            ]
    return []
