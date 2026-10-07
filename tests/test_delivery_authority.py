from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path

import pytest
import yaml

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from agents.delivery_preparation_agent import DeliveryPreparationAgent
from core.delivery_authority import (
    checkpoint_authority_context,
    verification_authority_fragments,
)
from core.delivery_authoring import DeliveryAuthoringParseError, parse_delivery_authoring_output
from core.delivery_obligations import delivery_authority_fragments
from core.delivery_plan import check_delivery_plan_file
from core.delivery_prepare_writer import write_delivery_prepare_artifacts
from core.delivery_verification import check_delivery_verification_readiness
from core.delivery_verification_scope import DeliveryVerificationScope
from tests.test_delivery_preparation_agent import CapturingLLM, _authoring_output
from tests.test_delivery_prepare_writer import _ready_task_markdown


SOURCE = "# Delivery\nGlobal context.\n\n## Foundation\nUse the exact `CACHE_KEY` literal.\n\n## Future\nUNRELATED consumer presentation.\n"


def _draft_data(source: str = SOURCE) -> dict:
    data = json.loads(_authoring_output(task_description=source))
    data["units"][0]["asset_paths"] = []
    data["units"][0]["task_markdown"] = _ready_task_markdown("Foundation") + "\nUse `CACHE_KEY`.\n"
    data["units"][0]["risk_tags"] = ["privacy"]
    consumer = deepcopy(data["units"][0])
    consumer.update(id="consumer", title="Consumer", depends_on=["foundation"])
    data["units"].append(consumer)
    data["checkpoints"] = [{"id": "storage", "unit_ids": ["foundation"], "obligation_ids": ["deliver-team-invites"]}]
    for index, record in enumerate(data["source_accounting"]):
        record["checkpoint_ids"] = ["storage"] if index < 2 else []
    return data


def _prepare(
    root: Path,
    source: str = SOURCE,
    data: dict | None = None,
    verification: dict | None = None,
    *,
    outputs: list[dict] | None = None,
    expect_ready: bool = True,
):
    root.mkdir(parents=True, exist_ok=True)
    (root / "agents").mkdir(exist_ok=True)
    (root / "source.md").write_text(source, encoding="utf-8", newline="\n")
    cfg = {
        "project": {"root_path": str(root), "build_tool": "python"},
        "sandbox": {"allowed_read_paths": ["."], "allowed_write_paths": ["agents/"]},
        "run_build": True,
        "run_tests": True,
        "run_checks": False,
        "build": {"test_command": "pytest"},
    }
    assessment = {
        "constraints_complete": True,
        "constraints": [],
        "unit_context_complete": True,
        "unit_context_gaps": [],
        "checkpoint_authority_complete": True,
    }
    assessment.update(verification or {})
    llm = CapturingLLM(json.dumps(data or _draft_data(source)), verification_output=json.dumps(assessment))
    if outputs is not None:
        llm.outputs = [json.dumps(item) for item in outputs]
    audit = []
    draft = DeliveryPreparationAgent(llm, cfg).author_delivery_plan(
        task_description=source,
        task_path="source.md",
        plan_id="team-invites",
        project_root=root,
        output_dir=".sikula/delivery/team-invites",
        audit_recorder=audit.append,
    )
    written = write_delivery_prepare_artifacts(
        draft,
        project_root=root,
        project_config=cfg,
        output_dir=".sikula/delivery/team-invites",
        source_task_description=source,
        source_task_path="source.md",
    )
    assert written.prepared == expect_ready, written.to_dict()
    if not expect_ready:
        return written, draft, llm, audit
    path = root / written.paths.plan_file
    checked = check_delivery_plan_file(path, project_root=root)
    assert checked.valid, checked.errors
    return path, cfg, checked, draft, llm, audit


def _prompt(root: Path, cfg: dict, source: str, scope: DeliveryVerificationScope, kind: str = "semantic") -> str:
    return DeliveryIntegrationReviewAgent(None, cfg)._prompt(
        cwd=root,
        review_kind=kind,
        source_task=source,
        plan_context=scope.plan_context(),
        validation_summary={},
        candidate_commit="a" * 40,
        candidate_tree="b" * 40,
        known_obligation_ids=set(scope.obligation_ids) if kind == "semantic" else set(),
    )


def test_independent_preparation_publishes_bound_authority_and_private_audit(tmp_path):
    path, cfg, checked, draft, llm, audit = _prepare(tmp_path)
    assert len(llm.prompts) == 2
    assert "COMPLETE authoritative source" in llm.prompts[1]
    assert "UNRELATED" in llm.prompts[1]  # The verifier can assess exclusions.
    assert audit[-1]["parsed"]["checkpoint_authority_input"] == draft.constraint_verification.checkpoint_authority_input
    assert checked.plan.checkpoint_authority.keys() == {"storage"}
    assert checked.plan.verified_checkpoint_authority.keys() == {"storage"}
    assert "rationale:" not in path.read_text()
    scope = DeliveryVerificationScope.from_plan(checked.plan, "storage")
    fragments = verification_authority_fragments(SOURCE, scope.plan_context())
    original = delivery_authority_fragments(SOURCE)
    assert [(item["id"], item["text"], item["sha256"]) for item in fragments] == [
        (item.id, item.text, item.sha256) for item in original[:2]
    ]
    for kind in ("semantic", "security"):
        prompt = _prompt(tmp_path, cfg, SOURCE, scope, kind)
        assert "CACHE_KEY" in prompt and "UNRELATED" not in prompt
        assert "consumer" not in prompt
    final = DeliveryVerificationScope.from_plan(checked.plan)
    assert "UNRELATED" in _prompt(tmp_path, cfg, SOURCE, final)
    assert len(final.plan_context()["source_accounting"]) == len(original)
    readiness = check_delivery_verification_readiness(checked, cfg, node_id="storage")
    assert readiness.ready, readiness.errors
    assert (
        readiness.packet_bytes
        >= max(len(_prompt(tmp_path, cfg, SOURCE, scope, kind).encode()) for kind in ("semantic", "security")) + 512
    )


@pytest.mark.parametrize("change", ["receipt", "contract", "routing", "source", "missing_receipt"])
def test_changed_authority_falls_back_to_complete_source(tmp_path, change):
    path, cfg, checked, _, _, _ = _prepare(tmp_path)
    data = yaml.safe_load(path.read_text())
    source = SOURCE
    if change == "receipt":
        data["checkpoint_authority"]["storage"] = "sha256:" + "0" * 64
    elif change == "contract":
        task = tmp_path / checked.plan.units[0].task_path
        task.write_text(task.read_text() + "\nChanged authority.\n")
    elif change == "routing":
        data["source_accounting"][-1]["checkpoint_ids"] = ["storage"]
    elif change == "source":
        source += "\n## Extra\nAnother requirement.\n"
        (tmp_path / "source.md").write_text(source)
        data["source_task"]["sha256"] = "sha256:" + sha256(source.encode()).hexdigest()
        data["source_accounting"][-1]["source_fragment_id"] = delivery_authority_fragments(source)[-2].id
        data["source_accounting"].append(
            {**data["source_accounting"][-1], "source_fragment_id": delivery_authority_fragments(source)[-1].id}
        )
    else:
        del data["checkpoint_authority"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    result = check_delivery_plan_file(path, project_root=tmp_path)
    assert result.valid, result.errors
    assert not result.plan.verified_checkpoint_authority
    scope = DeliveryVerificationScope.from_plan(result.plan, "storage")
    assert "authority_packet" not in scope.plan_context()
    assert "UNRELATED" in _prompt(tmp_path, cfg, source, scope)


def test_unrelated_pending_contract_does_not_change_node_packet(tmp_path):
    path, cfg, checked, _, _, _ = _prepare(tmp_path)
    before = DeliveryVerificationScope.from_plan(checked.plan, "storage")
    task = tmp_path / checked.plan.units[1].task_path
    task.write_text(task.read_text() + "\nUNRELATED future context.\n" * 50)
    after = DeliveryVerificationScope.from_plan(check_delivery_plan_file(path, project_root=tmp_path).plan, "storage")
    assert _prompt(tmp_path, cfg, SOURCE, before) == _prompt(tmp_path, cfg, SOURCE, after)


def test_unrelated_source_units_and_components_do_not_grow_checkpoint_prompts(tmp_path):
    lengths = []
    estimates = []
    for count in (1, 30):
        root = tmp_path / str(count)
        source = SOURCE + "".join(f"\n## Future {index}\nUNRELATED detail {index}.\n" for index in range(count))
        data = _draft_data(source)
        for index in range(count):
            data["units"].append({**deepcopy(data["units"][1]), "id": f"future-{index}"})
        path, cfg, checked, _, _, _ = _prepare(root, source, data)
        # Components are plan context too. The node has no contributors in these.
        raw = yaml.safe_load(path.read_text())
        raw["title"] = "UNRELATED future delivery context " * count
        raw["components"] = [{"id": f"unrelated-{index}", "path": "agents"} for index in range(count)]
        path.write_text(yaml.safe_dump(raw, sort_keys=False))
        checked = check_delivery_plan_file(path, project_root=root)
        assert checked.valid, checked.errors
        scope = DeliveryVerificationScope.from_plan(checked.plan, "storage")
        assert "authority_packet" in scope.plan_context()
        lengths.append([len(_prompt(root, cfg, source, scope, role).encode()) for role in ("semantic", "security")])
        estimates.append(check_delivery_verification_readiness(checked, cfg, node_id="storage").packet_bytes)
    assert lengths[0] == lengths[1]
    assert estimates[0] == estimates[1]


def test_writer_rejects_reusing_verification_for_a_changed_draft(tmp_path):
    _, cfg, _, draft, _, _ = _prepare(tmp_path)
    modified = replace(
        draft,
        units=[
            replace(draft.units[0], task_markdown=draft.units[0].task_markdown + "\nNew requirement.\n"),
            draft.units[1],
        ],
    )
    result = write_delivery_prepare_artifacts(modified, project_root=tmp_path, project_config=cfg, output_dir="other")
    assert not result.prepared
    assert result.errors[0].code == "delivery_prepare.authority_unresolved"
    assert not (tmp_path / "other").exists()


def test_preparer_rejects_omitted_contributor_authority_before_verification(tmp_path):
    data = _draft_data()
    data["source_accounting"][0]["checkpoint_ids"] = []
    with pytest.raises(DeliveryAuthoringParseError) as error:
        parse_delivery_authoring_output(
            json.dumps(data),
            project_root=tmp_path,
            output_dir=".sikula/delivery/team-invites",
            expected_plan_id="team-invites",
            source_task_description=SOURCE,
        )
    assert error.value.code == "source_accounting.checkpoint_incomplete"


def test_unverified_scope_declaration_cannot_omit_source(tmp_path):
    path, cfg, _, _, _, _ = _prepare(tmp_path)
    data = yaml.safe_load(path.read_text())
    del data["checkpoint_authority"]
    data["source_accounting"][0]["checkpoint_ids"] = []
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    assert checked.valid
    assert "Global context" in _prompt(
        tmp_path, cfg, SOURCE, DeliveryVerificationScope.from_plan(checked.plan, "storage")
    )


def test_packet_rejects_changed_source_and_preserves_windows_newlines(tmp_path):
    path, _, checked, _, _, _ = _prepare(tmp_path)
    context = DeliveryVerificationScope.from_plan(checked.plan, "storage").plan_context()
    with pytest.raises(ValueError):
        verification_authority_fragments(SOURCE + "Changed", context)
    for unit in checked.plan.units:
        task = tmp_path / unit.task_path
        task.write_bytes(task.read_bytes().replace(b"\n", b"\r\n"))
    (tmp_path / "source.md").write_bytes(SOURCE.replace("\n", "\r\n").encode())
    restored = check_delivery_plan_file(path, project_root=tmp_path)
    assert restored.valid
    assert restored.plan.verified_checkpoint_authority == checked.plan.verified_checkpoint_authority


def test_shared_constraint_and_cross_group_contribution_remain_in_packet(tmp_path):
    path, cfg, checked, _, _, _ = _prepare(tmp_path)
    data = yaml.safe_load(path.read_text())
    shared = delivery_authority_fragments(SOURCE)[1].id
    data["constraints"] = [
        {
            "id": "privacy",
            "kind": "security_boundary",
            "summary": "Preserve isolation across integration boundaries.",
            "unit_ids": ["foundation", "consumer"],
            "disposition": "preserved",
        }
    ]
    data["obligations"].append(
        {
            "id": "end-to-end",
            "summary": "Integrated consumers preserve storage guarantees.",
            "source_fragment_ids": [shared],
            "unit_ids": ["foundation", "consumer"],
        }
    )
    data["source_accounting"][1].update(disposition="mapped", constraint_ids=["privacy"], obligation_ids=["end-to-end"])
    data.pop("checkpoint_authority")
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    check = check_delivery_plan_file(path, project_root=tmp_path)
    assert check.valid
    context = checkpoint_authority_context(check.plan, "storage", SOURCE)
    assert context["constraints"][0]["id"] == "privacy"
    assert context["constraints"][0]["unit_ids"] == ["foundation"]
    assert context["source_accounting"][1]["responsibility"] == "contribution_and_final"
    assert [item["id"] for item in context["obligations"]] == ["deliver-team-invites"]
    assert context["authority_packet"]["remaining_authority"] == "final_gate"
    assert "CACHE_KEY" in json.dumps(verification_authority_fragments(SOURCE, context))


def test_enclosing_context_is_included_even_if_author_routes_only_a_list_item(tmp_path):
    source = "# Delivery\nGlobal context.\n\n## Foundation\nEnclosing REQUIRED context.\n- Use `CACHE_KEY`.\n\n## Future\nUNRELATED.\n"
    data = _draft_data(source)
    data["source_accounting"][1]["checkpoint_ids"] = []
    data["source_accounting"][2]["checkpoint_ids"] = ["storage"]
    _, cfg, checked, _, _, _ = _prepare(tmp_path, source, data)
    scope = DeliveryVerificationScope.from_plan(checked.plan, "storage")
    prompt = _prompt(tmp_path, cfg, source, scope)
    assert "Enclosing REQUIRED context" in prompt
    assert "CACHE_KEY" in prompt and "UNRELATED" not in prompt


@pytest.mark.parametrize("resolved", [True, False])
def test_context_only_attribution_disagreement_uses_one_audited_correction(tmp_path, resolved):
    authored = _draft_data()
    corrected = deepcopy(authored)
    corrected["source_accounting"][-1]["checkpoint_ids"] = None
    corrected["source_accounting"][-1]["rationale"] = "The checkpoint needs this shared contextual requirement."
    rejected = {
        "constraints_complete": True,
        "constraints": [],
        "unit_context_complete": True,
        "unit_context_gaps": [],
        "checkpoint_authority_complete": False,
        "source_accounting_gaps": [
            {
                "source_fragment_id": authored["source_accounting"][-1]["source_fragment_id"],
                "summary": "A shared contextual rule must govern the checkpoint too.",
            }
        ],
    }
    accepted = {**rejected, "checkpoint_authority_complete": True, "source_accounting_gaps": []}
    result = _prepare(
        tmp_path, outputs=[authored, rejected, corrected, accepted if resolved else rejected], expect_ready=resolved
    )
    if resolved:
        _, cfg, checked, _, llm, audit = result
        scope = DeliveryVerificationScope.from_plan(checked.plan, "storage")
        assert "UNRELATED" in _prompt(tmp_path, cfg, SOURCE, scope)
    else:
        written, _, llm, audit = result
        assert written.failure_reason == "authority_unresolved"
        assert not (tmp_path / ".sikula/delivery/team-invites/plan.yaml").exists()
    assert len(llm.prompts) == 4
    assert [record["phase"] for record in audit] == [
        "delivery_prepare_authoring",
        "delivery_prepare_constraint_verification",
        "delivery_prepare_draft_recovery",
        "delivery_prepare_constraint_verification",
    ]


@pytest.mark.parametrize("kind", ["semantic", "security"])
def test_actual_reviewer_calls_and_format_retry_share_the_scoped_packet(tmp_path, kind):
    from tests.test_delivery_repair import _LLM

    _, cfg, checked, _, _, _ = _prepare(tmp_path)
    scope = DeliveryVerificationScope.from_plan(checked.plan, "storage")
    output = json.dumps(
        {
            "schema_version": 2,
            "disposition": "approved",
            "summary": "Verified.",
            "findings": [],
            "obligation_results": [{"id": key, "outcome": "satisfied"} for key in scope.obligation_ids]
            if kind == "semantic"
            else [],
        }
    )
    llm = _LLM("malformed", output)
    result = DeliveryIntegrationReviewAgent(llm, cfg).review(
        cwd=tmp_path,
        review_kind=kind,
        source_task=SOURCE,
        plan_context=scope.plan_context(),
        validation_summary={},
        candidate_commit="a" * 40,
        candidate_tree="b" * 40,
        known_unit_ids=set(scope.unit_ids),
        known_obligation_ids=set(scope.obligation_ids) if kind == "semantic" else set(),
    )
    assert result.assessment.approved
    assert len(result.attempts) == len(llm.calls) == 2
    assert all("CACHE_KEY" in prompt and "UNRELATED" not in prompt for prompt in llm.calls)
    readiness = check_delivery_verification_readiness(checked, cfg, node_id="storage")
    assert all(len(prompt.encode()) <= readiness.packet_bytes for prompt in llm.calls)


def test_escaped_authority_and_rules_are_bounded_before_calls(tmp_path):
    source = SOURCE.replace("Use the exact `CACHE_KEY` literal.", "Use `CACHE_KEY`. " + "\U0001f512" * 25000)
    _, cfg, checked, _, _, _ = _prepare(tmp_path, source)
    (tmp_path / "review-rules.md").write_text("review " * 30000)
    cfg["reviewer"] = {"extra_rules": "review-rules.md"}
    cfg["security_reviewer"] = {"extra_rules": "review-rules.md"}
    readiness = check_delivery_verification_readiness(checked, cfg, node_id="storage")
    assert not readiness.ready
    assert any(issue.code == "delivery_verification.hierarchy_required" for issue in readiness.errors)


def test_missing_receipt_cannot_authorize_an_oversized_full_fallback(tmp_path):
    source = SOURCE.replace("UNRELATED consumer presentation.", "UNRELATED " + "\U0001f512" * 25000)
    path, cfg, checked, _, _, _ = _prepare(tmp_path, source)
    (tmp_path / "review-rules.md").write_text("Review.\n" * 17000)
    cfg["reviewer"] = {"extra_rules": "review-rules.md"}
    cfg["security_reviewer"] = {"extra_rules": "review-rules.md"}
    cfg["security"] = {"context": "Security. " * 8000}
    assert check_delivery_verification_readiness(checked, cfg, node_id="storage").ready
    # Whole-plan/final-gate limits still apply even when an individual node fits.
    assert not check_delivery_verification_readiness(checked, cfg).ready
    data = yaml.safe_load(path.read_text())
    del data["checkpoint_authority"]
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    checked = check_delivery_plan_file(path, project_root=tmp_path)
    fallback = check_delivery_verification_readiness(checked, cfg, node_id="storage")
    assert not fallback.ready
    assert any(issue.code == "delivery_verification.hierarchy_required" for issue in fallback.errors)
    assert not (tmp_path / ".sikula/state").exists()
