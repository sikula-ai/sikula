from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

from core.delivery_checkpoint_evidence import checkpoint_evidence_path, store_checkpoint_evidence
from core.delivery_composition import CompositionRefresh, build_node_composition_context
from core.delivery_composition_scope import DeliveryCompositionAttempt
from core.delivery_reverification import new_reverification, parse_reverification, run_node_composition_cycle
from core.delivery_verification import DeliveryVerificationSnapshot
from core.delivery_verification_review import DeliveryIntegrationAssessment, DeliveryObligationAssessment
from tests.test_delivery_checkpoint_evidence import evidence_input as evidence_input
from tests.test_delivery_composition_scope import node_input as node_input
from tests.test_delivery_checkpoints import _git


def _changed_node(inputs, change, *, crlf=False):
    root, directory, source, snapshot, parent, historical = inputs
    _git(root, "init")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "core.autocrlf", "true" if crlf else "false")
    (root / ".gitignore").write_bytes(b".sikula/\n")
    (root / "code.py").write_bytes(b"value = 1\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "initial")
    origin = replace(
        historical["storage"],
        gate_id="sha256:" + "7" * 64,
        candidate_commit=_git(root, "rev-parse", "HEAD"),
        candidate_tree=_git(root, "rev-parse", "HEAD^{tree}"),
    )
    child = snapshot.composition_scope.child("storage")
    child_snapshot = DeliveryVerificationSnapshot(
        child,
        snapshot.completed_units[:1],
        replace(snapshot.identity, **{key: getattr(origin, key) for key in vars(snapshot.identity)}),
    )
    digest = store_checkpoint_evidence(
        root,
        directory,
        child_snapshot,
        origin,
        DeliveryIntegrationAssessment(
            "approved", "Checked.", obligation_results=[DeliveryObligationAssessment("consistent-read", "satisfied")]
        ),
    )
    historical = {"storage": replace(origin, checkpoint_evidence_fingerprint=digest)}
    if change == "paths":
        for index in range(33):
            (root / f"part-{index}.py").write_bytes(b"value = 2\n")
    else:
        newline = b"\r\n" if crlf else b"\n"
        (root / "code.py").write_bytes((b"value = 'changed'" + newline) * 5000)
        if change == "binary":
            (root / "z.bin").write_bytes(b"\0binary")
        elif change == "private":
            (root / "local.properties").write_bytes(b"PRIVATE_FIXTURE=hidden\n")
        elif change == "state":
            (root / "private-state").mkdir()
            (root / "private-state/state.txt").write_bytes(b"PRIVATE_FIXTURE=hidden\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "changed candidate")
    identity = replace(
        snapshot.identity,
        candidate_commit=_git(root, "rev-parse", "HEAD"),
        candidate_tree=_git(root, "rev-parse", "HEAD^{tree}"),
    )
    snapshot = replace(snapshot, identity=identity)
    parent = replace(parent, **vars(identity))
    return root, directory, source, snapshot, parent, historical


@pytest.mark.parametrize("change", ["bytes", "paths"])
@pytest.mark.parametrize("role", ["semantic", "security"])
@pytest.mark.parametrize("crlf", [False, True])
def test_text_overflow_requests_children_without_supplying_a_truncated_delta(node_input, change, role, crlf):
    root, _, _, snapshot, parent, historical = _changed_node(node_input, change, crlf=crlf)
    attempt = DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, parent)
    result = build_node_composition_context(root, snapshot, attempt, {}, review_kind=role)
    assert isinstance(result, CompositionRefresh)
    assert result.nodes == {
        "storage": {"origin": historical["storage"].checkpoint_evidence_fingerprint, "reason": f"delta_{change}"}
    }
    preview = result.preview_context
    assert len(json.dumps(preview).encode()) < 4096
    assert preview["checkpoint_composition"]["children"][0]["delta"] == ""
    assert "CHILD_CONTRACT" not in json.dumps(preview)
    assert "CHILD_OUTCOME" not in json.dumps(preview)


@pytest.mark.parametrize("change", ["binary", "private", "state", "git_unavailable", "no_authority", "missing"])
def test_overflow_does_not_authorize_refresh_for_unsupported_or_missing_inputs(node_input, change):
    root, directory, _, snapshot, parent, historical = _changed_node(node_input, change)
    cfg = {"tasks": {"state_dir": "private-state"}} if change == "state" else {}
    if change == "private":
        cfg = {"project": {"build_tool": "gradle-android"}}
    if change == "git_unavailable":
        historical["storage"] = replace(historical["storage"], candidate_commit="0" * 40)
        # Evidence corruption itself must stop, not authorize reconstruction.
    if change == "no_authority":
        snapshot = replace(
            snapshot, composition_scope=replace(snapshot.composition_scope, retained_authority_json=None)
        )
    attempt = DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, parent)
    if change == "missing":
        checkpoint_evidence_path(directory, historical["storage"]).unlink()
    if change in {"missing", "git_unavailable"}:
        with pytest.raises((OSError, ValueError)):
            build_node_composition_context(root, snapshot, attempt, cfg)
    else:
        assert build_node_composition_context(root, snapshot, attempt, cfg) is None


@pytest.mark.parametrize("role", ["semantic", "security"])
def test_metadata_growth_keeps_the_refresh_prompt_bounded(node_input, role):
    root, _, _, snapshot, parent, historical = _changed_node(node_input, "paths", crlf=True)
    lengths = []
    for count in (128, 512):
        for index in range(count):
            (root / f"extra-{index:04}.py").write_bytes(b"value = 2\r\n")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "more public changes")
        identity = replace(
            snapshot.identity,
            candidate_commit=_git(root, "rev-parse", "HEAD"),
            candidate_tree=_git(root, "rev-parse", "HEAD^{tree}"),
        )
        current = replace(snapshot, identity=identity)
        attempt = DeliveryCompositionAttempt.capture(
            current.composition_scope, historical, replace(parent, **vars(identity))
        )
        result = build_node_composition_context(root, current, attempt, {}, review_kind=role)
        assert isinstance(result, CompositionRefresh)
        assert result.nodes["storage"]["reason"] == "delta_paths"
        packet = json.dumps(result.preview_context)
        assert "extra-" not in packet
        lengths.append(len(packet))
    assert lengths[0] == lengths[1]


@pytest.mark.parametrize("tail", ["private", "binary", "gitlink", "renamed_private"])
def test_large_metadata_does_not_hide_unsupported_tail(node_input, tail):
    from unittest.mock import patch

    root, _, _, snapshot, parent, historical = _changed_node(node_input, "paths")
    for index in range(256):
        (root / f"extra-{index:04}.py").write_bytes(b"value = 2\n")
    cfg = {"tasks": {"state_dir": "z-private"}}
    (root / "z-private").mkdir()
    private = root / "z-private/evidence.txt"
    _git(root, "add", ".")
    _git(root, "commit", "-m", "large metadata prefix")
    if tail == "renamed_private":
        _git(root, "mv", "extra-0255.py", "z-private/moved.py")
    elif tail == "private":
        private.write_bytes(b"PRIVATE_TEST_FIXTURE=hidden\n")
    elif tail == "binary":
        (root / "z-binary.bin").write_bytes(b"\0opaque")
    else:
        _git(root, "config", "diff.ignoreSubmodules", "all")
        _git(
            root, "update-index", "--add", "--cacheinfo", "160000," + _git(root, "rev-parse", "HEAD") + ",z-dependency"
        )
    if tail != "gitlink":
        _git(root, "add", ".")
    _git(root, "commit", "-m", "unsupported tail")
    identity = replace(
        snapshot.identity,
        candidate_commit=_git(root, "rev-parse", "HEAD"),
        candidate_tree=_git(root, "rev-parse", "HEAD^{tree}"),
    )
    current = replace(snapshot, identity=identity)
    attempt = DeliveryCompositionAttempt.capture(
        current.composition_scope, historical, replace(parent, **vars(identity))
    )
    with patch("core.delivery_composition._git_bounded", side_effect=AssertionError("Do not read the patch")):
        assert build_node_composition_context(root, current, attempt, cfg) is None


def _refresh_control(parent):
    control = new_reverification()
    control["refreshes"] = {
        "semantic": {
            "nodes": {"storage": {"origin": "sha256:" + "a" * 64, "reason": "delta_bytes"}},
            "review_reserved": False,
        }
    }
    return replace(parent, composition_attempted=True, reverification=control)


@pytest.mark.parametrize("damage", [None, "reason", "origin", "reservation", "role", "accepted"])
def test_refresh_control_is_typed_and_bound_to_reserved_role(node_input, damage):
    parent = _refresh_control(node_input[4])
    control = deepcopy(parent.reverification)
    request = control["refreshes"]["semantic"]
    if damage == "reason":
        request["nodes"]["storage"]["reason"] = "missing_evidence"
    elif damage == "origin":
        request["nodes"]["storage"]["origin"] = "unbound"
    elif damage == "reservation":
        parent = replace(parent, composition_attempted=False)
    elif damage == "role":
        control["refreshes"]["security"] = control["refreshes"].pop("semantic")
    elif damage == "accepted":
        parent = replace(parent, composition_evidence_fingerprint="sha256:" + "b" * 64)
    if damage:
        with pytest.raises(ValueError):
            parse_reverification(control, parent.to_dict())
    else:
        assert parse_reverification(control, parent.to_dict()) == control


@pytest.mark.parametrize("passes", [True, False])
def test_refresh_reviews_prerequisites_before_first_parent_exchange(node_input, passes):
    parent = _refresh_control(node_input[4])
    calls = []
    accepted = object()

    def review(record, node):
        calls.append(("child", node))
        return record, passes

    def exchange(record, retry):
        calls.append(("parent", retry))
        return accepted, record

    result, _ = run_node_composition_cycle(
        node_input[3].composition_scope,
        parent,
        "semantic",
        refresh=lambda record: (record, ["storage"]),
        exchange=exchange,
        validate=lambda record: None,
        load=Mock(side_effect=AssertionError("No invented initial reviewer approval")),
        review_child=review,
        semantic_fallback_covers=lambda record: False,
    )
    assert calls == [("child", "storage")] + ([("parent", False)] if passes else [])
    assert result is (accepted if passes else None)


def test_refresh_validates_entire_selection_before_child_work(node_input):
    parent = _refresh_control(node_input[4])
    child = Mock()
    exchange = Mock()
    with pytest.raises(ValueError, match="immediate children"):
        run_node_composition_cycle(
            node_input[3].composition_scope,
            parent,
            "semantic",
            refresh=lambda record: (record, ["storage", "foreign"]),
            exchange=exchange,
            validate=lambda record: None,
            load=Mock(),
            review_child=child,
            semantic_fallback_covers=lambda record: False,
        )
    child.assert_not_called()
    exchange.assert_not_called()


def test_security_refresh_cannot_retain_semantic_approval_after_child_gap(node_input):
    from core.delivery_reverification import SemanticRecheckRequired

    parent = _refresh_control(node_input[4])
    rejected = deepcopy(parent.reverification)
    child = node_input[5]["storage"]
    rejected["children"]["storage"] = {
        "origin": child.checkpoint_evidence_fingerprint,
        "verification": child.to_dict(),
        "semantic_gap": True,
    }
    rejected_parent = replace(parent, reverification=rejected)
    exchange = Mock()
    with pytest.raises(SemanticRecheckRequired) as error:
        run_node_composition_cycle(
            node_input[3].composition_scope,
            parent,
            "security",
            refresh=lambda record: (record, ["storage"]),
            exchange=exchange,
            validate=lambda record: None,
            load=Mock(),
            review_child=lambda record, node: (rejected_parent, False),
            semantic_fallback_covers=lambda record: False,
        )
    assert error.value.record == rejected_parent
    exchange.assert_not_called()


def test_refresh_declines_unusable_parent_packet_before_reserving_work(node_input, monkeypatch):
    from core import delivery_composition, delivery_verify

    root, directory, _, snapshot, parent, _ = node_input
    request = CompositionRefresh({"storage": {"origin": "sha256:" + "a" * 64, "reason": "delta_bytes"}}, {})
    monkeypatch.setattr(delivery_composition, "build_composition_context", lambda *args, **kwargs: request)
    persist = Mock(side_effect=AssertionError("No durable work before packet preflight"))
    monkeypatch.setattr(delivery_verify, "_persist_composition_control", persist)
    result = delivery_verify._reserve_composition_refresh(
        SimpleNamespace(project_root=str(root)),
        snapshot,
        parent,
        {},
        "semantic",
        directory / "audit.jsonl",
        packet_fits=lambda context: False,
    )
    assert result == (parent, [])
    persist.assert_not_called()


def test_interrupted_post_refresh_exchange_is_not_repeated(node_input, monkeypatch):
    from core import delivery_composition, delivery_verify
    from core.delivery_verification_validation import DeliveryVerificationValidationResult

    root, directory, source, snapshot, parent, historical = node_input
    context = build_node_composition_context(
        root, snapshot, DeliveryCompositionAttempt.capture(snapshot.composition_scope, historical, parent), {}
    )
    parent = _refresh_control(parent)
    monkeypatch.setattr(delivery_composition, "build_composition_context", lambda *args, **kwargs: context)
    monkeypatch.setattr(delivery_verify, "_composition_packet_fits", lambda *args: True)
    monkeypatch.setattr(delivery_verify, "_safe_append_audit", lambda *args, **kwargs: True)
    monkeypatch.setattr(delivery_verify, "_review_snapshot", lambda *args: None)
    captured = []

    def persist(status, snapshot, record, cfg, **kwargs):
        record = replace(record, reverification=kwargs["reverification"])
        captured.append(record)
        return record

    monkeypatch.setattr(delivery_verify, "_persist_composition_control", persist)
    provider = Mock(side_effect=KeyboardInterrupt)
    monkeypatch.setattr(delivery_verify, "_run_review", provider)
    kwargs = dict(
        status=SimpleNamespace(project_root=str(root)),
        snapshot=snapshot,
        running=parent,
        reviewer=Mock(),
        worktree=root,
        root=root,
        source_task=source,
        validation=DeliveryVerificationValidationResult(True, False, True),
        evidence_path=directory / "audit.jsonl",
        project_config={},
    )
    with pytest.raises(KeyboardInterrupt):
        delivery_verify._composition_exchange(**kwargs)
    assert captured[-1].reverification["refreshes"]["semantic"]["review_reserved"]
    assert delivery_verify._composition_exchange(**{**kwargs, "running": captured[-1]}) == (None, captured[-1])
    assert provider.call_count == 1


def test_checkpoint_boundary_preempts_semantic_repair_evidence(node_input, monkeypatch):
    from core import delivery_verify

    record = replace(
        node_input[4],
        status="blocked",
        security_status="rejected",
        stop_code="delivery_verification.repair_required",
        composition_attempted=True,
        reverification={"reference": "sha256:" + "0" * 64},
    )
    status = SimpleNamespace(
        valid=True,
        verification_node="integration",
        verification=record,
        plan=SimpleNamespace(checkpoints=[object()]),
    )
    readiness = SimpleNamespace(ready=True, required=True)
    monkeypatch.setattr(delivery_verify, "verification_scope_complete", lambda status: True)
    blocked = Mock(return_value=object())
    monkeypatch.setattr(delivery_verify, "_blocked_result", blocked)
    assert delivery_verify._preflight_result(status, readiness, {}) is blocked.return_value
    blocked.assert_called_once_with(status, "delivery_verification.security_rejected")
