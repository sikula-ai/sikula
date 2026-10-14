from __future__ import annotations

from dataclasses import replace
import json

import pytest

from core.delivery_candidate_control import (
    CandidateBudgetExceeded,
    compact_record,
    expand_record,
    record_at,
    replace_at,
    reserved_node_count,
)
from core.delivery_reverification import new_reverification
from core.delivery_verification_model import DeliveryVerificationRecord, delivery_verification_is_boundary_stop


def _record(key):
    return DeliveryVerificationRecord(
        1,
        "sha256:" + key * 64,
        "b" * 40,
        "c" * 40,
        *["sha256:" + "d" * 64] * 5,
        status="running",
        attempt=1,
        composition_attempted=True,
        reverification=new_reverification(),
    )


def _entry(record):
    return {"origin": "sha256:" + "e" * 64, "verification": record.to_dict()}


def test_node_references_keep_ancestor_records_bounded(tmp_path):
    directory = tmp_path / "state"
    leaf = _record("1")
    parent = _record("2")
    parent.reverification["children"]["leaf"] = _entry(compact_record(tmp_path, directory, leaf))
    ancestor = _record("3")
    ancestor.reverification["children"]["parent"] = _entry(compact_record(tmp_path, directory, parent))
    before = json.dumps(ancestor.to_dict())
    leaf.reverification["fallbacks"]["semantic"] = "sha256:" + "a" * 64
    changed = replace_at(tmp_path, directory, ancestor, ("parent", "leaf"), leaf)
    assert len(json.dumps(changed.to_dict())) == len(before)
    assert "fallbacks" not in json.dumps(changed.reverification["children"])
    assert record_at(tmp_path, directory, changed, ("parent", "leaf")) == leaf
    assert reserved_node_count(tmp_path, directory, changed) == 3
    compact = compact_record(tmp_path, directory, changed)
    assert expand_record(tmp_path, directory, compact) == changed


def test_referenced_control_accepts_windows_newlines(tmp_path):
    record = _record("1")
    compact = compact_record(tmp_path, tmp_path, record)
    digest = compact.reverification["reference"]
    path = tmp_path / f"candidate-node-{digest[7:]}.json"
    payload = json.loads(path.read_bytes())
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8", newline="\r\n")
    assert b"\r\n" in path.read_bytes()
    assert expand_record(tmp_path, tmp_path, compact) == record


@pytest.mark.parametrize("damage", ["missing", "hash", "owner"])
def test_referenced_control_must_be_intact_and_owned(tmp_path, damage):
    from core.delivery_composition import fingerprint

    record = _record("1")
    compact = compact_record(tmp_path, tmp_path, record)
    digest = compact.reverification["reference"]
    path = tmp_path / f"candidate-node-{digest[7:]}.json"
    if damage == "missing":
        path.unlink()
    elif damage == "hash":
        path.write_bytes(b"{}")
    else:
        payload = json.loads(path.read_bytes())
        payload["gate_id"] = _record("2").gate_id
        digest = fingerprint(payload)
        (tmp_path / f"candidate-node-{digest[7:]}.json").write_text(
            json.dumps(payload, indent=2), encoding="utf-8", newline="\r\n"
        )
        compact = replace(compact, reverification={"reference": digest})
    with pytest.raises(ValueError, match="unavailable"):
        expand_record(tmp_path, tmp_path, compact)


def test_budget_counts_reserved_work_and_rejects_repeated_identity(tmp_path, monkeypatch):
    import core.delivery_candidate_control as module

    root = _record("1")
    child = _record("2")
    root.reverification["children"]["child"] = _entry(child)
    monkeypatch.setattr(module, "MAX_CANDIDATE_NODES", 1)
    with pytest.raises(CandidateBudgetExceeded):
        reserved_node_count(tmp_path, tmp_path, root)
    monkeypatch.setattr(module, "MAX_CANDIDATE_NODES", 32)
    root.reverification["children"]["another"] = _entry(child)
    with pytest.raises(ValueError, match="Repeated"):
        reserved_node_count(tmp_path, tmp_path, root)


def test_boundary_classification_survives_missing_nested_control(tmp_path):
    root, middle, leaf = (_record(key) for key in ("1", "2", "3"))
    middle.reverification["children"]["leaf"] = _entry(compact_record(tmp_path, tmp_path, leaf))
    root.reverification["children"]["middle"] = _entry(compact_record(tmp_path, tmp_path, middle))
    terminal = replace(leaf, status="blocked", stop_code="delivery_verification.readonly_mutation")
    updated = replace_at(tmp_path, tmp_path, root, ("middle", "leaf"), terminal)
    for path in tmp_path.glob("candidate-node-*.json"):
        path.unlink()
    assert updated.stop_code == terminal.stop_code
    assert delivery_verification_is_boundary_stop(updated)


def test_descendant_gap_remains_visible_after_local_approval(tmp_path):
    from core.delivery_reverification import semantic_gap_ids

    root, middle, leaf = (_record(key) for key in ("1", "2", "3"))
    middle.reverification["children"]["leaf"] = _entry(compact_record(tmp_path, tmp_path, leaf))
    root.reverification["children"]["middle"] = _entry(compact_record(tmp_path, tmp_path, middle))
    rejected = replace(
        leaf, status="failed", semantic_status="rejected", stop_code="delivery_verification.repair_required"
    )
    updated = replace_at(tmp_path, tmp_path, root, ("middle", "leaf"), rejected)
    assert semantic_gap_ids(updated) == ["middle"]
    intermediate = record_at(tmp_path, tmp_path, updated, ("middle",))
    assert semantic_gap_ids(intermediate) == ["leaf"]
    approved = replace(intermediate, status="passed", semantic_status="approved")
    updated = replace_at(tmp_path, tmp_path, updated, ("middle",), approved)
    assert semantic_gap_ids(updated) == ["middle"]


@pytest.mark.parametrize("anchor_id", ["root", "integration"])
@pytest.mark.parametrize("change", ["attempt", "identity", "completed"])
def test_descendant_write_requires_running_initiating_attempt(tmp_path, anchor_id, change):
    from types import SimpleNamespace
    from core.delivery_progress import DeliveryProgress, delivery_progress_path, write_delivery_progress
    from core.delivery_verify import _update_candidate_record

    path = delivery_progress_path(tmp_path, "example")
    anchor, middle, leaf = (_record(key) for key in ("1", "2", "3"))
    middle.reverification["children"]["leaf"] = _entry(compact_record(tmp_path, path.parent, leaf))
    anchor.reverification["children"]["middle"] = _entry(compact_record(tmp_path, path.parent, middle))
    snapshot = SimpleNamespace(
        candidate_route=("middle", "leaf"),
        candidate_anchor=anchor_id,
        candidate_anchor_gate_id=anchor.gate_id,
        candidate_anchor_attempt=anchor.attempt,
    )
    changes = {
        "attempt": {"attempt": 2},
        "identity": {"gate_id": _record("4").gate_id},
        "completed": {"status": "interrupted"},
    }
    anchor = replace(anchor, **changes[change])
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="example",
        verification=anchor if anchor_id == "root" else None,
        checkpoint_verifications={anchor_id: anchor} if anchor_id != "root" else {},
        checkpoint_ids=(anchor_id,) if anchor_id != "root" else (),
    )
    write_delivery_progress(path, progress)
    before = path.read_bytes()
    status = SimpleNamespace(project_root=str(tmp_path), plan=SimpleNamespace(plan_id="example"))
    with pytest.raises(ValueError, match="Initiating candidate review attempt changed"):
        _update_candidate_record(status, snapshot, leaf, {}, {"reverification": new_reverification()})
    assert path.read_bytes() == before


@pytest.mark.parametrize("anchor_id", ["root", "integration"])
@pytest.mark.parametrize("violation", ["readonly", "security"])
def test_candidate_boundary_latches_without_descendant_control(tmp_path, monkeypatch, anchor_id, violation):
    from types import SimpleNamespace
    from core.delivery_progress import (
        DeliveryProgress,
        delivery_progress_path,
        read_delivery_progress,
        write_delivery_progress,
    )
    from core.delivery_verify import _persist_candidate_boundary
    from core import delivery_candidate_control as control_module

    path = delivery_progress_path(tmp_path, "example")
    anchor, child = (replace(_record(key), security_required=True) for key in ("1", "2"))
    anchor.reverification["children"]["child"] = _entry(compact_record(tmp_path, path.parent, child))
    snapshot = SimpleNamespace(
        candidate_route=("child",),
        candidate_anchor=anchor_id,
        candidate_anchor_gate_id=anchor.gate_id,
        candidate_anchor_attempt=anchor.attempt,
    )
    anchor = replace(anchor, attempt=2, status="interrupted")
    write_delivery_progress(
        path,
        DeliveryProgress(
            schema_version=1,
            plan_id="example",
            verification=anchor if anchor_id == "root" else None,
            checkpoint_verifications={anchor_id: anchor} if anchor_id != "root" else {},
            checkpoint_ids=(anchor_id,) if anchor_id != "root" else (),
        ),
    )
    for sidecar in path.parent.glob("candidate-node-*.json"):
        sidecar.unlink()

    def unavailable(project_root, file):
        pytest.fail("Boundary persistence must not load descendant sidecars")

    monkeypatch.setattr(control_module, "read_repair_state", unavailable)
    status = SimpleNamespace(project_root=str(tmp_path), plan=SimpleNamespace(plan_id="example"))
    terminal = replace(
        child,
        status="blocked",
        stop_code="delivery_verification." + ("readonly_mutation" if violation == "readonly" else "repair_required"),
        security_status="rejected" if violation == "security" else "not_run",
    )
    _persist_candidate_boundary(status, snapshot, terminal)
    progress, errors = read_delivery_progress(path, plan_id="example")
    assert not errors
    saved = progress.verification if anchor_id == "root" else progress.checkpoint_verifications[anchor_id]
    assert saved == replace(
        anchor,
        status="blocked",
        stop_code=terminal.stop_code,
        security_status=terminal.security_status,
        completed_at=saved.completed_at,
    )
    assert saved.completed_at
    assert not list(path.parent.glob("candidate-node-*.json"))
    before = path.read_bytes()
    _persist_candidate_boundary(
        status, snapshot, replace(terminal, stop_code="delivery_verification.workspace_boundary_invalid")
    )
    assert path.read_bytes() == before
