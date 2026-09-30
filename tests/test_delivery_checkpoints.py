from __future__ import annotations

import argparse
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from core.delivery_checkpoint_model import DeliveryCheckpointError, parse_delivery_checkpoints
from core.delivery_checkpoints import checkpoint_pass_is_usable, due_delivery_checkpoint, verification_node_status
from core.delivery_plan import DeliveryPlanUnit, check_delivery_plan_file
from core.delivery_obligations import DeliveryObligation, delivery_authority_fragments
from core.delivery_progress import (
    DeliveryProgress,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    read_delivery_progress,
    upsert_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_repair import coordinate_delivery_repair
from core.delivery_run_next import preview_delivery_run_next
from core.delivery_verify import verify_delivery_plan
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification_validation import DeliveryVerificationValidationResult
from core.state import JsonStateStore
from sikula_cli.delivery import _preview_delivery_run, DeliveryRunNextContext
from tests.test_delivery_repair import _CONTRACT, _LLM, _assessment, _draft, _git, _repair


def _declarations() -> tuple[list, list, list]:
    units = [
        DeliveryPlanUnit(key, None, key + ".md", depends_on=deps)
        for key, deps in (("read", []), ("write", []), ("consumer", ["read", "write"]))
    ]
    obligations = [DeliveryObligation("consistent-read", "Consistency", ["f"], ["read", "write"])]
    declarations = [{"id": "storage", "unit_ids": ["read", "write"], "obligation_ids": ["consistent-read"]}]
    return units, obligations, declarations


@pytest.mark.parametrize(
    "change", ["unknown", "future_owner", "missing_prerequisite", "root", "duplicate", "cycle", "empty"]
)
def test_declaration_rejects_unsafe_barriers(change: str) -> None:
    units, obligations, declarations = _declarations()
    if change == "unknown":
        declarations[0]["unit_ids"].append("unknown")
    elif change == "future_owner":
        obligations[0].unit_ids.append("consumer")
    elif change == "missing_prerequisite":
        units[0].depends_on.append("consumer")
    elif change == "root":
        declarations[0]["id"] = "root"
    elif change == "duplicate":
        declarations *= 2
    elif change == "empty":
        declarations[0]["obligation_ids"] = []
    else:
        units = [
            DeliveryPlanUnit(key, None, key + ".md", depends_on=deps)
            for key, deps in (("a", []), ("b", ["a"]), ("c", ["a"]), ("d", ["b", "c"]))
        ]
        obligations = [
            DeliveryObligation("left", "Left", ["f"], ["a", "b"]),
            DeliveryObligation("right", "Right", ["f"], ["a", "c"]),
        ]
        declarations = [
            {"id": "left", "unit_ids": ["a", "b"], "obligation_ids": ["left"]},
            {"id": "right", "unit_ids": ["a", "c"], "obligation_ids": ["right"]},
        ]
    with pytest.raises(DeliveryCheckpointError):
        parse_delivery_checkpoints(declarations, units, obligations)


@pytest.fixture
def checkpoint_plan(tmp_path: Path) -> tuple[Path, dict]:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/cache.py").write_text("cache = {}\n")
    units, obligations, declarations = _declarations()
    for unit in units:
        (tmp_path / unit.task_path).write_text(_CONTRACT)
    source = (
        "# Consistency\n\nCompleted updates must be visible through reads. Consumers use the integrated storage API.\n"
    )
    (tmp_path / "source.md").write_text(source)
    fragments = [item.id for item in delivery_authority_fragments(source)]
    path = tmp_path / "plan.yaml"
    data = {
        "schema_version": 3,
        "plan_id": "cache",
        "title": "Consistent storage",
        "final_branch": "sikula/delivery/cache",
        "verification": {"mode": "final_gate"},
        "source_task": {"path": "source.md", "sha256": "sha256:" + sha256(source.encode()).hexdigest()},
        "units": [{**item.to_authoring_dict(), "scope_paths": ["src/"]} for item in units],
        "obligations": [{**item.to_context_dict(), "source_fragment_ids": fragments} for item in obligations],
        "source_accounting": [
            {
                "source_fragment_id": key,
                "disposition": "mapped",
                "obligation_ids": ["consistent-read"],
                "constraint_ids": [],
                "rationale_sha256": "sha256:" + "f" * 64,
            }
            for key in fragments
        ],
        "checkpoints": declarations,
    }
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "checkpoint plan")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "cache"),
        DeliveryProgress(
            schema_version=1,
            plan_id="cache",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress(key, "done", commit=commit) for key in ("read", "write")],
        ),
    )
    cfg = {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "sandbox": {"allowed_write_paths": ["src/"], "allowed_read_paths": ["."]},
        "run_build": True,
        "run_tests": True,
        "run_checks": False,
        "build": {"test_command": "python -m pytest tests/"},
    }
    assert check_delivery_plan_file(path).valid
    return path, cfg


def _verify_node(
    path: Path, cfg: dict, disposition: str = "approved", node_id: str = "storage", *, assessment: str | None = None
):
    root = Path(cfg["project"]["root_path"])
    llm = _LLM(assessment if assessment is not None else _assessment(disposition))
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            state_store=JsonStateStore(root / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(llm, cfg),
            security_reviewer=None,
            project_root=root,
            node_id=node_id,
        )
    return result, llm


def _complete(path: Path, unit_id: str) -> None:
    status = get_delivery_status(path)
    root = Path(status.project_root)
    parent = status.assembled_commit
    tree = _git(root, "rev-parse", parent + "^{tree}")
    commit = _git(root, "commit-tree", tree, "-p", parent, "-m", "complete " + unit_id)
    progress_path = delivery_progress_path(root, status.plan.plan_id)
    progress, errors = read_delivery_progress(progress_path, plan_id=status.plan.plan_id)
    assert not errors
    write_delivery_progress(
        progress_path,
        upsert_delivery_unit_progress(progress, make_delivery_unit_progress(unit_id, "done", commit=commit)),
    )


def test_checkpoint_pass_releases_consumer_and_survives_downstream_commits(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    status = get_delivery_status(path)
    assert due_delivery_checkpoint(status, cfg) == "storage"
    assert not preview_delivery_run_next(path).ready
    scope = DeliveryVerificationScope.from_plan(status.plan, "storage")
    assert scope.unit_ids == ("read", "write")
    result, llm = _verify_node(path, cfg)
    assert result.succeeded, result
    assert "intermediate checkpoint" in llm.calls[0]
    status = get_delivery_status(path)
    assert status.verification is None
    assert status.to_dict()["checkpoints"][0]["status"] == "accepted_handoff"
    assert preview_delivery_run_next(path).selected_unit.id == "consumer"
    _complete(path, "consumer")
    result, _ = _verify_node(path, cfg, node_id="root")
    assert result.succeeded, result
    status = get_delivery_status(path)
    assert due_delivery_checkpoint(status, cfg) is None
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)


def test_checkpoint_repairs_only_completed_group_and_passes_repair_to_consumers(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    result, _ = _verify_node(path, cfg, "repair_required")
    assert result.stop_code == "delivery_verification.repair_required", result
    author = _LLM(_draft())
    repaired = _repair(path, cfg, author, node_id="storage")
    assert repaired.ready, repaired
    data = yaml.safe_load(path.read_text())
    unit = next(item for item in data["units"] if item["id"] == repaired.unit_id)
    assert unit["depends_on"] == ["read", "write"]
    assert data["checkpoints"][0]["unit_ids"] == ["read", "write", repaired.unit_id]
    assert data["units"][2]["depends_on"] == ["read", "write", repaired.unit_id]
    assert preview_delivery_run_next(path).selected_unit.id == repaired.unit_id
    _complete(path, repaired.unit_id)
    result, _ = _verify_node(path, cfg)
    assert result.succeeded, result
    assert preview_delivery_run_next(path).selected_unit.id == "consumer"
    exhausted = coordinate_delivery_repair(
        path, cfg, project_root=path.parent, agent_factory=None, dry_run=True, node_id="storage"
    )
    assert exhausted.issue.code == "delivery_repair.budget_exhausted"


@pytest.mark.parametrize("split", ["single", "nested", "downstream", "downstream_first"])
def test_checkpoint_repair_preserves_covered_amendment_lineage(checkpoint_plan, split: str) -> None:
    from core.delivery_amendment import apply_delivery_amendment, create_delivery_amendment_proposal
    from core.delivery_authoring import (
        DeliveryAmendmentAuthoringDraft,
        DeliveryAuthoringObligationDraft,
        DeliveryAuthoringUnitDraft,
        DeliveryConstraintVerification,
    )

    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    data["units"][1]["depends_on"] = ["read"]
    data["units"][2]["depends_on"] = ["write"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "dependent checkpoint member before splitting")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(progress.units[0], commit=commit)]),
    )

    def amend(target: str) -> None:
        status = get_delivery_status(path)
        replacements = [target + "-1", target + "-2"]
        obligations = [
            DeliveryAuthoringObligationDraft(item.id, item.summary, item.source_fragment_ids, replacements, "preserved")
            for item in status.plan.obligations
            if target in item.unit_ids
        ]
        draft = DeliveryAmendmentAuthoringDraft(
            plan_id="cache",
            target_unit_id=target,
            replacement_units=[
                DeliveryAuthoringUnitDraft(key, key, [] if index == 0 else [replacements[0]], _CONTRACT)
                for index, key in enumerate(replacements)
            ],
            constraint_verification=DeliveryConstraintVerification(
                constraints_complete=True, constraints=[], obligations_complete=True, obligations=obligations
            ),
        )
        proposal_root = root / ".sikula/state/delivery/cache/amendments"
        proposal, _ = create_delivery_amendment_proposal(
            path, target, draft, project_root=root, proposal_root=proposal_root, project_config=cfg
        )
        applied = apply_delivery_amendment(
            path, proposal.proposal_id, project_root=root, proposal_root=proposal_root, project_config=cfg
        )
        assert applied.applied, applied

    if split == "downstream_first":
        amend("consumer")
    amend("write")
    if split == "nested":
        amend("write-1")
    elif split == "downstream":
        amend("consumer")
    status = get_delivery_status(path)
    covered = set(status.plan.checkpoints[0].unit_ids)
    for unit in status.units:
        if unit.id in covered and unit.status != "done":
            _complete(path, unit.id)
    before = {unit.id: unit.to_authoring_dict() for unit in get_delivery_status(path).plan.units}
    contracts = {unit["task_path"]: (root / unit["task_path"]).read_bytes() for unit in before.values()}
    assessment = json.loads(_assessment("repair_required"))
    assessment["findings"][0]["unit_ids"] = sorted(covered)
    result, _ = _verify_node(path, cfg, assessment=json.dumps(assessment))
    assert result.stop_code == "delivery_verification.repair_required", result
    preview = coordinate_delivery_repair(
        path, cfg, project_root=root, agent_factory=None, dry_run=True, node_id="storage"
    )
    assert preview.ready, preview
    author = _LLM(_draft())
    if split == "nested":
        with (
            patch("core.delivery_repair.assemble_delivery_artifacts", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            _repair(path, cfg, author, node_id="storage")
    repaired = _repair(path, cfg, author, node_id="storage")
    assert repaired.ready, repaired
    assert len(author.calls) == 1
    status = get_delivery_status(path)
    assert status.valid, status.errors
    after = {unit.id: unit.to_authoring_dict() for unit in status.plan.units}
    for key in covered | {key for key in before if key.startswith("write")}:
        assert after[key] == before[key]
    assert contracts == {key: (root / key).read_bytes() for key in contracts}
    assert set(after[repaired.unit_id]["depends_on"]) == covered
    assert repaired.unit_id in after["consumer"]["depends_on"]
    if split.startswith("downstream"):
        assert repaired.unit_id in after["consumer-1"]["depends_on"]
        assert after["consumer-2"] == before["consumer-2"]
    assert preview_delivery_run_next(path).selected_unit.id == repaired.unit_id
    _complete(path, repaired.unit_id)
    assert _verify_node(path, cfg)[0].succeeded
    assert preview_delivery_run_next(path).selected_unit.id == (
        "consumer-1" if split.startswith("downstream") else "consumer"
    )


def test_contract_change_invalidates_historical_handoff(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    (path.parent / "read.md").write_text(_CONTRACT + "\nChanged requirement.\n")
    status = get_delivery_status(path)
    assert due_delivery_checkpoint(status, cfg) == "storage"
    assert not preview_delivery_run_next(path).ready
    assert status.to_dict()["checkpoints"][0]["status"] == "stale"


@pytest.mark.parametrize("change", ["same", "changed", "missing", "symlink"])
@pytest.mark.parametrize("nested", [False, True])
def test_checkpoint_receipt_binds_assembled_contracts(checkpoint_plan, change: str, nested: bool) -> None:
    from core.delivery_checkpoints import checkpoint_barrier_issue

    path, cfg = checkpoint_plan
    root = path.parent
    if nested:
        project = root / "apps/service"
        project.mkdir(parents=True)
        for name in ("src", "read.md", "write.md", "consumer.md", "source.md", "plan.yaml"):
            (root / name).rename(project / name)
        _git(root, "add", ".")
        _git(root, "commit", "-m", "nested project")
        path = project / "plan.yaml"
        cfg["project"]["root_path"] = str(project)
        (project / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n")
    project = path.parent
    data = yaml.safe_load(path.read_text())
    data["units"][2]["scope_paths"].append("read.md")
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "allow downstream contract edits")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(project, "cache")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="cache",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress(key, "done", commit=commit) for key in ("read", "write")],
        ),
    )
    assert _verify_node(path, cfg)[0].succeeded
    status = get_delivery_status(path, project_root=project)
    record = status.checkpoint_verifications["storage"]
    operator_branch = _git(root, "branch", "--show-current")
    _git(root, "checkout", status.plan.final_branch)
    if change == "changed":
        (project / "read.md").write_text(_CONTRACT + "\nChanged assembled requirement.\n")
    elif change == "missing":
        (project / "read.md").unlink()
    elif change == "symlink":
        (project / "read.md").unlink()
        try:
            (project / "read.md").symlink_to("write.md")
        except (OSError, NotImplementedError):
            pytest.skip("Symlinks unavailable")
    (project / "src/cache.py").write_text("cache = {'downstream': True}\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "downstream candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", operator_branch)
    assert (project / "read.md").read_text() == _CONTRACT
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    progress = upsert_delivery_unit_progress(
        progress, make_delivery_unit_progress("consumer", "done", commit=candidate)
    )
    write_delivery_progress(progress_path, replace(progress, assembled_commit=candidate))

    status = get_delivery_status(path, project_root=project)
    assert status.valid, status.errors
    assert status.checkpoint_verifications["storage"] == record
    for effective_cfg in (cfg, None):
        assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], effective_cfg) == (change == "same")
    issue = checkpoint_barrier_issue(status, cfg)
    if change == "same":
        assert issue is None
    else:
        assert issue.code == "delivery_checkpoint.handoff_stale"
        assert status.to_dict()["checkpoints"][0]["status"] == "stale"


@pytest.mark.parametrize("add_contract", [False, True])
def test_checkpoint_preserves_uncommitted_executed_contract_evidence(checkpoint_plan, add_contract: bool) -> None:
    from core.state import TaskState

    path, cfg = checkpoint_plan
    root = path.parent
    contract = root / "read.md"
    contract.unlink()
    _git(root, "add", "read.md")
    _git(root, "commit", "-m", "contract supplied outside candidate")
    commit = _git(root, "rev-parse", "HEAD")
    contract.write_text(_CONTRACT)
    store = JsonStateStore(root / ".sikula/state")
    child = TaskState(
        task_id="read-child",
        task_description=_CONTRACT,
        delivery_plan_id="cache",
        delivery_unit_id="read",
        delivery_plan_path="plan.yaml",
        done=True,
        result_commit=commit,
    )
    store.save(child)
    progress_path = delivery_progress_path(root, "cache")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="cache",
            assembly_base_commit=commit,
            units=[
                make_delivery_unit_progress("read", "done", commit=commit, child_task_id=child.task_id),
                make_delivery_unit_progress("write", "done", commit=commit),
            ],
        ),
    )
    result, _ = _verify_node(path, cfg)
    assert result.succeeded, result
    status = get_delivery_status(path)
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    operator_branch = _git(root, "branch", "--show-current")
    _git(root, "checkout", status.plan.final_branch)
    (root / "src/cache.py").write_text("cache = {'downstream': True}\n")
    _git(root, "add", "src/cache.py")
    if add_contract:
        _git(root, "add", "read.md")
    _git(root, "commit", "-m", "downstream candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", operator_branch)
    contract.write_text(_CONTRACT)
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(progress_path, replace(progress, assembled_commit=candidate))
    status = get_delivery_status(path)
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg) == (not add_contract)


def test_checkpoint_rename_cannot_reset_recovery_lineage(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    data = yaml.safe_load(path.read_text())
    data["checkpoints"][0]["id"] = "renamed"
    path.write_text(yaml.safe_dump(data))
    status = get_delivery_status(path)
    assert not status.valid
    assert any(issue.code == "delivery_checkpoint.policy_changed" for issue in status.errors)


def test_checkpoint_preparer_override_can_refresh_before_authoring(checkpoint_plan) -> None:
    from core.delivery_verification import with_delivery_verification_readiness

    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg, "repair_required")[0].stop_code == "delivery_verification.repair_required"
    cfg["agent_llms"] = {"delivery_preparer": {"model": "different"}}
    status = with_delivery_verification_readiness(verification_node_status(get_delivery_status(path), "storage"), cfg)
    assert status.verification_status == "stale"


def test_checkpoint_missing_handoff_blocks_preview_and_provider(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    units = [replace(item, handoff_schema_version=1, handoff_fingerprint="f" * 64) for item in progress.units]
    write_delivery_progress(progress_path, replace(progress, units=units))
    context = DeliveryRunNextContext(
        state_store=JsonStateStore(root / ".sikula/state"),
        run_task=lambda *args: None,
        resolve_state_dir=lambda cfg: root / ".sikula/state",
    )
    args = argparse.Namespace(plan_file=str(path), max_units=None, max_elapsed_minutes=None)
    preview = _preview_delivery_run(args, cfg, context=context, project_root=root)
    assert not preview.ready
    assert preview.errors[0].code == "delivery.dependency_handoff_missing"
    result, llm = _verify_node(path, cfg)
    assert result.stop_code == "delivery.dependency_handoff_missing"
    assert not llm.calls


def test_root_repair_does_not_reopen_accepted_checkpoint(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    _complete(path, "consumer")
    result, _ = _verify_node(path, cfg, "repair_required", node_id="root")
    assert result.stop_code == "delivery_verification.repair_required"
    repaired = _repair(path, cfg, _LLM(_draft()), node_id="root")
    assert repaired.ready, repaired
    status = get_delivery_status(path)
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    assert preview_delivery_run_next(path).selected_unit.id == repaired.unit_id
    _complete(path, repaired.unit_id)
    result, _ = _verify_node(path, cfg, node_id="root")
    assert result.succeeded, result


def test_changed_executed_contract_stops_before_checkpoint_provider(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    (path.parent / "read.md").write_text(_CONTRACT + "\nChanged requirement.\n")
    result, llm = _verify_node(path, cfg)
    assert result.stop_code == "delivery_checkpoint.evidence_unavailable"
    assert not llm.calls


@pytest.mark.parametrize("nested", [False, True])
@pytest.mark.parametrize("change", ["result", "restored", "candidate", "same", "crlf", "absent"])
def test_initial_checkpoint_binds_candidate_contracts_to_child_evidence(checkpoint_plan, nested, change) -> None:
    from core.delivery_checkpoints import checkpoint_preflight_issue
    from core.state import TaskState

    path, cfg = checkpoint_plan
    repo = path.parent
    if nested:
        project = repo / "apps/service"
        project.mkdir(parents=True)
        for name in ("src", "read.md", "write.md", "consumer.md", "source.md", "plan.yaml", ".gitignore"):
            (repo / name).rename(project / name)
        path = project / "plan.yaml"
        cfg["project"]["root_path"] = str(project)
    root = path.parent
    data = yaml.safe_load(path.read_text())
    for unit in data["units"][:2]:
        unit["scope_paths"].append("read.md")
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    # Keep CRLF bytes in Git too, independently of the host's checkout settings.
    _git(repo, "config", "core.autocrlf", "false")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "contract authority test setup")
    base = _git(repo, "rev-parse", "HEAD")
    operator_branch = _git(repo, "branch", "--show-current")
    _git(repo, "checkout", "-b", "child-results")
    contract = root / "read.md"
    if change in {"result", "restored"}:
        contract.write_text(_CONTRACT + "\nChanged child requirement.\n")
    elif change == "crlf":
        contract.write_bytes(("\n" + _CONTRACT + "\n").replace("\n", "\r\n").encode())
    elif change == "absent":
        contract.unlink()
    (root / "src/cache.py").write_text("cache = {'read': True}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "read child result")
    read_commit = _git(repo, "rev-parse", "HEAD")
    if change == "restored":
        contract.write_bytes(_CONTRACT.encode())
    elif change == "candidate":
        contract.write_text(_CONTRACT + "\nChanged by another checkpoint member.\n")
    (root / "src/cache.py").write_text("cache = {'read': True, 'write': True}\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "write child result")
    write_commit = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", operator_branch)
    assert contract.read_text() == _CONTRACT

    store = JsonStateStore(root / ".sikula/state")
    child = TaskState(
        task_id="read-child",
        task_description=_CONTRACT,
        delivery_plan_id="cache",
        delivery_unit_id="read",
        delivery_plan_path="plan.yaml",
        done=True,
        result_commit=read_commit,
    )
    store.save(child)
    write_delivery_progress(
        delivery_progress_path(root, "cache"),
        DeliveryProgress(
            schema_version=1,
            plan_id="cache",
            assembly_base_commit=base,
            units=[
                make_delivery_unit_progress("read", "done", commit=read_commit, child_task_id=child.task_id),
                make_delivery_unit_progress("write", "done", commit=write_commit),
            ],
        ),
    )
    status = verification_node_status(get_delivery_status(path, project_root=root), "storage")
    assert status.valid, status.errors
    issue = checkpoint_preflight_issue(status, cfg, store)
    if change in {"result", "restored"}:
        assert issue.code == "delivery_checkpoint.evidence_unavailable"
    else:
        assert issue is None
    result, llm = _verify_node(path, cfg)
    status = get_delivery_status(path, project_root=root)
    if change in {"result", "restored", "candidate"}:
        assert result.stop_code == "delivery_checkpoint.evidence_unavailable", result
        assert not llm.calls
        assert not status.checkpoint_verifications
        assert not preview_delivery_run_next(path, project_root=root).ready
        # An already assembled invalid candidate must also fail the next preflight.
        issue = checkpoint_preflight_issue(verification_node_status(status, "storage"), cfg, store)
        assert issue.code == "delivery_checkpoint.evidence_unavailable"
    else:
        assert result.succeeded, result
        assert llm.calls
        assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
        assert preview_delivery_run_next(path, project_root=root).selected_unit.id == "consumer"


def test_two_checkpoints_keep_independent_persistent_repair_budgets(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    (root / "client.md").write_text(_CONTRACT)
    data["units"].append(
        {"id": "client", "task_path": "client.md", "depends_on": ["consumer"], "scope_paths": ["src/"]}
    )
    data["checkpoints"].append(
        {"id": "consumer-boundary", "unit_ids": ["read", "write", "consumer"], "obligation_ids": ["consistent-read"]}
    )
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "second boundary")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]),
    )
    assert _verify_node(path, cfg, "repair_required")[0].stop_code == "delivery_verification.repair_required"
    first = _repair(path, cfg, _LLM(_draft()), node_id="storage")
    assert first.ready, first
    _complete(path, first.unit_id)
    assert _verify_node(path, cfg)[0].succeeded
    _complete(path, "consumer")
    second_gate, _ = _verify_node(path, cfg, "repair_required", node_id="consumer-boundary")
    assert second_gate.stop_code == "delivery_verification.repair_required", second_gate
    second = _repair(path, cfg, _LLM(_draft()), node_id="consumer-boundary")
    assert second.ready, second
    assert first.unit_id != second.unit_id
    _complete(path, second.unit_id)
    assert _verify_node(path, cfg, node_id="consumer-boundary")[0].succeeded
    assert preview_delivery_run_next(path).selected_unit.id == "client"
    for node_id in ("storage", "consumer-boundary"):
        exhausted = coordinate_delivery_repair(
            path, cfg, project_root=root, agent_factory=None, dry_run=True, node_id=node_id
        )
        assert exhausted.issue.code == "delivery_repair.budget_exhausted"
    _complete(path, "client")
    result, _ = _verify_node(path, cfg, node_id="root")
    assert result.succeeded, result


@pytest.mark.parametrize("extra_member", [False, True])
def test_repair_extends_unaccepted_covering_groups_without_reopening_accepted_groups(
    checkpoint_plan, extra_member: bool
) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    members = ["read", "write"]
    if extra_member:
        (root / "independent.md").write_text(_CONTRACT)
        data["units"].append({"id": "independent", "task_path": "independent.md", "scope_paths": ["src/"]})
        data["units"][2]["depends_on"].append("independent")
        members.append("independent")
    data["checkpoints"].append({"id": "combined", "unit_ids": members, "obligation_ids": ["consistent-read"]})
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "covering boundary without internal consumer")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]),
    )
    assert _verify_node(path, cfg, "repair_required")[0].stop_code == "delivery_verification.repair_required"
    first = _repair(path, cfg, _LLM(_draft()), node_id="storage")
    assert first.ready, first
    status = get_delivery_status(path)
    assert first.unit_id in status.plan.checkpoints[1].unit_ids
    assert preview_delivery_run_next(path).selected_unit.id in {first.unit_id, "independent"}
    _complete(path, first.unit_id)
    assert _verify_node(path, cfg)[0].succeeded
    if extra_member:
        _complete(path, "independent")
    assert (
        _verify_node(path, cfg, "repair_required", node_id="combined")[0].stop_code
        == "delivery_verification.repair_required"
    )
    author = _LLM(_draft())
    if extra_member:
        with (
            patch("core.delivery_repair.assemble_delivery_artifacts", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            _repair(path, cfg, author, node_id="combined")
    second = _repair(path, cfg, author, node_id="combined")
    assert second.ready, second
    assert len(author.calls) == 1
    status = get_delivery_status(path)
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    assert second.unit_id not in status.plan.checkpoints[0].unit_ids
    _complete(path, second.unit_id)
    assert _verify_node(path, cfg, node_id="combined")[0].succeeded
    assert preview_delivery_run_next(path).selected_unit.id == "consumer"
    _complete(path, "consumer")
    assert _verify_node(path, cfg, node_id="root")[0].succeeded


@pytest.mark.parametrize("stale", [False, True])
@pytest.mark.parametrize("interrupt", ["none", "authoring", "publication"])
def test_repair_propagates_to_stale_covering_checkpoint(checkpoint_plan, stale: bool, interrupt: str) -> None:
    from core.delivery_repair_storage import read_repair_state

    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    data["checkpoints"].append({"id": "combined", "unit_ids": ["read", "write"], "obligation_ids": ["consistent-read"]})
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "covering checkpoint")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]),
    )
    assert _verify_node(path, cfg, node_id="combined")[0].succeeded
    if stale:
        cfg["agents"] = {"reviewer": {"llm": {"model": "updated-reviewer"}}}
    assert _verify_node(path, cfg, "repair_required")[0].stop_code == "delivery_verification.repair_required"
    status = get_delivery_status(path)
    assert status.checkpoint_verifications["combined"].passed
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[1], cfg) == (not stale)
    preview = coordinate_delivery_repair(
        path, cfg, project_root=root, agent_factory=None, dry_run=True, node_id="storage"
    )
    assert preview.ready, preview
    state_path = root / ".sikula/state/delivery/cache/checkpoint-storage-integration-repair.json"
    assert not state_path.exists()
    author = _LLM(_draft())
    if interrupt != "none":
        target = (
            "agents.delivery_repair_agent.DeliveryRepairAgent.author"
            if interrupt == "authoring"
            else "core.delivery_repair.assemble_delivery_artifacts"
        )
        with patch(target, side_effect=KeyboardInterrupt), pytest.raises(KeyboardInterrupt):
            _repair(path, cfg, author, node_id="storage")
    repaired = _repair(path, cfg, author, node_id="storage")
    assert repaired.ready, repaired
    assert len(author.calls) == 1
    updated = yaml.safe_load(path.read_text())
    combined = next(item for item in updated["checkpoints"] if item["id"] == "combined")
    assert (repaired.unit_id in combined["unit_ids"]) == stale
    state = read_repair_state(root, state_path)
    assert state["accepted_checkpoints"] == ([] if stale else ["combined"])
    assert state["attempts"] == (2 if interrupt == "authoring" else 1)
    _complete(path, repaired.unit_id)
    assert _verify_node(path, cfg)[0].succeeded
    if stale:
        assert not preview_delivery_run_next(path).ready
        assert _verify_node(path, cfg, node_id="combined")[0].succeeded
    assert preview_delivery_run_next(path).selected_unit.id == "consumer"


def test_crossing_checkpoint_repair_cannot_rewrite_an_already_published_repair(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    (root / "independent.md").write_text(_CONTRACT)
    data["units"].append({"id": "independent", "task_path": "independent.md", "scope_paths": ["src/"]})
    data["units"][2]["depends_on"].append("independent")
    data["obligations"][0]["unit_ids"] = ["read"]
    data["obligations"].append({**data["obligations"][0], "id": "write-result", "unit_ids": ["write"]})
    for record in data["source_accounting"]:
        record["obligation_ids"].append("write-result")
    data["checkpoints"].append(
        {"id": "crossing", "unit_ids": ["write", "independent"], "obligation_ids": ["write-result"]}
    )
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "crossing independent boundary")
    commit = _git(root, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]),
    )
    assert _verify_node(path, cfg, "repair_required")[0].stop_code == "delivery_verification.repair_required"
    first = _repair(path, cfg, _LLM(_draft()), node_id="storage")
    assert first.ready, first
    _complete(path, "independent")
    assessment = json.loads(_assessment())
    assessment["findings"][0].update(unit_ids=["write"], obligation_ids=["write-result"])
    assessment["obligation_results"][0]["id"] = "write-result"
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            state_store=JsonStateStore(root / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(_LLM(json.dumps(assessment)), cfg),
            security_reviewer=None,
            node_id="crossing",
        )
    assert result.stop_code == "delivery_verification.repair_required", result
    before = path.read_bytes()
    preview = coordinate_delivery_repair(
        path, cfg, project_root=root, agent_factory=None, dry_run=True, node_id="crossing"
    )
    assert preview.issue.code == "delivery_repair.repair_lineage_bound"
    author = _LLM(_draft())
    blocked = _repair(path, cfg, author, node_id="crossing")
    assert blocked.issue.code == preview.issue.code
    assert not author.calls
    assert path.read_bytes() == before
    assert not (root / ".sikula/state/delivery/cache/checkpoint-crossing-integration-repair.json").exists()
    assert get_delivery_status(path).valid


@pytest.mark.parametrize("interrupt_at", ["authoring", "publication", "verification"])
def test_checkpoint_interruption_resumes_without_duplicate_repair(
    checkpoint_plan, monkeypatch: pytest.MonkeyPatch, interrupt_at: str
) -> None:
    import core.delivery_repair as repair_module
    from core.delivery_repair_storage import read_repair_state

    path, cfg = checkpoint_plan
    root = path.parent
    if interrupt_at == "verification":
        with (
            patch("core.delivery_verify.run_delivery_verification_validation", side_effect=KeyboardInterrupt),
            pytest.raises(KeyboardInterrupt),
        ):
            verify_delivery_plan(
                path,
                cfg,
                state_store=JsonStateStore(root / ".sikula/state"),
                semantic_reviewer=DeliveryIntegrationReviewAgent(_LLM(_assessment()), cfg),
                security_reviewer=None,
                node_id="storage",
            )
        assert get_delivery_status(path).checkpoint_verifications["storage"].status == "interrupted"
    assert _verify_node(path, cfg, "repair_required")[0].stop_code == "delivery_verification.repair_required"
    author = (
        _LLM(RuntimeError("provider temporarily unavailable"), _draft())
        if interrupt_at == "authoring"
        else _LLM(_draft())
    )
    if interrupt_at == "publication":
        with patch.object(repair_module, "assemble_delivery_artifacts", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                _repair(path, cfg, author, node_id="storage")
        assert not preview_delivery_run_next(path).ready
    elif interrupt_at == "authoring":
        failed = _repair(path, cfg, author, node_id="storage")
        assert failed.issue.code == "delivery_repair.provider_failed"
    resumed = _repair(path, cfg, author, node_id="storage")
    assert resumed.ready, resumed
    assert len(author.calls) == (2 if interrupt_at == "authoring" else 1)
    state = read_repair_state(root, root / ".sikula/state/delivery/cache/checkpoint-storage-integration-repair.json")
    assert state["attempts"] == len(author.calls)
    assert state["phase"] == "published"
    assert sum(unit.id == resumed.unit_id for unit in get_delivery_status(path).plan.units) == 1
    _complete(path, resumed.unit_id)
    assert _verify_node(path, cfg)[0].succeeded
    assert preview_delivery_run_next(path).selected_unit.id == "consumer"


def test_consumer_waits_for_entire_group_even_if_its_direct_dependency_is_done(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    data = yaml.safe_load(path.read_text())
    consumer = data["units"].pop(2)
    consumer["depends_on"] = ["read"]
    data["units"].insert(1, consumer)  # Ready consumer precedes the unfinished group member.
    path.write_text(yaml.safe_dump(data))
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(progress_path, replace(progress, units=[progress.units[0]]))
    status = get_delivery_status(path)
    consumer_status = next(unit for unit in status.units if unit.id == "consumer")
    assert not consumer_status.eligible
    assert consumer_status.to_dict()["blocked_by_checkpoints"] == ["storage"]
    assert preview_delivery_run_next(path).selected_unit.id == "write"


def test_checkpoint_status_projects_changed_review_policy_as_stale(checkpoint_plan) -> None:
    from core.delivery_verification import with_delivery_verification_readiness

    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    cfg["agents"] = {"reviewer": {"llm": {"model": "changed-model"}}}
    status = with_delivery_verification_readiness(get_delivery_status(path), cfg)
    assert status.to_dict()["checkpoints"][0]["status"] == "stale"
    assert not next(unit for unit in status.units if unit.id == "consumer").eligible


@pytest.mark.parametrize(
    "code,security",
    [("readonly_mutation", "blocked"), ("external_dependency_gap", "not_run"), ("repair_required", "rejected")],
)
@pytest.mark.parametrize("stale", [False, True])
def test_checkpoint_terminal_verdict_preempts_further_providers(
    checkpoint_plan, code: str, security: str, stale: bool
) -> None:
    from sikula_cli.delivery import _run_delivery_plan

    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    record = replace(
        progress.checkpoint_verifications["storage"],
        status="failed",
        semantic_status="rejected",
        security_status=security,
        stop_code="delivery_verification." + code,
        obligation_satisfied_count=0,
        obligation_gap_count=1,
    )
    write_delivery_progress(progress_path, replace(progress, checkpoint_verifications={"storage": record}))
    if stale:
        path.write_text(path.read_text() + "\n# metadata edit\n")

    def forbidden(*args):
        pytest.fail("Terminal checkpoint must preempt providers")

    context = DeliveryRunNextContext(
        run_task=forbidden,
        resolve_state_dir=lambda cfg: path.parent / ".sikula/state",
        state_store=JsonStateStore(path.parent / ".sikula/state"),
        verify_plan=forbidden,
        repair_agent_factory=forbidden,
    )
    args = argparse.Namespace(plan_file=str(path), max_units=5, max_elapsed_minutes=None)
    result = _run_delivery_plan(args, cfg, context, project_root=path.parent)
    assert not result.succeeded
    assert result.stop_code == (
        "delivery_verification.security_rejected" if security == "rejected" else "delivery_verification." + code
    )
    direct, llm = _verify_node(path, cfg)
    assert not direct.succeeded
    assert direct.stop_code == result.stop_code
    assert not llm.calls
    assert read_delivery_progress(progress_path, plan_id="cache")[0].checkpoint_verifications["storage"] == record


def test_checkpoint_runs_ordinary_validation_and_reserves_final_checks_for_root(checkpoint_plan) -> None:
    from core.delivery_verification_validation import run_delivery_verification_validation
    from tests.test_delivery_verification import _BuildTool

    path, cfg = checkpoint_plan
    cfg["delivery"] = {"verification": {"final_checks": [{"name": "complete-product", "command": "python final.py"}]}}
    tool = _BuildTool()
    with patch("core.delivery_verification_validation.create_build_tool", return_value=tool):
        checkpoint = run_delivery_verification_validation(path.parent, cfg, reusable=None, include_final_checks=False)
        assert checkpoint.passed
        assert "test" in tool.calls
        assert "check:complete-product" not in tool.calls
        assert checkpoint.to_review_dict(cfg, include_final_checks=False)["policy"]["final_checks"] == []
        final = run_delivery_verification_validation(path.parent, cfg, reusable=None)
        assert final.passed
        assert "check:complete-product" in tool.calls


def test_unbound_later_repair_cannot_evade_future_contributor_validation(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    data = yaml.safe_load(path.read_text())
    identity = "integration-repair-" + "a" * 16
    data["units"].append(
        {"id": identity, "task_path": "read.md", "depends_on": ["read", "write", "consumer"], "repair_node": "root"}
    )
    data["obligations"][0]["unit_ids"].append(identity)
    path.write_text(yaml.safe_dump(data))
    status = get_delivery_status(path)
    assert any(issue.code == "delivery_checkpoint.repair_unbound" for issue in status.errors)
    assert not preview_delivery_run_next(path).ready


def test_checkpoint_ids_do_not_alias_on_case_insensitive_filesystems() -> None:
    units, obligations, declarations = _declarations()
    declarations.append({**declarations[0], "id": "Storage"})
    with pytest.raises(DeliveryCheckpointError):
        parse_delivery_checkpoints(declarations, units, obligations)


def test_checkpoint_accepts_completed_noop_with_immutable_base_contract(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path, replace(progress, units=[replace(unit, commit=None) for unit in progress.units])
    )
    result, _ = _verify_node(path, cfg)
    assert result.succeeded, result


def test_checkpoint_legacy_contract_evidence_uses_nested_project_prefix(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    nested = root / "apps/service"
    nested.mkdir(parents=True)
    for name in ("src", "read.md", "write.md", "consumer.md", "source.md", "plan.yaml"):
        (root / name).rename(nested / name)
    _git(root, "add", ".")
    _git(root, "commit", "-m", "nested project")
    commit = _git(root, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(nested, "cache"),
        DeliveryProgress(
            schema_version=1,
            plan_id="cache",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress(key, "done", commit=commit) for key in ("read", "write")],
        ),
    )
    cfg["project"]["root_path"] = str(nested)
    result, _ = _verify_node(nested / "plan.yaml", cfg)
    assert result.succeeded, result


@pytest.mark.parametrize("change_during_review", ["none", "comment", "missing"])
def test_provider_reported_readonly_violation_is_a_durable_checkpoint_stop(
    checkpoint_plan, change_during_review: str
) -> None:
    from core.llm_client import LLMReadOnlyViolation
    from sikula_cli.delivery import _run_delivery_plan

    path, cfg = checkpoint_plan
    original_plan = path.read_text()

    class ViolatingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            if change_during_review == "comment":
                path.write_text(path.read_text() + "\n# changed during review\n")
            elif change_during_review == "missing":
                path.unlink()
            return super().run_readonly_agent(prompt, cwd)

    llm = ViolatingLLM(LLMReadOnlyViolation("Disposable provider workspace changed"))
    store = JsonStateStore(path.parent / ".sikula/state")
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            state_store=store,
            semantic_reviewer=DeliveryIntegrationReviewAgent(llm, cfg),
            security_reviewer=None,
            node_id="storage",
        )
    assert result.stop_code == "delivery_verification.readonly_mutation"
    assert len(llm.calls) == 1
    if change_during_review == "missing":
        path.write_text(original_plan)
    assert get_delivery_status(path).checkpoint_verifications["storage"].stop_code == result.stop_code

    def forbidden(*args):
        pytest.fail("Known read-only violation cannot invoke another provider")

    context = DeliveryRunNextContext(
        run_task=forbidden,
        resolve_state_dir=lambda cfg: path.parent / ".sikula/state",
        state_store=store,
        verify_plan=forbidden,
        repair_agent_factory=forbidden,
    )
    resumed = _run_delivery_plan(
        argparse.Namespace(plan_file=str(path), max_units=5, max_elapsed_minutes=None),
        cfg,
        context,
        project_root=path.parent,
    )
    assert resumed.stop_code == "delivery_verification.readonly_mutation"


@pytest.mark.parametrize("failure", ["review", "terminal", "both"])
def test_checkpoint_readonly_stop_survives_audit_failure(checkpoint_plan, failure: str) -> None:
    from core.delivery_verify import _safe_append_audit
    from core.llm_client import LLMReadOnlyViolation

    path, cfg = checkpoint_plan
    llm = _LLM(LLMReadOnlyViolation("Provider workspace changed"))
    rejected = []

    def append_audit(audit_path, entry, *, project_root):
        if (failure in {"review", "both"} and entry["event"] == "review_failed") or (
            failure in {"terminal", "both"} and entry["event"] in {"blocked", "failed"}
        ):
            rejected.append(entry)
            return False
        return _safe_append_audit(audit_path, entry, project_root=project_root)

    with (
        patch("core.delivery_verify._safe_append_audit", side_effect=append_audit),
        patch(
            "core.delivery_verify.run_delivery_verification_validation",
            return_value=DeliveryVerificationValidationResult(True, False, True),
        ),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            node_id="storage",
            state_store=JsonStateStore(path.parent / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(llm, cfg),
            security_reviewer=None,
        )
    assert rejected
    assert result.stop_code == "delivery_verification.readonly_mutation"
    record = get_delivery_status(path).checkpoint_verifications["storage"]
    assert record.stop_code == result.stop_code
    resumed, reviewer = _verify_node(path, cfg)
    assert resumed.stop_code == result.stop_code
    assert not reviewer.calls
    if failure == "review":
        audit_path = delivery_progress_path(path.parent, "cache").parent / "checkpoint-storage-verification.jsonl"
        audit = [json.loads(line) for line in audit_path.read_text().splitlines()]
        assert any(item.get("review_evidence") == rejected[0] for item in audit)


@pytest.mark.parametrize("mode", ["run_next", "run", "verify"])
@pytest.mark.parametrize("changed_authority", [False, True])
def test_checkpoint_allows_resolved_downstream_assembly_recovery(
    checkpoint_plan, mode: str, changed_authority: bool
) -> None:
    from core.delivery_finalize import assemble_delivery_candidate
    from core.delivery_progress import delivery_events_path
    from sikula_cli.delivery import _run_next_delivery_unit, _run_delivery_plan
    from tests.test_delivery_run_next import _run_next_args

    path, cfg = checkpoint_plan
    root = path.parent
    if mode != "verify":
        data = yaml.safe_load(path.read_text())
        data["units"].append(
            {"id": "after", "task_path": "after.md", "depends_on": ["consumer"], "scope_paths": ["src/"]}
        )
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        (root / "after.md").write_text(_CONTRACT)
        _git(root, "add", ".")
        _git(root, "commit", "-m", "downstream followup")
    assert _verify_node(path, cfg)[0].succeeded
    status = get_delivery_status(path)
    receipt = status.checkpoint_verifications["storage"]
    operator_branch = _git(root, "branch", "--show-current")
    _git(root, "checkout", "-b", "consumer-work", status.assembled_commit)
    (root / "src/cache.py").write_text("cache = {'consumer': True}\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "consumer work")
    consumer_commit = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", "-b", "other-work", status.assembled_commit)
    (root / "src/cache.py").write_text("cache = {'other': True}\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "concurrent downstream work")
    partial = _git(root, "rev-parse", "HEAD")
    _git(root, "checkout", operator_branch)
    _git(root, "update-ref", "refs/heads/" + status.plan.final_branch, partial)
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    progress = upsert_delivery_unit_progress(
        progress, make_delivery_unit_progress("consumer", "done", commit=consumer_commit)
    )
    write_delivery_progress(progress_path, progress)
    _, _, issue = assemble_delivery_candidate(
        root=root,
        status=get_delivery_status(path),
        progress=progress,
        progress_path=progress_path,
        events_path=delivery_events_path(root, "cache"),
    )
    assert issue.code == "delivery.assembly_conflict"
    failed_bytes = progress_path.read_bytes()
    args = _run_next_args(path)
    args.max_units = 1
    args.max_elapsed_minutes = None
    context = DeliveryRunNextContext(
        run_task=lambda *args: pytest.fail("No child provider expected"),
        resolve_state_dir=lambda cfg: root / ".sikula/state",
        state_store=JsonStateStore(root / ".sikula/state"),
    )
    blocked = _preview_delivery_run(args, cfg, context=context, project_root=root)
    assert not blocked.ready
    assert any(item.code == "delivery.assembly_conflict" for item in blocked.errors)
    if mode == "verify":
        stopped, reviewer = _verify_node(path, cfg, node_id="root")
        assert not reviewer.calls
    else:
        stopped = (_run_next_delivery_unit if mode == "run_next" else _run_delivery_plan)(
            args, cfg, context, project_root=root
        )
    assert not stopped.succeeded
    assert any(item.code == "delivery.assembly_conflict" for item in stopped.errors)
    assert progress_path.read_bytes() == failed_bytes

    tree = _git(root, "rev-parse", partial + "^{tree}")
    resolved = _git(root, "commit-tree", tree, "-p", partial, "-p", consumer_commit, "-m", "resolved conflict")
    _git(root, "update-ref", "refs/heads/" + status.plan.final_branch, resolved)
    if changed_authority:
        (root / "read.md").write_text(_CONTRACT + "\nChanged checkpoint authority.\n")
    preview = _preview_delivery_run(args, cfg, context=context, project_root=root)
    assert preview.ready is not changed_authority, preview
    if mode != "verify":
        next_preview = preview_delivery_run_next(path)
        assert next_preview.ready is not changed_authority
    assert progress_path.read_bytes() == failed_bytes

    class ReachedChild(Exception):
        pass

    if mode == "verify":
        result, reviewer = _verify_node(path, cfg, node_id="root")
        assert result.succeeded is not changed_authority, result
        assert bool(reviewer.calls) is not changed_authority
    else:
        target = (
            "sikula_cli.delivery._invoke_delivery_child_run"
            if mode == "run_next"
            else "sikula_cli.delivery._run_next_delivery_unit"
        )
        with patch(target, side_effect=ReachedChild) as invoke:
            if changed_authority:
                result = (_run_next_delivery_unit if mode == "run_next" else _run_delivery_plan)(
                    args, cfg, context, project_root=root
                )
                assert not result.succeeded
                assert any(item.code == "delivery_checkpoint.handoff_stale" for item in result.errors)
                invoke.assert_not_called()
            else:
                with pytest.raises(ReachedChild):
                    (_run_next_delivery_unit if mode == "run_next" else _run_delivery_plan)(
                        args, cfg, context, project_root=root
                    )
                if mode == "run_next":
                    assert invoke.call_args.kwargs["worktree_start_ref"] == resolved
    recovered, errors = read_delivery_progress(progress_path, plan_id="cache")
    assert not errors
    assert recovered.assembly_status == "ready"
    assert recovered.assembled_commit == resolved
    assert recovered.checkpoint_verifications["storage"] == receipt


@pytest.mark.parametrize("boundary", ["readonly", "security"])
def test_checkpoint_boundary_survives_concurrent_assembly_advance(checkpoint_plan, boundary: str) -> None:
    from core.delivery_progress import mark_delivery_assembly
    from core.llm_client import LLMReadOnlyViolation

    path, cfg = checkpoint_plan
    root = path.parent
    data = yaml.safe_load(path.read_text())
    data["units"][0]["risk_tags"] = ["privacy"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "security-sensitive checkpoint")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    commit = _git(root, "rev-parse", "HEAD")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]),
    )
    advanced = []

    class AdvancingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            current, errors = read_delivery_progress(progress_path, plan_id="cache")
            assert not errors
            tree = _git(root, "rev-parse", current.assembled_commit + "^{tree}")
            candidate = _git(root, "commit-tree", tree, "-p", current.assembled_commit, "-m", "advance assembly")
            _git(root, "update-ref", "refs/heads/sikula/delivery/cache", candidate, current.assembled_commit)
            updated = mark_delivery_assembly(
                current, base_commit=current.assembly_base_commit, assembled_commit=candidate, status="ready"
            )
            advanced.append(updated)
            write_delivery_progress(progress_path, updated)
            return super().run_readonly_agent(prompt, cwd)

    semantic = (
        AdvancingLLM(LLMReadOnlyViolation("Provider workspace changed"))
        if boundary == "readonly"
        else _LLM(_assessment("approved"))
    )
    security = AdvancingLLM(
        '{"schema_version":1,"disposition":"repair_required","summary":"Unsafe integration.",'
        '"findings":[{"code":"security_gap","summary":"Boundary violated.","unit_ids":["read"]}]}'
    )
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            project_root=root,
            node_id="storage",
            state_store=JsonStateStore(root / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(semantic, cfg),
            security_reviewer=DeliveryIntegrationReviewAgent(security, cfg),
        )
    assert not result.succeeded
    saved, errors = read_delivery_progress(progress_path, plan_id="cache")
    assert not errors
    record = saved.checkpoint_verifications["storage"]
    captured = advanced[0].checkpoint_verifications["storage"]
    assert record.status in {"blocked", "failed"}
    assert (record.gate_id, record.attempt, record.candidate_commit) == (
        captured.gate_id,
        captured.attempt,
        captured.candidate_commit,
    )
    assert saved.assembled_commit == advanced[0].assembled_commit != record.candidate_commit
    assert saved.units == advanced[0].units
    assert saved.verification is None
    assert record.stop_code == (
        "delivery_verification.readonly_mutation" if boundary == "readonly" else "delivery_verification.repair_required"
    )
    if boundary == "security":
        assert record.security_status == "rejected"
    audit = [
        json.loads(line)
        for line in (progress_path.parent / "checkpoint-storage-verification.jsonl").read_text().splitlines()
    ]
    assert any(item.get("record") == record.to_dict() for item in audit)
    resumed, llm = _verify_node(path, cfg)
    assert resumed.stop_code == (
        "delivery_verification.readonly_mutation"
        if boundary == "readonly"
        else "delivery_verification.security_rejected"
    )
    assert not llm.calls
    assert read_delivery_progress(progress_path, plan_id="cache")[0] == saved


@pytest.mark.parametrize("role", ["reviewer", "security_reviewer"])
@pytest.mark.parametrize("change", ["same", "changed", "missing"])
def test_checkpoint_receipt_binds_candidate_review_rules(checkpoint_plan, role: str, change: str) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    rules = root / "rules.md"
    _git(root, "config", "core.autocrlf", "true")
    rules.write_bytes(b"Review storage invariants.\r\n")
    cfg[role] = {"extra_rules": "rules.md"}
    if role == "security_reviewer":
        data = yaml.safe_load(path.read_text())
        data["units"][0]["risk_tags"] = ["privacy"]
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(root, "add", ".")
    _git(root, "commit", "-m", "review authority")
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    commit = _git(root, "rev-parse", "HEAD")
    write_delivery_progress(
        progress_path,
        replace(progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]),
    )
    llm = _LLM(_assessment("approved"))
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            cfg,
            project_root=root,
            node_id="storage",
            state_store=JsonStateStore(root / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(llm, cfg),
            security_reviewer=DeliveryIntegrationReviewAgent(
                _LLM('{"schema_version":1,"disposition":"approved","summary":"Secure.","findings":[]}'), cfg
            ),
        )
    assert result.succeeded, result
    status = get_delivery_status(path)
    record = status.checkpoint_verifications["storage"]
    assert record.review_rule_fingerprints == {
        "rules.md": "sha256:" + sha256(b"Review storage invariants.\n").hexdigest()
    }
    assert "review_rule_fingerprints" not in json.dumps(status.to_dict())
    _git(root, "checkout", status.plan.final_branch)
    if change == "changed":
        rules.write_text("Review stricter storage invariants.\n")
    elif change == "missing":
        rules.unlink()
    (root / "src/cache.py").write_text("cache = {'downstream': True}\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "downstream candidate")
    candidate = _git(root, "rev-parse", "HEAD")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(progress_path, replace(progress, assembled_commit=candidate))
    # The operator's file is not authority for rules in the assembled candidate.
    rules.write_text("Operator checkout differs.\n")
    status = get_delivery_status(path)
    assert status.valid, status.errors
    for effective_cfg in (cfg, None):
        assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], effective_cfg) == (change == "same")


@pytest.mark.parametrize("change", ["none", "contract", "missing_plan"])
@pytest.mark.parametrize("reset_failed", [False, True])
def test_direct_child_resume_checks_parent_checkpoint_before_mutation(
    checkpoint_plan, change: str, reset_failed: bool, capsys
) -> None:
    from core.state import TaskState
    from tests.test_sikula_run import _run_args
    from sikula import cmd_run

    path, cfg = checkpoint_plan
    root = path.parent
    assert _verify_node(path, cfg)[0].succeeded
    store = JsonStateStore(root / ".sikula/state")
    state = TaskState(
        task_id="consumer-child",
        task_description=_CONTRACT,
        delivery_plan_id="cache",
        delivery_unit_id="consumer",
        delivery_plan_path="plan.yaml",
        failed=reset_failed,
        worktree_path=str(root),
    )
    store.save(state)
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        upsert_delivery_unit_progress(
            progress,
            make_delivery_unit_progress(
                "consumer", "failed" if reset_failed else "running", child_task_id=state.task_id
            ),
        ),
    )
    if change == "contract":
        (root / "read.md").write_text(_CONTRACT + "\nChanged authority.\n")
    elif change == "missing_plan":
        path.unlink()

    class ReachedResume(Exception):
        pass

    with (
        patch("sikula.build_orchestrator", side_effect=ReachedResume) as build,
        patch("sikula._reset_failed_state", side_effect=ReachedResume) as reset,
    ):
        if change == "none":
            with pytest.raises(ReachedResume):
                cmd_run(_run_args(task_id=state.task_id, reset_failed=reset_failed), cfg)
            assert reset.called if reset_failed else build.called
        else:
            with pytest.raises(SystemExit) as exc:
                cmd_run(_run_args(task_id=state.task_id, reset_failed=reset_failed), cfg)
            assert exc.value.code == 1
            build.assert_not_called()
            reset.assert_not_called()
            assert "delivery_checkpoint." in capsys.readouterr().out
            assert store.load(state.task_id) == state


@pytest.mark.parametrize("role", ["reviewer", "security_reviewer"])
@pytest.mark.parametrize(
    "flag,value,changed", [("agent_model", "checkpoint-model", "another-model"), ("agent_provider", "claude", "gemini")]
)
@pytest.mark.parametrize("reset_failed", [False, True])
def test_direct_child_resume_uses_effective_reviewer_overrides(
    checkpoint_plan, role: str, flag: str, value: str, changed: str, reset_failed: bool, capsys
) -> None:
    from core.state import TaskState
    from sikula import _delivery_verification_effective_config, cmd_run
    from tests.test_sikula_run import _run_args

    path, cfg = checkpoint_plan
    root = path.parent
    cfg["agents"] = {role: {"llm": {"temperature": 0.2, "max_tokens": 2048}, "enabled": True}}
    original_agents = deepcopy(cfg["agents"])
    if role == "security_reviewer":
        data = yaml.safe_load(path.read_text())
        data["units"][0]["risk_tags"] = ["privacy"]
        path.write_text(yaml.safe_dump(data, sort_keys=False))
        _git(root, "add", ".")
        _git(root, "commit", "-m", "security-sensitive checkpoint")
        progress_path = delivery_progress_path(root, "cache")
        progress, _ = read_delivery_progress(progress_path, plan_id="cache")
        commit = _git(root, "rev-parse", "HEAD")
        write_delivery_progress(
            progress_path,
            replace(
                progress, assembly_base_commit=commit, units=[replace(unit, commit=commit) for unit in progress.units]
            ),
        )
    gate_cfg = _delivery_verification_effective_config(_run_args(**{flag: [f"{role}={value}"]}), cfg)
    store = JsonStateStore(root / ".sikula/state")
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        result = verify_delivery_plan(
            path,
            gate_cfg,
            project_root=root,
            node_id="storage",
            state_store=store,
            semantic_reviewer=DeliveryIntegrationReviewAgent(_LLM(_assessment("approved")), gate_cfg),
            security_reviewer=DeliveryIntegrationReviewAgent(
                _LLM('{"schema_version":1,"disposition":"approved","summary":"Secure.","findings":[]}'), gate_cfg
            ),
        )
    assert result.succeeded, result
    state = TaskState(
        task_id="consumer-child",
        task_description=_CONTRACT,
        delivery_plan_id="cache",
        delivery_unit_id="consumer",
        delivery_plan_path="plan.yaml",
        failed=reset_failed,
        worktree_path=str(root),
    )
    store.save(state)
    progress_path = delivery_progress_path(root, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        upsert_delivery_unit_progress(
            progress,
            make_delivery_unit_progress(
                "consumer", "failed" if reset_failed else "running", child_task_id=state.task_id
            ),
        ),
    )

    class ReachedResume(Exception):
        pass

    for override in (value, changed, None):
        resume_cfg = deepcopy(cfg)
        args = _run_args(
            task_id=state.task_id,
            reset_failed=reset_failed,
            **{flag: [f"{role}={override}"] if override is not None else None},
        )
        with (
            patch("sikula.build_orchestrator", side_effect=ReachedResume) as build,
            patch("sikula._reset_failed_state", side_effect=ReachedResume) as reset,
        ):
            if override == value:
                with pytest.raises(ReachedResume):
                    cmd_run(args, resume_cfg)
                assert reset.called if reset_failed else build.called
            else:
                with pytest.raises(SystemExit) as exc:
                    cmd_run(args, resume_cfg)
                assert exc.value.code == 1
                assert "delivery_checkpoint.handoff_stale" in capsys.readouterr().out
                build.assert_not_called()
                reset.assert_not_called()
        assert resume_cfg["agents"] == original_agents
    assert cfg["agents"] == original_agents


@pytest.mark.parametrize("link", ["file", "directory", "escape", "cycle"])
def test_candidate_rule_hashes_resolve_links_within_nested_project(tmp_path: Path, link: str) -> None:
    from core.delivery_checkpoints import checkpoint_review_rule_fingerprints

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "core.autocrlf", "true")
    root = tmp_path / "app"
    (root / "rules").mkdir(parents=True)
    target = root / "rules/review.md"
    target.write_bytes(b"Original rules.\r\n")
    (tmp_path / "outside.md").write_text("Outside the project.\n")
    alias = root / "alias"
    destination = {"file": "rules/review.md", "directory": "rules", "escape": "../outside.md", "cycle": "alias"}[link]
    try:
        alias.symlink_to(destination, target_is_directory=link == "directory")
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks unavailable")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "nested review rules")
    candidate = _git(tmp_path, "rev-parse", "HEAD")
    path = "alias/review.md" if link == "directory" else "alias"
    if link in {"escape", "cycle"}:
        with pytest.raises(ValueError):
            checkpoint_review_rule_fingerprints(root, candidate, [path])
        return
    expected = {path: "sha256:" + sha256(b"Original rules.\n").hexdigest()}
    target.write_text("Changed rules.\n")
    assert checkpoint_review_rule_fingerprints(root, candidate, [path]) == expected
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "changed rule target")
    updated = _git(tmp_path, "rev-parse", "HEAD")
    assert checkpoint_review_rule_fingerprints(root, updated, [path]) != expected


def test_checkpoint_without_rule_binding_requires_new_verification(checkpoint_plan) -> None:
    path, cfg = checkpoint_plan
    assert _verify_node(path, cfg)[0].succeeded
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    old = replace(progress.checkpoint_verifications["storage"], review_rule_fingerprints=None)
    write_delivery_progress(progress_path, replace(progress, checkpoint_verifications={"storage": old}))
    status = get_delivery_status(path)
    assert not checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
    result, llm = _verify_node(path, cfg)
    assert result.succeeded, result
    assert len(llm.calls) == 1
    status = get_delivery_status(path)
    assert checkpoint_pass_is_usable(status, status.plan.checkpoints[0], cfg)
