from dataclasses import replace
import os
from unittest.mock import Mock

import pytest

from core import delivery_checkpoint_evidence as evidence
from core import delivery_evidence_resolution as resolution
from core.delivery_evidence_resolution import EvidenceResolver, EvidenceResolutionLimit, EvidenceResolutionLimits
from core.delivery_repair_storage import read_repair_state, write_repair_state
from tests.test_delivery_checkpoint_evidence import evidence_input as evidence_input, _store


def test_reads_and_parses_once_per_operation_without_sharing_mutable_values(tmp_path):
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"outcomes": ["accepted"]})
    reader = Mock(wraps=read_repair_state)
    with EvidenceResolver() as resolver:
        first = resolver.read(tmp_path, path, reader=reader)
        first["outcomes"].append("changed")
        assert resolver.read(tmp_path, path, reader=reader) == {"outcomes": ["accepted"]}
        assert reader.call_count == 1
    assert resolver.bytes_read == 2 * path.stat().st_size  # initial read plus final byte-integrity check
    assert resolver.cache_bytes == 0
    with pytest.raises(EvidenceResolutionLimit):
        resolver.read(tmp_path, path)
    with EvidenceResolver() as resumed:
        resumed.read(tmp_path, path, reader=reader)
    assert reader.call_count == 2


@pytest.mark.parametrize("change", ["edit", "delete", "replace", "symlink", "hardlink"])
def test_cached_reads_recheck_filesystem_identity(tmp_path, change):
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"value": "original"})
    with pytest.raises((OSError, ValueError)):
        with EvidenceResolver() as resolver:
            resolver.read(tmp_path, path)
            if change == "edit":
                path.write_bytes(b'{"value":"modified"}')
            elif change == "replace":
                write_repair_state(tmp_path, path, {"value": "replaced"})
            elif change == "hardlink":
                os.link(path, tmp_path / "alias.json")
            else:
                path.unlink()
                if change == "symlink":
                    target = tmp_path / "target.json"
                    write_repair_state(tmp_path, target, {"value": "original"})
                    try:
                        path.symlink_to(target)
                    except (OSError, NotImplementedError):
                        pytest.skip("Symlinks unavailable")
            resolver.read(tmp_path, path)


def test_final_integrity_check_rejects_edits_even_with_unchanged_stat_identity(tmp_path, monkeypatch):
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"value": "before"})
    identity = resolution.repair_state_identity(tmp_path, path)
    # Models restored timestamps, including Windows ctime semantics.
    monkeypatch.setattr(resolution, "repair_state_identity", lambda *args: identity)
    with pytest.raises(ValueError, match="changed"):
        with EvidenceResolver() as resolver:
            resolver.read(tmp_path, path)
            path.write_bytes(path.read_bytes().replace(b"before", b"after!"))
            assert resolver.read(tmp_path, path)["value"] == "before"


def test_new_operation_after_publication_uses_new_evidence(tmp_path):
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"version": 1})
    with EvidenceResolver() as resolver:
        assert resolver.read(tmp_path, path)["version"] == 1
    write_repair_state(tmp_path, path, {"version": 2})
    with EvidenceResolver() as resolver:
        assert resolver.read(tmp_path, path)["version"] == 2


@pytest.mark.parametrize("field,limit", [("artifacts", 1), ("reads", 1), ("bytes_read", 30), ("materialized_bytes", 1)])
def test_aggregate_resource_exhaustion_is_sticky(tmp_path, field, limit):
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    for path in (first, second):
        write_repair_state(tmp_path, path, {"value": "some content"})
    resolver = EvidenceResolver(replace(EvidenceResolutionLimits(), **{field: limit}))
    with pytest.raises(EvidenceResolutionLimit):
        with resolver:
            resolver.read(tmp_path, first)
            resolver.read(tmp_path, second)
    with pytest.raises(EvidenceResolutionLimit):
        resolver.read(tmp_path, first)


def test_lru_eviction_retains_change_detection_and_charges_rereads(tmp_path):
    paths = [tmp_path / f"{index}.json" for index in range(3)]
    payload = {"value": "x" * 1000}
    for path in paths:
        write_repair_state(tmp_path, path, payload)
    size = resolution._weight(payload, 10000)
    reader = Mock(wraps=read_repair_state)
    with EvidenceResolver(replace(EvidenceResolutionLimits(), cache_bytes=size)) as resolver:
        for path in [*paths, paths[0]]:
            assert resolver.read(tmp_path, path, reader=reader) == payload
            assert resolver.cache_bytes <= size
        assert reader.call_count == 4
    assert resolver.bytes_read == 7 * paths[0].stat().st_size


def test_cycle_depth_and_expansion_budgets():
    with EvidenceResolver() as resolver:
        with resolver.visit("node"):
            with pytest.raises(ValueError, match="Cyclic"):
                with resolver.visit("node"):
                    pytest.fail("cycle entered")
    with pytest.raises(EvidenceResolutionLimit):
        with EvidenceResolver(replace(EvidenceResolutionLimits(), depth=1)) as resolver:
            with resolver.visit("parent"):
                with resolver.visit("child"):
                    pytest.fail("depth exceeded")
    with pytest.raises(EvidenceResolutionLimit):
        with EvidenceResolver(replace(EvidenceResolutionLimits(), expanded_items=2)) as resolver:
            resolver.expand(2)
            resolver.expand(1)


@pytest.mark.parametrize("change", ["plan", "node", "attempt", "coverage", "digest"])
def test_shared_payload_never_skips_caller_authority_validation(evidence_input, monkeypatch, change):
    root, directory, snapshot, _, _ = evidence_input
    record = _store(evidence_input)
    reader = Mock(wraps=read_repair_state)
    monkeypatch.setattr(evidence, "read_repair_state", reader)
    with EvidenceResolver() as resolver:
        loaded = evidence.load_checkpoint_evidence(
            root, directory, record, plan_id="cache", node_id="storage", resolver=resolver
        )
        assert loaded.covers(snapshot.scope)
        changed = record
        kwargs = dict(plan_id="cache", node_id="storage", resolver=resolver)
        if change in {"plan", "node"}:
            kwargs[change + "_id"] = "other"
        elif change == "attempt":
            changed = replace(record, attempt=2)
        elif change == "coverage":
            changed = replace(record, obligation_count=2)
        else:
            changed = replace(record, checkpoint_evidence_fingerprint="sha256:" + "0" * 64)
        with pytest.raises(ValueError):
            evidence.load_checkpoint_evidence(root, directory, changed, **kwargs)
        assert reader.call_count == 1


def test_separate_calls_do_not_reuse_evidence_after_mutation(evidence_input):
    root, directory, _, _, _ = evidence_input
    record = _store(evidence_input)
    evidence.load_checkpoint_evidence(root, directory, record, plan_id="cache", node_id="storage")
    path = evidence.checkpoint_evidence_path(directory, record)
    path.write_bytes(b"{}")
    with pytest.raises(ValueError):
        evidence.load_checkpoint_evidence(root, directory, record, plan_id="cache", node_id="storage")


def test_nested_roles_share_raw_artifacts_but_resume_resolves_again(tmp_path, monkeypatch):
    from collections import Counter
    from core import delivery_candidate_control, delivery_composition, delivery_repair_storage
    from core.delivery_checkpoint_applicability import validate_root_evidence
    from core.delivery_checkpoints import with_checkpoint_barriers, checkpoint_barrier_issue
    from core.delivery_finalize import _final_verification_issue
    from core.delivery_progress import get_delivery_status
    from core.delivery_verification_model import delivery_verification_recovery_action
    from tests import test_delivery_recursive_reverification as recursive
    from tests.test_delivery_authority_flow import _run

    path, cfg = recursive.candidate.__wrapped__(tmp_path)
    semantic, security = recursive._reviewers(recursive._outputs(path, cfg))
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    status = get_delivery_status(path)
    reads = Counter()

    def counted(root, path, **kwargs):
        reads[path] += 1
        return read_repair_state(root, path, **kwargs)

    for module in (evidence, delivery_candidate_control, delivery_composition, delivery_repair_storage):
        monkeypatch.setattr(module, "read_repair_state", counted)
    with EvidenceResolver() as resolver:
        validate_root_evidence(status, status.verification, resolver=resolver)
        assert len(reads) > 4  # child results, both compositions and candidate control sidecars
        assert all(count == 1 for count in reads.values())
        assert resolver.reads > sum(reads.values())
    assert resolver.bytes_read == 2 * sum(path.stat().st_size for path in reads)
    originals = reads.copy()
    validate_root_evidence(status, status.verification)
    assert reads == Counter({path: 2 * count for path, count in originals.items()})

    limits = replace(EvidenceResolutionLimits(), artifacts=1)
    monkeypatch.setattr(resolution, "EvidenceResolutionLimits", lambda: limits)
    projected = with_checkpoint_barriers(status, cfg)
    assert not projected.valid
    assert projected.errors[0].code == resolution.EVIDENCE_BUDGET_STOP
    assert checkpoint_barrier_issue(status, cfg).code == resolution.EVIDENCE_BUDGET_STOP
    assert _final_verification_issue(tmp_path, status, cfg).code == resolution.EVIDENCE_BUDGET_STOP
    assert delivery_verification_recovery_action(resolution.EVIDENCE_BUDGET_STOP) == "resolve_delivery_evidence_budget"
    semantic, security = recursive._LLM(), recursive._LLM()
    blocked, _ = _run(path, cfg, semantic, security, node_id="root")
    assert not blocked.succeeded
    assert resolution.EVIDENCE_BUDGET_STOP in {blocked.stop_code, *(issue.code for issue in blocked.errors)}
    assert not semantic.calls and not security.calls
    for code, security_status in (
        ("delivery_verification.readonly_mutation", "approved"),
        ("delivery_verification.security_rejected", "rejected"),
    ):
        boundary = replace(
            status,
            verification=replace(
                status.verification, status="blocked", stop_code=code, security_status=security_status
            ),
        )
        assert with_checkpoint_barriers(boundary, cfg).verification.stop_code == code
        assert checkpoint_barrier_issue(boundary, cfg).code == code
        assert _final_verification_issue(tmp_path, boundary, cfg).code == code


def test_retained_json_memory_does_not_grow_with_artifact_inventory(tmp_path):
    import gc
    import json.decoder
    import tracemalloc

    paths = [tmp_path / f"artifact-{index}.json" for index in range(200)]
    for path in paths:
        write_repair_state(tmp_path, path, {"result": "x" * 32_000})
    cache_limit = 64 * 1024
    started = not tracemalloc.is_tracing()
    if started:
        tracemalloc.start()
    try:
        filters = (tracemalloc.Filter(True, json.decoder.__file__),)
        before = tracemalloc.take_snapshot().filter_traces(filters)
        with EvidenceResolver(replace(EvidenceResolutionLimits(), cache_bytes=cache_limit)) as resolver:
            for path in paths:
                resolver.read(tmp_path, path)
            gc.collect()
            retained = tracemalloc.take_snapshot().filter_traces(filters)
            allocated = sum(max(0, item.size_diff) for item in retained.compare_to(before, "filename"))
            # Real live decoder allocations, excluding coverage/tracer allocations.
            # An unbounded memo would retain more than 6 MiB for these artifacts.
            assert 32_000 <= allocated < cache_limit * 2
        gc.collect()
        released = tracemalloc.take_snapshot().filter_traces(filters)
        # Coverage can retain its own JSON metadata after the baseline. Measure
        # release relative to the populated cache, not total process allocations.
        assert sum(item.size_diff for item in released.compare_to(retained, "filename")) <= -32_000
    finally:
        if started:
            tracemalloc.stop()


@pytest.mark.parametrize("security", [False, True])
@pytest.mark.parametrize("boundary_node", ["root", "storage"])
@pytest.mark.parametrize("consumer_state", ["pending", "running", "failed"])
def test_known_boundary_preempts_readiness_evidence_resolution(
    evidence_input, monkeypatch, security, boundary_node, consumer_state
):
    from types import SimpleNamespace
    from core.delivery_checkpoint_model import DeliveryCheckpoint
    from core.delivery_plan import DeliveryPlanUnit
    from core.delivery_progress import DeliveryStatusResult, DeliveryStatusUnit, select_next_delivery_unit
    from core.delivery_verification import with_delivery_verification_readiness

    record = replace(
        evidence_input[3],
        status="blocked",
        stop_code="delivery_verification.repair_required" if security else "delivery_verification.readonly_mutation",
        security_status="rejected" if security else "not_run",
        reverification={"reference": "sha256:" + "a" * 64},
    )
    plan_units = [
        DeliveryPlanUnit(key, None, key + ".md", depends_on=dependencies)
        for key, dependencies in (("foundation", []), ("bridge", ["foundation"]), ("consumer", ["bridge"]))
    ]
    units = [
        DeliveryStatusUnit(
            id=unit.id,
            status=consumer_state if unit.id == "consumer" else "done",
            title=None,
            task_path=unit.task_path,
            depends_on=unit.depends_on,
            child_task_id="child-" + unit.id,
        )
        for unit in plan_units
    ]
    status = DeliveryStatusResult(
        plan_path="plan.yaml",
        project_root=str(evidence_input[0]),
        progress_path=None,
        progress_exists=True,
        status="blocked",
        errors=[],
        warnings=[],
        verification=record if boundary_node == "root" else None,
        checkpoint_verifications={"storage": record} if boundary_node == "storage" else {},
        units=units,
        plan=SimpleNamespace(
            checkpoints=[DeliveryCheckpoint("storage", ("foundation",), ("outcome",))], units=plan_units
        ),
    )

    def no_reads(*args, **kwargs):
        pytest.fail("Known boundary must not depend on evidence resolution")

    monkeypatch.setattr(EvidenceResolver, "read", no_reads)
    result = with_delivery_verification_readiness(status, {})
    assert (result.verification if boundary_node == "root" else result.checkpoint_verifications["storage"]) == record
    assert not result.errors
    assert result.checkpoint_candidate_evidence == {"storage": "unavailable"}
    foundation, bridge, consumer = result.units
    assert foundation.blocked_by_checkpoints == []
    assert bridge.blocked_by_checkpoints == ["storage"]
    assert consumer.to_dict()["blocked_by_checkpoints"] == ["storage"]
    assert consumer.to_dict()["eligible"] is False
    assert not consumer.run_next_available
    assert consumer.run_next_action is None
    assert select_next_delivery_unit(result) is None
    assert select_next_delivery_unit(result, reset_failed=True) is None


def test_artifact_larger_than_cache_is_validated_without_retaining_it(tmp_path):
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"value": "uncached"})
    reader = Mock(wraps=read_repair_state)
    with EvidenceResolver(replace(EvidenceResolutionLimits(), cache_bytes=1)) as resolver:
        for _ in range(2):
            assert resolver.read(tmp_path, path, reader=reader) == {"value": "uncached"}
        assert reader.call_count == 2
        assert resolver.cache_bytes == 0
    assert resolver.bytes_read == 3 * path.stat().st_size


def test_caught_content_change_remains_evidence_failure_not_resource_exhaustion(tmp_path):
    path = tmp_path / "evidence.json"
    write_repair_state(tmp_path, path, {"value": "original"})
    with pytest.raises(ValueError) as error:
        with EvidenceResolver() as resolver:
            resolver.read(tmp_path, path)
            write_repair_state(tmp_path, path, {"value": "changed"})
            with pytest.raises(ValueError):
                resolver.read(tmp_path, path)
    assert not isinstance(error.value, EvidenceResolutionLimit)
    assert resolution.evidence_failure_code(error.value, "evidence_changed") == "evidence_changed"


def test_repair_input_preserves_resource_stop_before_authoring(monkeypatch):
    from types import SimpleNamespace
    from core import delivery_checkpoints, delivery_repair
    from core.delivery_plan import DeliveryPlanIssue

    status = SimpleNamespace(valid=True, plan=object(), project_root=".", verification_node="storage")
    monkeypatch.setattr(delivery_repair, "verification_scope_complete", lambda status: True)
    issue = DeliveryPlanIssue("error", resolution.EVIDENCE_BUDGET_STOP, "Evidence resource limit.")
    monkeypatch.setattr(delivery_checkpoints, "checkpoint_barrier_issue", lambda *args: issue)
    with pytest.raises(delivery_repair.DeliveryRepairError) as error:
        delivery_repair._current_input(status, {})
    assert error.value.issue == issue
