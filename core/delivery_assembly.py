from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import re
import subprocess

from core.delivery_plan import DeliveryPlan, DeliveryPlanIssue
from core.worktree import branch_checked_out, resolve_git_commit

_OBJECT_ID_RE = re.compile(r"^[0-9a-f]{40,64}$")


@dataclass(frozen=True)
class DeliveryAssemblyUnit:
    unit_id: str
    commit: str | None


@dataclass(frozen=True)
class DeliveryAssemblyOutcome:
    unit_id: str
    outcome: str
    result_commit: str | None
    assembled_commit: str


@dataclass(frozen=True)
class DeliveryAssemblyResult:
    success: bool
    branch: str
    base_commit: str
    assembled_commit: str | None
    outcomes: list[DeliveryAssemblyOutcome] = field(default_factory=list)
    failed_unit_id: str | None = None
    error: DeliveryPlanIssue | None = None


def recorded_delivery_assembly_conflict_issue(
    project_root: Path,
    *,
    branch: str,
    assembly_status: str | None,
    assembly_error_code: str | None,
    assembled_commit: str | None,
    failed_unit_id: str | None,
    failed_unit_commit: str | None,
) -> DeliveryPlanIssue | None:
    if assembly_status != "failed" or assembly_error_code != "delivery.assembly_conflict":
        return None

    root = project_root.resolve()
    branch_commit = _branch_commit(root, branch)
    resolved_assembled = resolve_git_commit(root, assembled_commit)[0] if assembled_commit else None
    resolved_failed_unit = resolve_git_commit(root, failed_unit_commit)[0] if failed_unit_commit else None
    conflict_resolved = (
        branch_commit is not None
        and resolved_assembled is not None
        and resolved_failed_unit is not None
        and branch_commit != resolved_assembled
        and _is_ancestor(root, resolved_assembled, branch_commit)
        and _is_ancestor(root, resolved_failed_unit, branch_commit)
    )
    if conflict_resolved:
        return None

    unit_detail = f" at unit {failed_unit_id}" if failed_unit_id else ""
    return DeliveryPlanIssue(
        "error",
        "delivery.assembly_conflict",
        (
            f"Delivery assembly remains blocked by the recorded merge conflict{unit_detail}; "
            f"resolve it on {branch} before retrying."
        ),
    )


def ordered_delivery_assembly_units(
    plan: DeliveryPlan,
    completed_commits: dict[str, str | None],
) -> list[DeliveryAssemblyUnit]:
    units_by_id = {unit.id: unit for unit in plan.units}
    ordered: list[DeliveryAssemblyUnit] = []
    visited: set[str] = set()

    def visit(unit_id: str) -> None:
        if unit_id in visited or unit_id not in completed_commits:
            return
        visited.add(unit_id)
        unit = units_by_id.get(unit_id)
        if unit is None or unit.superseded:
            return
        for dependency in unit.depends_on:
            visit(dependency)
        ordered.append(DeliveryAssemblyUnit(unit_id, completed_commits[unit_id]))

    for unit in plan.units:
        visit(unit.id)
    return ordered


def assemble_delivery_commits(
    project_root: Path,
    *,
    plan_id: str,
    branch: str,
    base_commit: str,
    expected_commit: str | None,
    units: list[DeliveryAssemblyUnit],
) -> DeliveryAssemblyResult:
    root = project_root.resolve()
    preflight = preview_delivery_assembly(
        root,
        branch=branch,
        base_commit=base_commit,
        expected_commit=expected_commit,
        units=units,
    )
    if not preflight.success:
        return preflight
    base = preflight.base_commit
    current = _branch_commit(root, branch)
    if current is None:
        if not _update_ref(root, branch, base, None):
            return _failure(
                branch,
                base,
                None,
                "delivery.assembly_branch_update_failed",
                "Git refused to create the delivery assembly branch.",
            )
        current = base
    elif not expected_commit and _is_ancestor(root, current, base) and current != base:
        if not _update_ref(root, branch, base, current):
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch changed while it was being initialized.",
            )
        current = base

    outcomes: list[DeliveryAssemblyOutcome] = []
    for unit in units:
        if not unit.commit:
            outcomes.append(DeliveryAssemblyOutcome(unit.unit_id, "no_op", None, current))
            continue

        result_commit, _ = resolve_git_commit(root, unit.commit)
        if result_commit is None:
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_unit_commit_missing",
                f"Delivery unit {unit.unit_id} result commit is missing.",
                failed_unit_id=unit.unit_id,
                outcomes=outcomes,
            )
        if _is_ancestor(root, result_commit, current):
            outcomes.append(DeliveryAssemblyOutcome(unit.unit_id, "already_applied", result_commit, current))
            continue
        if _is_ancestor(root, current, result_commit):
            if not _update_ref(root, branch, result_commit, current):
                return _failure(
                    branch,
                    base,
                    current,
                    "delivery.assembly_branch_diverged",
                    "Delivery assembly branch changed while it was being updated.",
                    failed_unit_id=unit.unit_id,
                    outcomes=outcomes,
                )
            current = result_commit
            outcomes.append(DeliveryAssemblyOutcome(unit.unit_id, "fast_forward", result_commit, current))
            continue

        merged_commit, merge_error = _merge_commits(root, plan_id, unit.unit_id, current, result_commit)
        if merge_error is not None:
            return DeliveryAssemblyResult(
                success=False,
                branch=branch,
                base_commit=base,
                assembled_commit=current,
                outcomes=outcomes,
                failed_unit_id=unit.unit_id,
                error=merge_error,
            )
        if merged_commit is None or not _update_ref(root, branch, merged_commit, current):
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch changed while it was being updated.",
                failed_unit_id=unit.unit_id,
                outcomes=outcomes,
            )
        current = merged_commit
        outcomes.append(DeliveryAssemblyOutcome(unit.unit_id, "merged", result_commit, current))

    return DeliveryAssemblyResult(
        success=True,
        branch=branch,
        base_commit=base,
        assembled_commit=current,
        outcomes=outcomes,
    )


def preview_delivery_assembly(
    project_root: Path,
    *,
    branch: str,
    base_commit: str,
    expected_commit: str | None,
    units: list[DeliveryAssemblyUnit],
) -> DeliveryAssemblyResult:
    root = project_root.resolve()
    base, _ = resolve_git_commit(root, base_commit)
    if base is None:
        return _failure(
            branch, base_commit, None, "delivery.assembly_base_missing", "Delivery assembly base commit is missing."
        )
    if not _valid_branch_name(root, branch):
        return _failure(
            branch,
            base,
            None,
            "delivery.assembly_branch_invalid",
            "Delivery final_branch is not a valid local branch name.",
        )
    if delivery_assembly_branch_is_symbolic(root, branch):
        return _failure(
            branch,
            base,
            None,
            "delivery.assembly_branch_symbolic",
            "Delivery final_branch must be a direct ref, not a symbolic ref.",
        )

    current = _branch_commit(root, branch)
    if current is not None and branch_checked_out(root, branch):
        return _failure(
            branch,
            base,
            current,
            "delivery.assembly_branch_checked_out",
            "Delivery assembly branch is checked out; switch that worktree away before retrying.",
        )
    if expected_commit:
        expected, _ = resolve_git_commit(root, expected_commit)
        if expected is None:
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_expected_commit_missing",
                "Recorded delivery assembly commit is missing.",
            )
        if current is None:
            return _failure(
                branch,
                base,
                None,
                "delivery.assembly_branch_missing",
                "Delivery assembly branch is missing after assembly progress was recorded.",
            )
        if not _is_ancestor(root, expected, current):
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch diverged from recorded assembly progress.",
            )
    elif current is not None:
        branch_contains_base = _is_ancestor(root, base, current)
        if branch_contains_base and current != base:
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch is ahead of the base without recorded assembly progress.",
            )
        if not branch_contains_base and not _is_ancestor(root, current, base):
            return _failure(
                branch,
                base,
                current,
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch does not share the recorded assembly history.",
            )

    resolved_units: list[tuple[DeliveryAssemblyUnit, str | None]] = []
    for unit in units:
        resolved_commit = resolve_git_commit(root, unit.commit)[0] if unit.commit else None
        if unit.commit and resolved_commit is None:
            return _failure(
                branch,
                base,
                current or base,
                "delivery.assembly_unit_commit_missing",
                f"Delivery unit {unit.unit_id} result commit is missing.",
                failed_unit_id=unit.unit_id,
            )
        resolved_units.append((unit, resolved_commit))

    initialized_commit = current or base
    if current is not None and not expected_commit and current != base and _is_ancestor(root, current, base):
        initialized_commit = base

    prospective_commit = initialized_commit
    for unit, resolved_commit in resolved_units:
        if resolved_commit is None or _is_ancestor(root, resolved_commit, prospective_commit):
            continue
        if _is_ancestor(root, prospective_commit, resolved_commit):
            prospective_commit = resolved_commit
            continue
        if not _merge_tree_write_tree_supported(root, base):
            return _failure(
                branch,
                base,
                current or base,
                "delivery.assembly_git_unsupported",
                (
                    "Delivery assembly requires Git 2.38 or newer with "
                    "git merge-tree --write-tree support when unit commits need a merge."
                ),
                failed_unit_id=unit.unit_id,
            )
        break
    return DeliveryAssemblyResult(
        success=True,
        branch=branch,
        base_commit=base,
        assembled_commit=initialized_commit,
    )


def _merge_commits(
    root: Path,
    plan_id: str,
    unit_id: str,
    current: str,
    result_commit: str,
) -> tuple[str | None, DeliveryPlanIssue | None]:
    try:
        merge = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--no-messages", current, result_commit],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None, DeliveryPlanIssue(
            "error",
            "delivery.assembly_git_failed",
            "Git could not evaluate the delivery unit merge.",
        )
    if merge.returncode == 1:
        return None, DeliveryPlanIssue(
            "error",
            "delivery.assembly_conflict",
            (
                f"Delivery unit {unit_id} conflicts with the assembled branch. "
                "Resolve the merge on the delivery final_branch, then rerun the delivery command."
            ),
        )
    tree = merge.stdout.strip().splitlines()[0] if merge.returncode == 0 and merge.stdout.strip() else ""
    if not _OBJECT_ID_RE.fullmatch(tree):
        return None, DeliveryPlanIssue(
            "error",
            "delivery.assembly_git_failed",
            "Git could not produce an assembled delivery tree.",
        )

    message = f"sikula: assemble delivery unit {unit_id}\n\nPlan: {plan_id}\nUnit result: {result_commit}"
    try:
        commit = subprocess.run(
            [
                "git",
                "-c",
                "user.name=Sikula",
                "-c",
                "user.email=sikula@localhost",
                "commit-tree",
                tree,
                "-p",
                current,
                "-p",
                result_commit,
                "-m",
                message,
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None, DeliveryPlanIssue(
            "error",
            "delivery.assembly_git_failed",
            "Git could not create the delivery merge commit.",
        )
    merged_commit = commit.stdout.strip().splitlines()[0] if commit.returncode == 0 and commit.stdout.strip() else ""
    if not _OBJECT_ID_RE.fullmatch(merged_commit):
        return None, DeliveryPlanIssue(
            "error",
            "delivery.assembly_git_failed",
            "Git could not create the delivery merge commit.",
        )
    return merged_commit, None


def _failure(
    branch: str,
    base_commit: str,
    assembled_commit: str | None,
    code: str,
    message: str,
    *,
    failed_unit_id: str | None = None,
    outcomes: list[DeliveryAssemblyOutcome] | None = None,
) -> DeliveryAssemblyResult:
    return DeliveryAssemblyResult(
        success=False,
        branch=branch,
        base_commit=base_commit,
        assembled_commit=assembled_commit,
        outcomes=list(outcomes or []),
        failed_unit_id=failed_unit_id,
        error=DeliveryPlanIssue("error", code, message),
    )


def _valid_branch_name(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "check-ref-format", f"refs/heads/{branch}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def delivery_assembly_branch_is_symbolic(root: Path, branch: str) -> bool:
    result = subprocess.run(
        ["git", "symbolic-ref", "--quiet", f"refs/heads/{branch}"],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _merge_tree_write_tree_supported(root: Path, commit: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "merge-tree", "--write-tree", "--no-messages", commit, commit],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    tree = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else ""
    return bool(_OBJECT_ID_RE.fullmatch(tree))


def _branch_commit(root: Path, branch: str) -> str | None:
    commit, _ = resolve_git_commit(root, f"refs/heads/{branch}")
    return commit


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def _update_ref(root: Path, branch: str, target: str, expected: str | None) -> bool:
    if branch_checked_out(root, branch) or delivery_assembly_branch_is_symbolic(root, branch):
        return False
    expected_oid = expected if expected is not None else "0" * len(target)
    result = subprocess.run(
        ["git", "update-ref", "--no-deref", f"refs/heads/{branch}", target, expected_oid],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
