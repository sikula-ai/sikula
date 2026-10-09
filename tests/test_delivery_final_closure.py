from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from core.delivery_checkpoint_applicability import validate_root_evidence
from core.delivery_checkpoint_evidence import root_evidence_path
from core.delivery_composition import build_composition_context, composition_example
from core.delivery_obligations import delivery_authority_fragments
from core.delivery_plan import check_delivery_plan_file
from core.delivery_progress import (
    DeliveryProgress,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_verification import build_delivery_verification_snapshot, check_delivery_verification_readiness
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification_review import delivery_integration_review_control_example
from tests.test_delivery_authority import _draft_data, _prepare
from tests.test_delivery_authority_flow import _run
from tests.test_delivery_checkpoints import _complete
from tests.test_delivery_repair import _LLM, _git


def _prepared(root: Path, count=4, *, independent=True, direct=False):
    source = "# Delivery\nPreserve the public API.\n\n"
    source += "".join(
        f"## Storage {i}\nCHILD_INTERNAL_{i}: " + "Maintain internal storage consistency. " * 40 + "\n\n"
        for i in range(count)
    )
    source += "## Integration\nConsumers must observe successful storage updates.\n"
    data = _draft_data(source)
    fragments = delivery_authority_fragments(source)
    node = data["checkpoints"][0]
    node["integration_context"] = (
        "Storage supplies reads and atomic writes to consumers. Verify visibility, failure isolation and privacy across the storage API."
    )
    for index, record in enumerate(data["source_accounting"]):
        internal = "CHILD_INTERNAL" in fragments[index].text
        record["checkpoint_ids"] = ["storage"] if internal or index == 0 else []
        record["final_gate"] = not internal
        if internal:
            key = f"storage-{index}"
            data["obligations"].append(
                {
                    "id": key,
                    "summary": f"Internal storage property {index} remains correct.",
                    "source_fragment_ids": [record["source_fragment_id"]],
                    "unit_ids": ["foundation"],
                    "disposition": "preserved",
                }
            )
            node["obligation_ids"].append(key)
            record.update(disposition="mapped", obligation_ids=[key])
    if direct:
        record = data["source_accounting"][-1]
        data["obligations"].append(
            {
                "id": "consumer-visible",
                "summary": "Consumers observe updates.",
                "source_fragment_ids": [record["source_fragment_id"]],
                "unit_ids": ["consumer"],
                "disposition": "preserved",
            }
        )
        record.update(disposition="mapped", obligation_ids=["consumer-visible"])
    return _prepare(root, source, data, verification={"final_gate_authority_complete": independent})


def _prompt(root, cfg, source, context, role):
    return DeliveryIntegrationReviewAgent(None, cfg)._prompt(
        cwd=root,
        review_kind=role,
        source_task=source,
        plan_context=context,
        validation_summary={},
        candidate_commit="a" * 40,
        candidate_tree="b" * 40,
        known_obligation_ids={item["id"] for item in context["obligations"]},
    )


def test_final_delegation_requires_separate_independent_approval(tmp_path):
    for approved in (False, True):
        path, cfg, checked, draft, llm, audit = _prepared(tmp_path / str(approved), independent=approved)
        assert bool(checked.plan.verified_final_gate_authority) == approved
        assert bool(checked.plan.final_gate_authority) == approved
        assert audit[-1]["parsed"]["final_gate_authority_complete"] == approved
        assert "final_gate_authority" not in checked.plan.to_dict()
        assert "integration_context" not in json.dumps(checked.plan.to_dict())
        assert len(llm.prompts) == 2
        assert check_delivery_verification_readiness(checked, cfg).ready


@pytest.mark.parametrize("change", ["contract", "receipt", "context", "ownership", "attribution"])
def test_changed_final_authority_selects_full_review(tmp_path, change):
    path, _, checked, _, _, _ = _prepared(tmp_path)
    data = yaml.safe_load(path.read_text())
    if change == "contract":
        file = tmp_path / checked.plan.units[-1].task_path
        file.write_text(file.read_text() + "\nChanged consumer contract.\n")
    elif change == "receipt":
        data["final_gate_authority"] = "sha256:" + "0" * 64
    elif change == "context":
        data["checkpoints"][0]["integration_context"] += " Changed interface."
    elif change == "ownership":
        data["constraints"] = [
            {
                "id": "cross",
                "kind": "security_boundary",
                "summary": "Preserve integration privacy.",
                "unit_ids": ["foundation", "consumer"],
                "disposition": "preserved",
            }
        ]
        data["source_accounting"][1]["constraint_ids"] = ["cross"]
    else:
        data["source_accounting"][1]["final_gate"] = True
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    result = check_delivery_plan_file(path, project_root=tmp_path)
    assert result.valid, result.errors
    assert result.plan.verified_final_gate_authority is None
    assert "authority_packet" not in DeliveryVerificationScope.from_plan(result.plan).plan_context()


def test_final_authority_growth_does_not_copy_child_details(tmp_path):
    sizes = []
    for count in (2, 24):
        root = tmp_path / str(count)
        path, cfg, checked, _, _, _ = _prepared(root, count)
        context = json.loads(checked.plan.verified_final_gate_authority)
        children = [
            {
                **child,
                "evidence_fingerprint": "sha256:" + "a" * 64,
                "exact_tree": True,
                "delta": "",
                "origin_commit": "b" * 40,
                "origin_tree": "c" * 40,
                "security_outcome": "approved",
            }
            for child in context["final_authority"]["children"]
        ]
        context["final_authority"] = {"policy": context["final_authority"]["policy"]}
        values = []
        for role in ("semantic", "security"):
            context["checkpoint_composition"] = {"compact": True, "review_kind": role, "children": children}
            prompt = _prompt(root, cfg, (root / "source.md").read_text(), context, role)
            assert "CHILD_INTERNAL" not in prompt
            assert "Consumers must observe" in prompt
            assert "storage-1" not in prompt
            values.append(len(prompt.encode()))
        sizes.append(values)
    assert all(abs(a - b) < 100 for a, b in zip(*sizes))


@pytest.fixture
def closure_plan(tmp_path, request):
    path, cfg, _, _, _, _ = _prepared(tmp_path, direct=getattr(request, "param", False))
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n")
    (tmp_path / "agents/code.py").write_text("value = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "prepared final authority")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "team-invites"),
        DeliveryProgress(
            schema_version=1,
            plan_id="team-invites",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress("foundation", "done", commit=commit)],
        ),
    )
    status = get_delivery_status(path)
    output = delivery_integration_review_control_example(set(status.plan.checkpoints[0].obligation_ids))
    result, _ = _run(path, cfg, _LLM(output))
    assert result.succeeded, result
    _complete(path, "consumer")
    return path, cfg


def _contexts(path, cfg):
    status = get_delivery_status(path)
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    return [build_composition_context(status, snapshot, cfg, review_kind=role) for role in ("semantic", "security")]


@pytest.mark.parametrize("closure_plan", [False, True], indirect=True)
def test_final_closure_runs_both_reviewers_persists_and_reuses(closure_plan):
    path, cfg = closure_plan
    contexts = _contexts(path, cfg)
    assert all(context["checkpoint_composition"]["compact"] for context in contexts)
    semantic, security = [_LLM(composition_example(context)) for context in contexts]
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    assert len(semantic.calls) == len(security.calls) == 1
    assert all("CHILD_INTERNAL" not in call for call in semantic.calls + security.calls)
    status = get_delivery_status(path)
    record = status.verification
    assert record.obligation_satisfied_count == len(status.plan.obligations)
    assert record.security_composition_evidence_fingerprint
    directory = delivery_progress_path(path.parents[3], status.plan.plan_id).parent
    payload = json.loads(root_evidence_path(directory, record.root_evidence_fingerprint).read_text())
    assert payload["obligation_results"] == [
        {"id": item["id"], "outcome": "satisfied"} for item in contexts[0]["obligations"]
    ]
    assert len(payload["completed_units"]) == 1
    assert len(payload["child_evidence"]) == 1
    validate_root_evidence(status, record)
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert again.succeeded and not unused.calls
    from core.delivery_finalize import finalize_delivery_plan

    finalized = finalize_delivery_plan(path, project_config=cfg)
    assert finalized.finalized, finalized


@pytest.mark.parametrize("point", ["security", "evidence"])
def test_final_closure_recovers_interruption_without_repeating_composition(closure_plan, point):
    from unittest.mock import patch

    path, cfg = closure_plan
    contexts = _contexts(path, cfg)
    semantic = _LLM(composition_example(contexts[0]))
    security = _LLM(KeyboardInterrupt()) if point == "security" else _LLM(composition_example(contexts[1]))
    if point == "evidence":
        with patch("core.delivery_verify.store_root_evidence", side_effect=KeyboardInterrupt):
            with pytest.raises(KeyboardInterrupt):
                _run(path, cfg, semantic, security, node_id="root")
    else:
        with pytest.raises(KeyboardInterrupt):
            _run(path, cfg, semantic, security, node_id="root")
    record = get_delivery_status(path).verification
    assert record.status == "interrupted"
    assert record.composition_evidence_fingerprint
    assert record.security_composition_attempted
    assert bool(record.security_composition_evidence_fingerprint) == (point == "evidence")
    unused = _LLM()
    fallback = _LLM(delivery_integration_review_control_example(set())) if point == "security" else unused
    result, _ = _run(path, cfg, unused, fallback, node_id="root")
    assert result.succeeded, result
    assert not unused.calls
    if point == "security":
        assert len(fallback.calls) == 1
        assert "CHILD_INTERNAL" in fallback.calls[0]
    status = get_delivery_status(path)
    validate_root_evidence(status, status.verification)


@pytest.mark.parametrize("failure", ["rejection", "readonly"])
def test_security_closure_boundary_preempts_fallback_and_retry(closure_plan, failure):
    from core.llm_client import LLMReadOnlyViolation

    path, cfg = closure_plan
    contexts = _contexts(path, cfg)
    control = json.loads(composition_example(contexts[1]))
    control["disposition"] = "repair_required"
    control["findings"] = [
        {"code": "security_gap", "summary": "Private data is exposed.", "unit_ids": ["consumer"], "obligation_ids": []}
    ]
    security = _LLM(LLMReadOnlyViolation("boundary")) if failure == "readonly" else _LLM(json.dumps(control))
    result, _ = _run(path, cfg, _LLM(composition_example(contexts[0])), security, node_id="root")
    assert not result.succeeded, (result, security.calls)
    assert len(security.calls) == 1
    assert result.stop_code == (
        "delivery_verification.readonly_mutation" if failure == "readonly" else "delivery_verification.repair_required"
    )
    assert result.security_status in {"blocked", "rejected"}
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert again.stop_code == result.stop_code
    assert not unused.calls


@pytest.mark.parametrize("artifact", ["child", "semantic", "security", "root"])
def test_closure_requires_every_accepted_evidence_artifact(closure_plan, artifact):
    from core.delivery_checkpoint_evidence import checkpoint_evidence_path
    from core.delivery_composition import composition_evidence_path
    from core.delivery_finalize import finalize_delivery_plan

    path, cfg = closure_plan
    semantic, security = [_LLM(composition_example(context)) for context in _contexts(path, cfg)]
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    status = get_delivery_status(path)
    directory = delivery_progress_path(Path(cfg["project"]["root_path"]), status.plan.plan_id).parent
    record = status.verification
    damaged = {
        "child": checkpoint_evidence_path(directory, status.checkpoint_verifications["storage"]),
        "semantic": composition_evidence_path(directory, record.composition_evidence_fingerprint),
        "security": composition_evidence_path(directory, record.security_composition_evidence_fingerprint),
        "root": root_evidence_path(directory, record.root_evidence_fingerprint),
    }[artifact]
    damaged.write_text("{}")
    with pytest.raises(ValueError):
        validate_root_evidence(status, record)
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert not again.succeeded and not unused.calls
    assert not finalize_delivery_plan(path, project_config=cfg).finalized


@pytest.mark.parametrize(
    "field,value", [("final_gate", "false"), ("final_gate", 0), ("checkpoint_ids", None), ("checkpoint_ids", [])]
)
def test_invalid_final_delegation_fails_plan_validation(tmp_path, field, value):
    path, _, _, _, _, _ = _prepared(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["source_accounting"][1][field] = value
    path.write_text(yaml.safe_dump(data))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    assert not checked.valid


@pytest.mark.parametrize("value", ["", "x" * 4097, "\ud800", 42])
def test_invalid_child_interface_fails_plan_validation(tmp_path, value):
    path, _, _, _, _, _ = _prepared(tmp_path)
    data = yaml.safe_load(path.read_text())
    data["checkpoints"][0]["integration_context"] = value
    path.write_text(yaml.safe_dump(data))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    assert not checked.valid


def test_final_authority_normalizes_checkout_newlines(tmp_path):
    path, _, original, _, _, _ = _prepared(tmp_path)
    for task in [tmp_path / "source.md", *(tmp_path / unit.task_path for unit in original.plan.units)]:
        task.write_bytes(task.read_text(encoding="utf-8").replace("\n", "\r\n").encode("utf-8"))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    assert checked.valid
    assert checked.plan.verified_final_gate_authority == original.plan.verified_final_gate_authority


@pytest.mark.parametrize("attribution", ["local", "global", "rerouted", "final_only"])
def test_final_interface_correction_is_independently_reverified(tmp_path, attribution):
    from copy import deepcopy
    from tests.test_delivery_authority import SOURCE

    data = _draft_data(SOURCE)
    data["checkpoints"][0]["integration_context"] = "Missing security assumptions."
    data["source_accounting"][1]["final_gate"] = attribution != "local"
    if attribution != "local":
        data["source_accounting"][1]["checkpoint_ids"] = None if attribution == "global" else []
    corrected = deepcopy(data)
    corrected["checkpoints"][0]["integration_context"] = (
        "Preserve CACHE_KEY and isolate private data across the storage interface."
    )
    corrected["source_accounting"][1]["final_gate"] = True
    if attribution == "rerouted":
        corrected["source_accounting"][1]["checkpoint_ids"] = ["storage"]
    accepted = {
        "constraints_complete": True,
        "constraints": [],
        "unit_context_complete": True,
        "unit_context_gaps": [],
        "checkpoint_authority_complete": True,
        "final_gate_authority_complete": True,
    }
    gap = {
        **accepted,
        "final_gate_authority_complete": False,
        "source_accounting_gaps": [
            {
                "source_fragment_id": data["source_accounting"][1]["source_fragment_id"],
                "summary": "Keep the exact literal in final authority and repair the child integration context.",
            }
        ],
    }
    if attribution == "final_only":
        from core.delivery_authoring import DeliveryAuthoringParseError

        with pytest.raises(DeliveryAuthoringParseError, match="unrelated"):
            _prepare(tmp_path, SOURCE, outputs=[data, gap, corrected, accepted])
        return
    path, cfg, checked, _, llm, audit = _prepare(tmp_path, SOURCE, outputs=[data, gap, corrected, accepted])
    assert len(llm.prompts) == 4
    assert checked.plan.verified_final_gate_authority
    assert "CACHE_KEY" in checked.plan.checkpoints[0].integration_context
    assert len([entry for entry in audit if entry["phase"] == "delivery_prepare_constraint_verification"]) == 2


def test_failed_full_fallback_preserves_repair_provenance(closure_plan):
    from tests.test_delivery_repair import _draft, _repair

    path, cfg = closure_plan
    contexts = _contexts(path, cfg)
    uncertain = json.loads(composition_example(contexts[0]))
    uncertain["checkpoint_results"][0]["outcome"] = "verification_required"
    status = get_delivery_status(path)
    full = json.loads(delivery_integration_review_control_example(set(status.plan.checkpoints[0].obligation_ids)))
    full["disposition"] = "repair_required"
    key = "deliver-team-invites"
    for outcome in full["obligation_results"]:
        if outcome["id"] == key:
            outcome["outcome"] = "missing"
    full["findings"] = [
        {
            "code": "integration_gap",
            "summary": "Storage no longer satisfies its outcome.",
            "unit_ids": ["foundation"],
            "obligation_ids": [key],
        }
    ]
    semantic = _LLM(json.dumps(uncertain), json.dumps(full), json.dumps(full))
    security = _LLM()
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.stop_code == "delivery_verification.repair_required", result
    assert not security.calls
    assert len(semantic.calls) == 3 and "CHILD_INTERNAL" in semantic.calls[-1]
    before = get_delivery_status(path)
    contract = Path(cfg["project"]["root_path"]) / before.plan.units[0].task_path
    repaired = _repair(path, cfg, _LLM(_draft(markdown=contract.read_text())))
    assert repaired.ready, repaired
    after = get_delivery_status(path)
    obligation = next(item for item in after.plan.obligations if item.id == key)
    assert repaired.unit_id in obligation.unit_ids
    assert after.plan.verified_final_gate_authority is None
    assert after.checkpoint_verifications["storage"] == before.checkpoint_verifications["storage"]


def test_security_rejection_survives_audit_failure(closure_plan):
    from unittest.mock import patch
    from core.delivery_verify import _safe_append_audit

    path, cfg = closure_plan
    contexts = _contexts(path, cfg)
    control = json.loads(composition_example(contexts[1]))
    control["disposition"] = "repair_required"
    control["findings"] = [
        {"code": "security_gap", "summary": "Private data is exposed.", "unit_ids": ["consumer"], "obligation_ids": []}
    ]

    def audit(path, value, **kwargs):
        return False if value.get("event") == "security_review" else _safe_append_audit(path, value, **kwargs)

    with patch("core.delivery_verify._safe_append_audit", side_effect=audit):
        result, _ = _run(path, cfg, _LLM(composition_example(contexts[0])), _LLM(json.dumps(control)), node_id="root")
    assert not result.succeeded and result.security_status == "rejected"
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert not again.succeeded and not unused.calls


def test_both_final_reviewers_ground_changed_candidate_in_delta(closure_plan):
    from core.delivery_progress import read_delivery_progress, upsert_delivery_unit_progress
    from tests.delivery_fixtures import assemble_delivery_fixture

    path, cfg = closure_plan
    root = Path(cfg["project"]["root_path"])
    (root / "agents/code.py").write_text("value = 2\n", encoding="utf-8", newline="\n")
    _git(root, "add", "agents/code.py")
    _git(root, "commit", "-m", "change shared implementation")
    progress_path = delivery_progress_path(root, "team-invites")
    progress, errors = read_delivery_progress(progress_path, plan_id="team-invites")
    assert not errors
    write_delivery_progress(
        progress_path,
        upsert_delivery_unit_progress(
            progress, make_delivery_unit_progress("consumer", "done", commit=_git(root, "rev-parse", "HEAD"))
        ),
    )
    _, _, error = assemble_delivery_fixture(path)
    assert error is None
    contexts = _contexts(path, cfg)
    for context in contexts:
        child = context["checkpoint_composition"]["children"][0]
        assert not child["exact_tree"]
        assert "+value = 2" in child["delta"]
    semantic, security = [_LLM(composition_example(context)) for context in contexts]
    result, _ = _run(path, cfg, semantic, security, node_id="root")
    assert result.succeeded, result
    assert len(semantic.calls) == len(security.calls) == 1
    for prompt in semantic.calls + security.calls:
        assert "+value = 2" in prompt and "delta:storage" in prompt
        assert "CHILD_INTERNAL" not in prompt
    unused = _LLM()
    again, _ = _run(path, cfg, unused, unused, node_id="root")
    assert again.succeeded and not unused.calls
