"""Lazy barrier traversal retains ordering, recovery and evidence boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from core.delivery_checkpoint_model import DeliveryCheckpoint
from core.delivery_checkpoints import checkpoint_barrier_issue
from core.delivery_evidence_resolution import EVIDENCE_BUDGET_STOP, EvidenceResolver
from core.delivery_repair_storage import write_repair_state


def _status(nodes: list[DeliveryCheckpoint]) -> SimpleNamespace:
    return SimpleNamespace(
        valid=True,
        plan=SimpleNamespace(checkpoints=nodes, units=[]),
        units=[],
        verification=None,
        checkpoint_verifications={
            node.id: SimpleNamespace(passed=True, status="passed", reverification=None, security_status="approved")
            for node in nodes
        },
    )


@pytest.mark.parametrize("depth", [2, 16])
@pytest.mark.parametrize("parent_first", [False, True])
def test_barriers_validate_each_needed_node_once_and_start_fresh(depth: int, parent_first: bool) -> None:
    nodes = [DeliveryCheckpoint(str(i), (), (), parent_id=str(i - 1) if i else "root") for i in range(depth)]
    status = _status(nodes if parent_first else list(reversed(nodes)))
    with (
        patch("core.delivery_checkpoints._checkpoint_local_pass_is_usable", return_value=True) as check,
        patch("core.delivery_checkpoints.verification_node_status", return_value=status),
        patch("core.delivery_checkpoints.verification_scope_complete", return_value=True),
    ):
        assert checkpoint_barrier_issue(status, {}) is None
        assert [call.args[1].id for call in check.call_args_list] == [node.id for node in reversed(nodes)]
        resolver = check.call_args.kwargs["resolver"]
        assert all(call.kwargs["resolver"] is resolver for call in check.call_args_list)
        check.reset_mock()
        check.return_value = False
        assert checkpoint_barrier_issue(status, {}).code == "delivery_checkpoint.required"
        assert [call.args[1].id for call in check.call_args_list] == [nodes[-1].id]
        assert check.call_args.kwargs["resolver"] is not resolver


@pytest.mark.parametrize("failure", [False, OSError("missing"), ValueError("invalid"), RuntimeError("unavailable")])
def test_failed_subtree_is_not_rechecked_and_independent_siblings_remain_checkable(failure: bool | Exception) -> None:
    nodes = [
        DeliveryCheckpoint("parent", (), ()),
        DeliveryCheckpoint("leaf", (), (), parent_id="parent"),
        DeliveryCheckpoint("sibling", (), ()),
    ]
    status = _status(nodes)

    def check(status: SimpleNamespace, node: DeliveryCheckpoint, cfg: dict, **kwargs: object) -> bool:
        if node.id == "leaf":
            if isinstance(failure, Exception):
                raise failure
            return False
        assert node.id == "sibling", "A rejected child cannot authorize its parent review"
        return True

    with patch("core.delivery_checkpoints._checkpoint_local_pass_is_usable", side_effect=check) as checked:
        assert checkpoint_barrier_issue(status, {}, unit_id="independent") is None
        assert [call.args[1].id for call in checked.call_args_list] == ["leaf", "sibling"]


@pytest.mark.parametrize("first", ["parent", "leaf"])
def test_first_declared_blocker_does_not_read_later_subtrees(first: str) -> None:
    parent = DeliveryCheckpoint("parent", (), ())
    leaf = DeliveryCheckpoint("leaf", (), (), parent_id="parent")
    sibling = DeliveryCheckpoint("sibling", (), ())
    status = _status([parent, leaf, sibling] if first == "parent" else [leaf, parent, sibling])
    status.checkpoint_verifications[first].passed = False
    visited = []

    def guarded(node: DeliveryCheckpoint, units: list) -> set[str]:
        visited.append(node.id)
        return {"consumer"}

    with (
        patch("core.delivery_checkpoints._checkpoint_local_pass_is_usable", side_effect=AssertionError("unneeded")),
        patch("core.delivery_checkpoints._checkpoint_runtime_consumers", side_effect=guarded),
    ):
        assert checkpoint_barrier_issue(status, {}, unit_id="consumer").code == "delivery_checkpoint.required"
    assert visited == [first]


@pytest.mark.parametrize("reconciled", [False, True])
def test_started_consumer_reconciliation_precedes_required_barrier(reconciled: bool) -> None:
    status = _status([DeliveryCheckpoint("group", (), ())])
    status.checkpoint_verifications["group"].passed = False
    status.units = [SimpleNamespace(id="consumer", status="done")]
    with (
        patch("core.delivery_checkpoints._checkpoint_runtime_consumers", return_value={"consumer"}),
        patch("core.delivery_repair.candidate_repair_reconciles_checkpoint", return_value=reconciled) as recovery,
    ):
        issue = checkpoint_barrier_issue(status, {}, unit_id="consumer")
        assert issue.code == ("delivery_checkpoint.required" if reconciled else "delivery_checkpoint.handoff_stale")
        recovery.assert_called_once_with(status, "group")


@pytest.mark.parametrize("failure", ["budget", "changed_success", "changed_blocker"])
def test_barrier_returns_cannot_hide_resource_or_integrity_failures(tmp_path: Path, failure: str) -> None:
    nodes = [DeliveryCheckpoint("parent", (), ()), DeliveryCheckpoint("leaf", (), (), parent_id="parent")]
    status = _status(nodes)
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"accepted": True})

    def check(status: SimpleNamespace, node: DeliveryCheckpoint, cfg: dict, *, resolver: EvidenceResolver) -> bool:
        if node.id == "leaf":
            resolver.read(tmp_path, path)
            if failure == "budget":
                resolver._charge("reads", resolver.limits.reads)
        else:
            write_repair_state(tmp_path, path, {"accepted": False})
        return failure != "changed_blocker" or node.id == "leaf"

    with (
        patch("core.delivery_checkpoints._checkpoint_local_pass_is_usable", side_effect=check),
        patch("core.delivery_checkpoints.verification_node_status", return_value=status),
        patch("core.delivery_checkpoints.verification_scope_complete", return_value=True),
    ):
        issue = checkpoint_barrier_issue(status, {})
        assert issue.code == (
            EVIDENCE_BUDGET_STOP if failure == "budget" else "delivery_checkpoint.evidence_unavailable"
        )


@pytest.mark.parametrize("code", ["security_rejected", "readonly_mutation"])
def test_terminal_boundary_preempts_even_an_earlier_ordinary_blocker(code: str) -> None:
    status = _status([DeliveryCheckpoint("first", (), ()), DeliveryCheckpoint("last", (), ())])
    status.checkpoint_verifications["first"].passed = False
    boundary = status.checkpoint_verifications["last"]
    boundary.status = "failed"
    boundary.security_status = "rejected" if code == "security_rejected" else "approved"
    boundary.stop_code = "delivery_verification." + code
    with patch("core.delivery_checkpoints._checkpoint_pass_is_usable", side_effect=AssertionError("unneeded")):
        assert checkpoint_barrier_issue(status, {}).code == boundary.stop_code
