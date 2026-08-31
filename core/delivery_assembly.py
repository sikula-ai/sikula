from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import hashlib
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import subprocess
import tempfile
from typing import Iterator

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


@dataclass(frozen=True)
class DeliveryAssemblyArtifact:
    path: str
    content: bytes
    mode: str = "100644"
    expected_content: bytes | None = None
    expected_object_id: str | None = None
    expected_fingerprint: str | None = None
    must_not_exist: bool = False


@dataclass(frozen=True)
class DeliveryArtifactAssemblyResult:
    success: bool
    branch: str
    parent_commit: str
    assembled_commit: str | None
    previous_commit: str | None = None
    error: DeliveryPlanIssue | None = None


@dataclass
class _DeliveryArtifactGitContext:
    project_root: Path
    git_root: Path
    git_dir: Path
    common_dir: Path
    object_format: str
    parent_commit: str
    filter_root: Path
    env: dict[str, str]
    tree_entries_by_commit: dict[str, dict[bytes, tuple[str, str]]]
    index_loaded: bool = False

    @property
    def project_prefix(self) -> str:
        return self.project_root.relative_to(self.git_root).as_posix()

    def tree_entries(self, commit: str) -> dict[bytes, tuple[str, str]]:
        entries = self.tree_entries_by_commit.get(commit)
        if entries is not None:
            return entries
        entries = _load_tree_entries(self.git_root, commit, env=self.env)
        if entries is None:
            raise OSError("Git tree entries are unavailable")
        self.tree_entries_by_commit[commit] = entries
        return entries

    def ensure_index_loaded(self) -> bool:
        if self.index_loaded:
            return True
        try:
            result = subprocess.run(
                ["git", "read-tree", self.parent_commit],
                cwd=self.git_root,
                env=self.env,
                capture_output=True,
                text=True,
            )
        except OSError:
            return False
        if result.returncode != 0:
            return False
        self.index_loaded = True
        return True


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


def assemble_delivery_artifacts(
    project_root: Path,
    *,
    plan_id: str,
    proposal_id: str,
    branch: str,
    parent_commit: str,
    artifacts: list[DeliveryAssemblyArtifact],
    created_at: str,
) -> DeliveryArtifactAssemblyResult:
    """Commit exact delivery-owned artifacts and advance a direct branch ref."""
    root = project_root.resolve()
    try:
        parent, _ = resolve_git_commit(root, parent_commit)
    except OSError:
        return _artifact_failure(
            branch,
            parent_commit,
            "delivery.assembly_artifact_git_failed",
            "Git could not validate delivery amendment artifacts.",
        )
    if parent is None:
        return _artifact_failure(
            branch,
            parent_commit,
            "delivery.assembly_expected_commit_missing",
            "Recorded delivery assembly commit is missing.",
        )
    with _delivery_artifact_git_context(root, parent) as context:
        if context is None:
            return _artifact_failure(
                branch,
                parent,
                "delivery.assembly_artifact_git_failed",
                "Git could not validate delivery amendment artifacts.",
            )
        return _assemble_delivery_artifacts_with_context(
            context,
            plan_id=plan_id,
            proposal_id=proposal_id,
            branch=branch,
            artifacts=artifacts,
            created_at=created_at,
        )


def _assemble_delivery_artifacts_with_context(
    context: _DeliveryArtifactGitContext,
    *,
    plan_id: str,
    proposal_id: str,
    branch: str,
    artifacts: list[DeliveryAssemblyArtifact],
    created_at: str,
) -> DeliveryArtifactAssemblyResult:
    root = context.project_root
    parent = context.parent_commit
    git_artifacts, artifact_issue = _artifacts_relative_to_git_root(context, artifacts)
    if artifact_issue is not None:
        return DeliveryArtifactAssemblyResult(
            success=False,
            branch=branch,
            parent_commit=parent,
            assembled_commit=None,
            error=artifact_issue,
        )
    try:
        current = _branch_commit(root, branch)
        issue = _artifact_preflight_issue(context, branch, git_artifacts)
    except OSError:
        return _artifact_failure(
            branch,
            parent,
            "delivery.assembly_artifact_git_failed",
            "Git could not validate delivery amendment artifacts.",
        )
    if issue is not None:
        return DeliveryArtifactAssemblyResult(
            success=False,
            branch=branch,
            parent_commit=parent,
            assembled_commit=None,
            previous_commit=current,
            error=issue,
        )

    if current is not None and current != parent:
        try:
            branch_is_behind_parent = _is_ancestor(root, current, parent)
        except OSError:
            return _artifact_failure(
                branch,
                parent,
                "delivery.assembly_artifact_git_failed",
                "Git could not validate delivery amendment artifacts.",
            )
        if not branch_is_behind_parent:
            return _artifact_failure(
                branch,
                parent,
                "delivery.assembly_branch_diverged",
                "Delivery assembly branch changed before amendment artifacts were integrated.",
            )

    commit, issue = _create_artifact_commit(
        context,
        plan_id=plan_id,
        proposal_id=proposal_id,
        artifacts=git_artifacts,
        created_at=created_at,
    )
    if issue is not None or commit is None:
        return DeliveryArtifactAssemblyResult(
            success=False,
            branch=branch,
            parent_commit=parent,
            assembled_commit=None,
            previous_commit=current,
            error=issue,
        )
    try:
        updated = _update_ref(root, branch, commit, current)
    except OSError:
        updated = False
    if not updated:
        return _artifact_failure(
            branch,
            parent,
            "delivery.assembly_branch_diverged",
            "Delivery assembly branch changed while amendment artifacts were integrated.",
        )
    return DeliveryArtifactAssemblyResult(True, branch, parent, commit, current)


def preview_delivery_artifacts(
    project_root: Path,
    *,
    branch: str,
    parent_commit: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> DeliveryArtifactAssemblyResult:
    """Validate an artifact assembly without writing objects or updating refs."""
    root = project_root.resolve()
    try:
        parent, _ = resolve_git_commit(root, parent_commit)
    except OSError:
        return _artifact_failure(
            branch,
            parent_commit,
            "delivery.assembly_artifact_git_failed",
            "Git could not validate delivery amendment artifacts.",
        )
    if parent is None:
        return _artifact_failure(
            branch,
            parent_commit,
            "delivery.assembly_expected_commit_missing",
            "Recorded delivery assembly commit is missing.",
        )
    with _delivery_artifact_git_context(root, parent) as context:
        if context is None:
            return _artifact_failure(
                branch,
                parent,
                "delivery.assembly_artifact_git_failed",
                "Git could not validate delivery amendment artifacts.",
            )
        return _preview_delivery_artifacts_with_context(context, branch=branch, artifacts=artifacts)


def _preview_delivery_artifacts_with_context(
    context: _DeliveryArtifactGitContext,
    *,
    branch: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> DeliveryArtifactAssemblyResult:
    root = context.project_root
    parent = context.parent_commit
    git_artifacts, artifact_issue = _artifacts_relative_to_git_root(context, artifacts)
    if artifact_issue is not None:
        return DeliveryArtifactAssemblyResult(False, branch, parent, None, error=artifact_issue)
    try:
        current = _branch_commit(root, branch)
        issue = _artifact_preflight_issue(context, branch, git_artifacts)
    except OSError:
        return _artifact_failure(
            branch,
            parent,
            "delivery.assembly_artifact_git_failed",
            "Git could not validate delivery amendment artifacts.",
        )
    if issue is not None:
        return DeliveryArtifactAssemblyResult(False, branch, parent, None, current, issue)
    return DeliveryArtifactAssemblyResult(True, branch, parent, None, current)


def delivery_artifact_compatibility_issue(
    project_root: Path,
    *,
    parent_commit: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> DeliveryPlanIssue | None:
    """Validate artifact paths and expected content against their parent tree."""
    root = project_root.resolve()
    try:
        parent, _ = resolve_git_commit(root, parent_commit)
    except OSError:
        return _artifact_git_issue()
    if parent is None:
        return DeliveryPlanIssue(
            "error",
            "delivery.assembly_expected_commit_missing",
            "Recorded delivery assembly commit is missing.",
        )
    with _delivery_artifact_git_context(root, parent) as context:
        if context is None:
            return _artifact_git_issue()
        git_artifacts, artifact_issue = _artifacts_relative_to_git_root(context, artifacts)
        if artifact_issue is not None:
            return artifact_issue
        return _artifact_parent_compatibility_issue(context, git_artifacts)


def rollback_delivery_artifacts(
    project_root: Path,
    *,
    branch: str,
    assembled_commit: str,
    previous_commit: str | None,
) -> bool:
    """Move an artifact commit back only when the branch still points to it."""
    root = project_root.resolve()
    try:
        if previous_commit is None:
            return _delete_ref(root, branch, assembled_commit)
        return _update_ref(root, branch, previous_commit, assembled_commit)
    except OSError:
        return False


def delivery_commit_is_ancestor(project_root: Path, ancestor: str, descendant: str) -> bool:
    return _is_ancestor(project_root.resolve(), ancestor, descendant)


def delivery_branch_commit(project_root: Path, branch: str) -> str | None:
    return _branch_commit(project_root.resolve(), branch)


def delivery_artifact_content_id(
    project_root: Path,
    *,
    parent_commit: str,
    path: str,
    content: bytes,
) -> str | None:
    """Return the blob id produced by the parent tree's clean-filter rules."""
    root = project_root.resolve()
    with _delivery_artifact_git_context(root, parent_commit) as context:
        if context is None:
            return None
        artifacts, issue = _artifacts_relative_to_git_root(
            context,
            [DeliveryAssemblyArtifact(path, content)],
        )
        if issue is not None:
            return None
        if _artifact_external_filter_issue(context.filter_root, artifacts, env=context.env) is not None:
            return None
        return _hash_artifact_content(context.filter_root, artifacts[0].path, content, env=context.env)


def find_delivery_artifact_commit(
    project_root: Path,
    *,
    branch: str,
    parent_commit: str,
    proposal_id: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> tuple[str | None, str | None]:
    """Return the amendment commit and current branch commit when exact artifacts are present."""
    root = project_root.resolve()
    try:
        current = _branch_commit(root, branch)
        ancestor = current is not None and _is_ancestor(root, parent_commit, current)
    except OSError:
        return None, None
    if current is None or not ancestor:
        return None, current
    with _delivery_artifact_git_context(root, parent_commit) as context:
        if context is None:
            return None, current
        git_artifacts, artifact_issue = _artifacts_relative_to_git_root(context, artifacts)
        if artifact_issue is not None:
            return None, current
        try:
            revisions = subprocess.run(
                [
                    "git",
                    "log",
                    "--format=%H",
                    "--fixed-strings",
                    f"--grep=sikula: apply delivery amendment {proposal_id}",
                    current,
                    f"^{parent_commit}",
                ],
                cwd=root,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None, current
        if revisions.returncode != 0:
            return None, current
        expected_subject = f"sikula: apply delivery amendment {proposal_id}"
        for commit in revisions.stdout.splitlines():
            if not _OBJECT_ID_RE.fullmatch(commit):
                continue
            try:
                message = subprocess.run(
                    ["git", "show", "-s", "--format=%s", commit], cwd=root, capture_output=True, text=True
                )
            except OSError:
                return None, current
            if message.returncode != 0 or message.stdout.strip() != expected_subject:
                continue
            if _commit_has_artifacts(context, commit, parent_commit, git_artifacts):
                return (commit, current) if _tree_has_artifacts(context, current, git_artifacts) else (None, current)
    return None, current


def preview_delivery_assembly(
    project_root: Path,
    *,
    branch: str,
    base_commit: str,
    expected_commit: str | None,
    units: list[DeliveryAssemblyUnit],
    allow_checked_out: bool = False,
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
    if current is not None and not allow_checked_out and branch_checked_out(root, branch):
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


def _artifact_preflight_issue(
    context: _DeliveryArtifactGitContext,
    branch: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> DeliveryPlanIssue | None:
    root = context.project_root
    parent_commit = context.parent_commit
    if not _valid_branch_name(root, branch):
        return DeliveryPlanIssue(
            "error",
            "delivery.assembly_branch_invalid",
            "Delivery final_branch is not a valid local branch name.",
        )
    if delivery_assembly_branch_is_symbolic(root, branch):
        return DeliveryPlanIssue(
            "error",
            "delivery.assembly_branch_symbolic",
            "Delivery final_branch must be a direct ref, not a symbolic ref.",
        )
    if branch_checked_out(root, branch):
        return DeliveryPlanIssue(
            "error",
            "delivery.assembly_branch_checked_out",
            "Delivery assembly branch is checked out; switch that worktree away before retrying.",
        )
    current = _branch_commit(root, branch)
    if current is not None and current != parent_commit and not _is_ancestor(root, current, parent_commit):
        return DeliveryPlanIssue(
            "error",
            "delivery.assembly_branch_diverged",
            "Delivery assembly branch changed before amendment artifacts were integrated.",
        )
    return _artifact_parent_compatibility_issue(context, artifacts)


def _artifact_parent_compatibility_issue(
    context: _DeliveryArtifactGitContext,
    artifacts: list[DeliveryAssemblyArtifact],
) -> DeliveryPlanIssue | None:
    issue = _artifact_paths_issue(artifacts)
    if issue is not None:
        return issue
    filter_issue = _artifact_external_filter_issue(context.filter_root, artifacts, env=context.env)
    if filter_issue is not None:
        return filter_issue
    for artifact in artifacts:
        try:
            existing = _tree_artifact_entry(context, context.parent_commit, artifact.path)
            ancestor_conflict = _artifact_ancestor_conflict(context, context.parent_commit, artifact.path)
        except (OSError, UnicodeError, ValueError):
            return _artifact_git_issue()
        if ancestor_conflict:
            return DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_conflict",
                "A delivery amendment artifact path conflicts with a non-directory assembly entry.",
            )
        if artifact.must_not_exist and existing is not None:
            return DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_conflict",
                "A replacement unit artifact already exists in the delivery assembly.",
            )
        expected_blob = artifact.expected_object_id
        if artifact.expected_content is not None:
            expected_blob = _hash_artifact_content(
                context.filter_root,
                artifact.path,
                artifact.expected_content,
                env=context.env,
            )
            if expected_blob is None:
                return _artifact_git_issue()
        expected_fingerprint = None
        if artifact.expected_fingerprint is not None and existing is not None:
            filtered = _filtered_tree_content(
                context.filter_root,
                context.parent_commit,
                artifact.path,
                env=context.env,
            )
            if filtered is None:
                return _artifact_git_issue()
            expected_fingerprint = hashlib.sha256(filtered).hexdigest()
        existing_stale = existing is not None and (
            (expected_blob is not None and existing[1] != expected_blob)
            or (artifact.expected_fingerprint is not None and expected_fingerprint != artifact.expected_fingerprint)
        )
        if existing_stale:
            return DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_stale",
                "A tracked delivery artifact changed in the delivery assembly.",
            )
    return None


def _artifact_paths_issue(artifacts: list[DeliveryAssemblyArtifact]) -> DeliveryPlanIssue | None:
    if not artifacts:
        return DeliveryPlanIssue(
            "error",
            "delivery.assembly_artifacts_empty",
            "Delivery amendment has no tracked artifacts to integrate.",
        )
    seen: set[str] = set()
    for artifact in artifacts:
        path = artifact.path
        posix_path = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        if (
            not path
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or bool(windows_path.drive)
            or any(part in {"", ".", ".."} for part in posix_path.parts)
            or ".." in windows_path.parts
            or "\\" in path
            or "\x00" in path
            or artifact.mode not in {"100644", "100755"}
            or path.casefold() in seen
        ):
            return DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_path_invalid",
                "Delivery amendment artifact paths must be unique project-relative Git paths.",
            )
        seen.add(path.casefold())
    for artifact in artifacts:
        if any(
            ancestor != PurePosixPath(".") and ancestor.as_posix().casefold() in seen
            for ancestor in PurePosixPath(artifact.path).parents
        ):
            return DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_path_invalid",
                "Delivery amendment artifacts must not contain directory/file path conflicts.",
            )
    return None


def _artifacts_relative_to_git_root(
    context: _DeliveryArtifactGitContext,
    artifacts: list[DeliveryAssemblyArtifact],
) -> tuple[list[DeliveryAssemblyArtifact], DeliveryPlanIssue | None]:
    issue = _artifact_paths_issue(artifacts)
    if issue is not None:
        return [], issue
    project_prefix = context.project_prefix
    git_artifacts = (
        list(artifacts)
        if project_prefix == "."
        else [replace(artifact, path=f"{project_prefix}/{artifact.path}") for artifact in artifacts]
    )
    result: list[DeliveryAssemblyArtifact] = []
    try:
        for artifact in git_artifacts:
            entry = _tree_artifact_entry(context, context.parent_commit, artifact.path)
            if entry is not None and entry[0] not in {"100644", "100755"}:
                return [], DeliveryPlanIssue(
                    "error",
                    "delivery.assembly_artifact_stale",
                    "A tracked delivery artifact has an unsupported Git file mode.",
                )
            result.append(replace(artifact, mode=entry[0]) if entry is not None else artifact)
    except (OSError, UnicodeError, ValueError):
        return [], _artifact_git_issue()
    return result, None


def _create_artifact_commit(
    context: _DeliveryArtifactGitContext,
    *,
    plan_id: str,
    proposal_id: str,
    artifacts: list[DeliveryAssemblyArtifact],
    created_at: str,
) -> tuple[str | None, DeliveryPlanIssue | None]:
    issue = _artifact_paths_issue(artifacts)
    if issue is not None:
        return None, issue
    root = context.project_root
    parent_commit = context.parent_commit
    try:
        filter_issue = _artifact_external_filter_issue(context.filter_root, artifacts, env=context.env)
        if filter_issue is not None:
            return None, filter_issue
        if not context.ensure_index_loaded():
            return None, _artifact_git_issue()
        index_entries: list[bytes] = []
        for artifact in artifacts:
            object_id = _hash_artifact_content(
                context.filter_root,
                artifact.path,
                artifact.content,
                write=True,
                env=context.env,
            )
            if object_id is None:
                return None, _artifact_git_issue()
            index_entries.append(f"{artifact.mode} {object_id}\t{artifact.path}".encode("utf-8") + b"\0")
        updated = subprocess.run(
            ["git", "update-index", "-z", "--index-info"],
            cwd=context.filter_root,
            env=context.env,
            input=b"".join(index_entries),
            capture_output=True,
        )
        if updated.returncode != 0:
            return None, _artifact_git_issue()
        written = subprocess.run(
            ["git", "write-tree"],
            cwd=context.filter_root,
            env=context.env,
            capture_output=True,
            text=True,
        )
        tree = written.stdout.strip() if written.returncode == 0 else ""
        if not _OBJECT_ID_RE.fullmatch(tree):
            return None, _artifact_git_issue()

        commit_env = os.environ.copy()
        commit_env.update(
            {
                "GIT_AUTHOR_NAME": "Sikula",
                "GIT_AUTHOR_EMAIL": "sikula@localhost",
                "GIT_AUTHOR_DATE": created_at,
                "GIT_COMMITTER_NAME": "Sikula",
                "GIT_COMMITTER_EMAIL": "sikula@localhost",
                "GIT_COMMITTER_DATE": created_at,
            }
        )
        message = f"sikula: apply delivery amendment {proposal_id}\n\nPlan: {plan_id}"
        committed = subprocess.run(
            ["git", "commit-tree", tree, "-p", parent_commit, "-m", message],
            cwd=root,
            env=commit_env,
            capture_output=True,
            text=True,
        )
        commit = committed.stdout.strip() if committed.returncode == 0 else ""
        if not _OBJECT_ID_RE.fullmatch(commit):
            return None, _artifact_git_issue()
        return commit, None
    except (OSError, UnicodeError, ValueError):
        return None, _artifact_git_issue()


def _artifact_git_issue() -> DeliveryPlanIssue:
    return DeliveryPlanIssue(
        "error",
        "delivery.assembly_artifact_git_failed",
        "Git could not create the delivery amendment artifact commit.",
    )


def _hash_artifact_content(
    root: Path,
    path: str,
    content: bytes,
    *,
    write: bool = False,
    env: dict[str, str] | None = None,
) -> str | None:
    command = ["git", "hash-object"]
    if write:
        command.append("-w")
    command.extend([f"--path={path}", "--stdin"])
    try:
        result = subprocess.run(
            command,
            cwd=root,
            env=env,
            input=content,
            capture_output=True,
        )
        object_id = result.stdout.decode("ascii", errors="strict").strip() if result.returncode == 0 else ""
    except (OSError, UnicodeError):
        return None
    return object_id if _OBJECT_ID_RE.fullmatch(object_id) else None


def _artifact_external_filter_issue(
    root: Path,
    artifacts: list[DeliveryAssemblyArtifact],
    *,
    env: dict[str, str],
) -> DeliveryPlanIssue | None:
    try:
        result = subprocess.run(
            ["git", "check-attr", "-z", "filter", "--", *(artifact.path for artifact in artifacts)],
            cwd=root,
            env=env,
            capture_output=True,
        )
    except OSError:
        return _artifact_git_issue()
    fields = result.stdout.rstrip(b"\0").split(b"\0") if result.stdout else []
    if result.returncode != 0 or len(fields) != len(artifacts) * 3:
        return _artifact_git_issue()
    for index in range(0, len(fields), 3):
        if fields[index + 1] != b"filter":
            return _artifact_git_issue()
        if fields[index + 2] not in {b"unspecified", b"unset"}:
            return DeliveryPlanIssue(
                "error",
                "delivery.assembly_artifact_filter_unsupported",
                "Delivery amendment artifacts cannot use external Git content filters.",
            )
    return None


@contextmanager
def _delivery_artifact_git_context(
    root: Path,
    parent_commit: str,
) -> Iterator[_DeliveryArtifactGitContext | None]:
    with tempfile.TemporaryDirectory(prefix="sikula-delivery-filter-") as temp_dir:
        yield _parent_filter_context(root, parent_commit, Path(temp_dir))


def _parent_filter_context(
    root: Path,
    parent_commit: str,
    temp_dir: Path,
) -> _DeliveryArtifactGitContext | None:
    autocrlf_valid, autocrlf = _safe_core_autocrlf(root)
    if not autocrlf_valid:
        return None
    try:
        repository_result = subprocess.run(
            [
                "git",
                "rev-parse",
                "--show-toplevel",
                "--absolute-git-dir",
                "--git-common-dir",
                "--show-object-format",
            ],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    repository_metadata = repository_result.stdout.splitlines()
    if repository_result.returncode != 0 or len(repository_metadata) != 4:
        return None
    git_root_text, git_dir_text, common_dir_text, object_format = repository_metadata
    if not git_root_text or not git_dir_text or not common_dir_text or object_format not in {"sha1", "sha256"}:
        return None
    git_root = Path(git_root_text).resolve()
    common_dir = Path(common_dir_text)
    if not common_dir.is_absolute():
        common_dir = (root / common_dir).resolve()
    object_dir = common_dir / "objects"
    worktree = temp_dir / "worktree"
    worktree.mkdir()
    isolated_git_dir = temp_dir / "git"
    (isolated_git_dir / "objects" / "info").mkdir(parents=True)
    (isolated_git_dir / "objects" / "pack").mkdir()
    (isolated_git_dir / "refs" / "heads").mkdir(parents=True)
    (isolated_git_dir / "HEAD").write_text("ref: refs/heads/sikula-filter-context\n", encoding="ascii")
    if object_format == "sha256":
        (isolated_git_dir / "config").write_text(
            "[core]\n\trepositoryFormatVersion = 1\n[extensions]\n\tobjectFormat = sha256\n",
            encoding="ascii",
        )
    empty_attributes = temp_dir / "attributes"
    empty_attributes.touch()
    empty_global_config = temp_dir / "global-config"
    empty_global_config.touch()
    config_values = [
        ("core.repositoryFormatVersion", "1" if object_format == "sha256" else "0"),
        ("core.bare", "false"),
        ("core.attributesFile", str(empty_attributes)),
    ]
    if autocrlf is not None:
        config_values.append(("core.autocrlf", autocrlf))
    if object_format == "sha256":
        config_values.append(("extensions.objectFormat", "sha256"))
    env = os.environ.copy()
    for key in list(env):
        if key in {"GIT_COMMON_DIR", "GIT_CONFIG", "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS"} or key.startswith(
            ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")
        ):
            env.pop(key)
    env.update(
        GIT_DIR=str(isolated_git_dir),
        GIT_INDEX_FILE=str(temp_dir / "index"),
        GIT_WORK_TREE=str(worktree),
        GIT_OBJECT_DIRECTORY=str(object_dir),
        GIT_CONFIG_GLOBAL=str(empty_global_config),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_ATTR_NOSYSTEM="1",
        GIT_CONFIG_COUNT=str(len(config_values)),
    )
    for index, (key, value) in enumerate(config_values):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    env.pop("GIT_ATTR_SOURCE", None)
    entries = _load_tree_entries(git_root, parent_commit, env=env)
    if entries is None:
        return None
    attribute_entries: list[tuple[str, str]] = []
    for path, (mode, object_id) in entries.items():
        if path.rsplit(b"/", 1)[-1] != b".gitattributes" or mode not in {"100644", "100755"}:
            continue
        try:
            decoded_path = path.decode("utf-8", errors="strict")
        except UnicodeError:
            continue
        attribute_entries.append((decoded_path, object_id))
    for path, object_id in attribute_entries:
        try:
            blob = subprocess.run(
                ["git", "cat-file", "blob", object_id],
                cwd=git_root,
                env=env,
                capture_output=True,
            )
        except OSError:
            return None
        if blob.returncode != 0:
            return None
        target = worktree.joinpath(*PurePosixPath(path).parts)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob.stdout)
        except OSError:
            return None
    return _DeliveryArtifactGitContext(
        project_root=root,
        git_root=git_root,
        git_dir=Path(git_dir_text).resolve(),
        common_dir=common_dir,
        object_format=object_format,
        parent_commit=parent_commit,
        filter_root=worktree,
        env=env,
        tree_entries_by_commit={parent_commit: entries},
    )


def _load_tree_entries(
    root: Path,
    commit: str,
    *,
    env: dict[str, str] | None = None,
) -> dict[bytes, tuple[str, str]] | None:
    try:
        result = subprocess.run(
            ["git", "ls-tree", "-r", "-t", "--full-tree", "-z", commit],
            cwd=root,
            env=env,
            capture_output=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    entries: dict[bytes, tuple[str, str]] = {}
    for raw_entry in result.stdout.rstrip(b"\0").split(b"\0"):
        if not raw_entry:
            continue
        if b"\t" not in raw_entry:
            return None
        metadata, path = raw_entry.split(b"\t", 1)
        fields = metadata.split(b" ")
        if len(fields) != 3 or not path or path in entries:
            return None
        try:
            mode = fields[0].decode("ascii", errors="strict")
            object_id = fields[2].decode("ascii", errors="strict")
        except UnicodeError:
            return None
        if not _OBJECT_ID_RE.fullmatch(object_id):
            return None
        entries[path] = (mode, object_id)
    return entries


def _safe_core_autocrlf(root: Path) -> tuple[bool, str | None]:
    try:
        result = subprocess.run(
            ["git", "config", "--get", "core.autocrlf"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False, None
    if result.returncode == 1 and not result.stdout:
        return True, None
    if result.returncode != 0:
        return False, None
    value = result.stdout.strip().casefold()
    normalized = {
        "1": "true",
        "yes": "true",
        "on": "true",
        "true": "true",
        "0": "false",
        "no": "false",
        "off": "false",
        "false": "false",
        "input": "input",
    }.get(value)
    return (True, normalized) if normalized is not None else (False, None)


def _tree_artifact_entry(
    context: _DeliveryArtifactGitContext,
    commit: str,
    path: str,
) -> tuple[str, str] | None:
    return context.tree_entries(commit).get(path.encode("utf-8"))


def _artifact_ancestor_conflict(context: _DeliveryArtifactGitContext, commit: str, path: str) -> bool:
    for ancestor in reversed(PurePosixPath(path).parents):
        if ancestor == PurePosixPath("."):
            continue
        entry = _tree_artifact_entry(context, commit, ancestor.as_posix())
        if entry is not None and entry[0] != "040000":
            return True
    return False


def _filtered_tree_content(
    root: Path,
    commit: str,
    path: str,
    *,
    env: dict[str, str],
) -> bytes | None:
    try:
        result = subprocess.run(
            ["git", "cat-file", "--filters", f"--path={path}", f"{commit}:{path}"],
            cwd=root,
            env=env,
            capture_output=True,
        )
    except OSError:
        return None
    return result.stdout if result.returncode == 0 else None


def _commit_has_artifacts(
    context: _DeliveryArtifactGitContext,
    commit: str,
    parent_commit: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> bool:
    root = context.project_root
    try:
        parent = subprocess.run(
            ["git", "rev-parse", "--verify", f"{commit}^"],
            cwd=root,
            capture_output=True,
            text=True,
        )
        if parent.returncode != 0 or parent.stdout.strip() != parent_commit:
            return False
        changed = subprocess.run(
            ["git", "diff-tree", "--no-commit-id", "--name-only", "-r", "-z", commit],
            cwd=root,
            capture_output=True,
        )
    except OSError:
        return False
    artifact_paths = {artifact.path.encode("utf-8") for artifact in artifacts}
    changed_paths = {path for path in changed.stdout.rstrip(b"\0").split(b"\0") if path}
    if changed.returncode != 0 or not changed_paths.issubset(artifact_paths):
        return False
    return _tree_has_artifacts(context, commit, artifacts)


def _tree_has_artifacts(
    context: _DeliveryArtifactGitContext,
    commit: str,
    artifacts: list[DeliveryAssemblyArtifact],
) -> bool:
    if _artifact_external_filter_issue(context.filter_root, artifacts, env=context.env) is not None:
        return False
    for artifact in artifacts:
        try:
            entry = _tree_artifact_entry(context, commit, artifact.path)
            expected_blob = _hash_artifact_content(
                context.filter_root,
                artifact.path,
                artifact.content,
                env=context.env,
            )
        except (OSError, UnicodeError, ValueError):
            return False
        if entry is None or expected_blob is None or entry[0] != artifact.mode or entry[1] != expected_blob:
            return False
    return True


def _artifact_failure(
    branch: str,
    parent_commit: str,
    code: str,
    message: str,
) -> DeliveryArtifactAssemblyResult:
    return DeliveryArtifactAssemblyResult(
        success=False,
        branch=branch,
        parent_commit=parent_commit,
        assembled_commit=None,
        error=DeliveryPlanIssue("error", code, message),
    )


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


def _delete_ref(root: Path, branch: str, expected: str) -> bool:
    if branch_checked_out(root, branch) or delivery_assembly_branch_is_symbolic(root, branch):
        return False
    result = subprocess.run(
        ["git", "update-ref", "--no-deref", "-d", f"refs/heads/{branch}", expected],
        cwd=root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0
