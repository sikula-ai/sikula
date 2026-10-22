"""Bounded preparation protocol recovery must not erase findings or authority."""

from copy import deepcopy
import json
from unittest.mock import Mock

import pytest

from agents.delivery_preparation_agent import DeliveryPreparationAgent, DeliveryPreparationAgentError
from core.delivery_authoring import (
    DeliveryAuthoringConstraintDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringProtocolError,
    delivery_output_has_prerequisite_stop,
    delivery_verification_gap_contract,
    parse_delivery_authoring_output,
    parse_delivery_constraint_verification_output,
    public_delivery_preparation_failure,
)
from core.llm_client import LLMReadOnlyViolation
from tests.test_delivery_preparation_agent import _authoring_output, _DEFAULT_TASK_DESCRIPTION


def _case(root):
    payload = json.loads(_authoring_output())
    draft = parse_delivery_authoring_output(
        json.dumps(payload),
        expected_plan_id="team-invites",
        project_root=root,
        output_dir=".sikula/delivery/team-invites",
        source_task_description=_DEFAULT_TASK_DESCRIPTION,
        require_obligations=True,
        require_source_accounting=True,
    )
    review = {
        "constraints_complete": True,
        "constraints": [],
        "constraint_gaps": [],
        "obligations_complete": True,
        "obligations": payload["obligations"],
        "obligation_gaps": [],
        "source_accounting": payload["source_accounting"],
        "unit_context_complete": True,
        "unit_context_gaps": [],
    }
    arguments = dict(
        authority_description=_DEFAULT_TASK_DESCRIPTION,
        constraints=draft.constraints,
        obligations=draft.obligations,
        units=draft.units,
        source_accounting=draft.source_accounting,
        verification_scope="test_source_to_units",
        completeness_rule="Preserve the complete source.",
        audit_phase="delivery_prepare_constraint_verification",
        round_index=1,
        verify_unit_context=True,
        verify_obligations=True,
    )
    return review, arguments


def _reviewer(outputs):
    llm = Mock()
    llm.generate.side_effect = [json.dumps(item) if isinstance(item, dict) else item for item in outputs]
    return DeliveryPreparationAgent(llm), llm


@pytest.mark.parametrize("gap_kind", ["constraint", "obligation"])
@pytest.mark.parametrize("round_index", [1, 2])
def test_verifier_corrects_extra_gap_fields_once_without_losing_findings(tmp_path, gap_kind, round_index):
    review, arguments = _case(tmp_path)
    gap = {
        "reason": "omitted",
        "summary": "A missing requirement needs explicit delivery ownership.",
        "affected_unit_ids": ["foundation"],
    }
    if gap_kind == "constraint":
        gap["kind"] = "repository_ownership"
    else:
        gap["source_fragment_ids"] = review["obligations"][0]["source_fragment_ids"]
    review[gap_kind + "s_complete"] = False
    review[gap_kind + "_gaps"] = [gap]
    malformed = deepcopy(review)
    malformed[gap_kind + "_gaps"][0]["detail"] = "PRIVATE_DETAIL must stay in audit."
    agent, llm = _reviewer([malformed, review])
    audit = []
    arguments["round_index"] = round_index
    result = agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert not getattr(result, gap_kind + "s_complete")
    assert len(getattr(result, gap_kind + "_gaps")) == 1
    assert llm.generate.call_count == 2
    assert [item["round_index"] for item in audit] == [round_index, round_index]
    assert [item["protocol_attempt"] for item in audit] == [1, 2]
    assert [item["parsed"]["status"] for item in audit] == ["failed", "parsed"]
    assert audit[0]["parsed"]["error_code"] == "delivery_authoring.unknown_field"
    assert json.loads(audit[0]["raw_output"]) == malformed
    assert json.loads(audit[1]["raw_output"]) == review
    assert "PRIVATE_DETAIL" not in audit[1]["prompt"]
    assert "Protocol correction (one retry)" in audit[1]["prompt"]
    contract = json.dumps(delivery_verification_gap_contract(), indent=2, sort_keys=True)
    assert contract in audit[0]["prompt"] and contract in audit[1]["prompt"]


@pytest.mark.parametrize("malformation", ["json", "extra_field", "source_accounting"])
def test_repeated_protocol_failure_stops_with_complete_audit_and_public_diagnostic(tmp_path, malformation):
    review, arguments = _case(tmp_path)
    malformed = deepcopy(review)
    if malformation == "json":
        malformed = "PRIVATE invalid response"
    elif malformation == "extra_field":
        malformed["detail"] = "PRIVATE diagnostic"
    else:
        malformed["source_accounting"][0]["detail"] = "PRIVATE accounting explanation"
    agent, llm = _reviewer([malformed, malformed, review])
    audit = []
    with pytest.raises(DeliveryAuthoringProtocolError) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert llm.generate.call_count == len(audit) == 2
    assert [item["protocol_attempt"] for item in audit] == [1, 2]
    assert all(item["parsed"]["status"] == "failed" for item in audit)
    code, message = public_delivery_preparation_failure(caught.value)
    assert code == caught.value.code
    assert "delivery_prepare_constraint_verification" in message
    assert "PRIVATE" not in str((code, message))


@pytest.mark.parametrize("failure", ["identity", "provenance", "accounting", "prerequisite", "readonly", "provider"])
def test_verifier_does_not_retry_authority_changes_or_prerequisite_and_provider_stops(tmp_path, failure):
    review, arguments = _case(tmp_path)
    malformed = deepcopy(review)
    if failure == "identity":
        malformed["obligations"][0]["id"] = "different-outcome"
    elif failure == "provenance":
        malformed["obligations"][0]["source_fragment_ids"] = ["unknown-fragment"]
    elif failure == "accounting":
        malformed["source_accounting"][0]["rationale"] = "Changed supplied accounting."
    elif failure == "prerequisite":
        malformed["constraints_complete"] = False
        malformed["constraint_gaps"] = [{"kind": "stop_and_follow_up", "detail": "PRIVATE invalid extra field"}]
    elif failure == "readonly":
        malformed = LLMReadOnlyViolation("PRIVATE workspace mutation")
    else:
        malformed = RuntimeError("PRIVATE provider failure")
    agent, llm = _reviewer([malformed, review])
    audit = []
    with pytest.raises((DeliveryAuthoringParseError, DeliveryPreparationAgentError)) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert llm.generate.call_count == len(audit) == 1
    assert audit[0]["parsed"]["status"] == "failed"
    assert public_delivery_preparation_failure(caught.value) is not None
    assert "PRIVATE" not in str(public_delivery_preparation_failure(caught.value))


def test_protocol_retry_still_checks_supplied_authority(tmp_path):
    review, arguments = _case(tmp_path)
    changed = deepcopy(review)
    changed["obligations"][0]["summary"] = "Weakened required behavior."
    agent, llm = _reviewer(["not JSON", changed, review])
    audit = []
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert caught.value.code == "delivery_obligation_verification.obligations_mismatch"
    assert llm.generate.call_count == len(audit) == 2


@pytest.mark.parametrize("gap_kind", ["constraint", "obligation"])
@pytest.mark.parametrize("reason", ["omitted", "incompletely_assigned"])
def test_prompt_gap_examples_and_allowed_fields_match_parser(tmp_path, gap_kind, reason):
    review, arguments = _case(tmp_path)
    contract = delivery_verification_gap_contract()[gap_kind + "_gaps"]
    gap = deepcopy(contract[reason + "_example"])
    gap["affected_unit_ids"] = ["consumer"]
    gap["summary"] = "An explicit boundary must remain enforced."
    if gap_kind == "constraint":
        existing = {
            "id": "existing-rule",
            "kind": gap["kind"],
            "summary": gap["summary"],
            "unit_ids": ["foundation"],
            "disposition": "preserved",
        }
        review["constraints"] = [existing]
        review["source_accounting"][0]["constraint_ids"] = [existing["id"]]
    else:
        existing = review["obligations"][0]
        gap["summary"] = existing["summary"]
        gap["source_fragment_ids"] = existing["source_fragment_ids"]
    if reason == "incompletely_assigned":
        gap[gap_kind + "_id"] = existing["id"]
    review[gap_kind + "s_complete"] = False
    review[gap_kind + "_gaps"] = [gap]
    # Schema examples are parsed against real identities, not merely string-matched.
    parse_delivery_constraint_verification_output(
        json.dumps(review), unit_ids={"foundation", "consumer"}, source_task_description=_DEFAULT_TASK_DESCRIPTION
    )
    for key in contract["allowed_fields"]:
        assert key in gap or key == gap_kind + "_id"
    gap["detail"] = "Unsupported"
    with pytest.raises(DeliveryAuthoringProtocolError):
        parse_delivery_constraint_verification_output(
            json.dumps(review), unit_ids={"foundation", "consumer"}, source_task_description=_DEFAULT_TASK_DESCRIPTION
        )


@pytest.mark.parametrize("malformation", ["json", "extra_field", "source_accounting"])
def test_authoring_uses_its_existing_retry_budget_for_protocol_errors(tmp_path, malformation):
    from tests.test_delivery_preparation_agent import _author_delivery_plan

    authored = json.loads(_authoring_output())
    review, _ = _case(tmp_path)
    malformed = deepcopy(authored)
    if malformation == "json":
        malformed = "Malformed response"
    elif malformation == "extra_field":
        malformed["detail"] = "PRIVATE provider explanation"
    else:
        malformed["source_accounting"][0]["detail"] = "PRIVATE explanation"
    agent, llm = _reviewer([malformed, authored, review])
    audit = []
    result = _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit)
    assert result.constraint_verification.obligations_complete
    assert llm.generate.call_count == len(audit) == 3
    assert [item["phase"] for item in audit] == [
        "delivery_prepare_authoring",
        "delivery_prepare_authoring",
        "delivery_prepare_constraint_verification",
    ]
    assert [item["round_index"] for item in audit] == [1, 2, 1]
    assert audit[0]["parsed"]["status"] == "failed"


def test_public_error_vocabulary_covers_declared_parser_and_agent_codes():
    # Newly introduced fixed codes must not silently become generic public errors.
    import ast
    from pathlib import Path
    import re
    from core.delivery_authoring_diagnostics import PUBLIC_AUTHORING_ERROR_CODES

    root = Path(__file__).resolve().parents[1]
    for filename in ("core/delivery_authoring.py", "agents/delivery_preparation_agent.py"):
        for node in ast.walk(ast.parse((root / filename).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and re.fullmatch(r"(?:delivery_[a-z_]+|source_accounting)\.[a-z_]+", node.value)
            ):
                assert node.value in PUBLIC_AUTHORING_ERROR_CODES
    for filename in ("core/delivery_source_accounting.py", "core/delivery_authority.py"):
        for node in ast.walk(ast.parse((root / filename).read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in {"SourceAccountingError", "SourceAccountingProtocolError"}
                and node.args
                and isinstance(node.args[0], ast.Constant)
            ):
                assert "source_accounting." + node.args[0].value in PUBLIC_AUTHORING_ERROR_CODES


@pytest.mark.parametrize("prose", [False, True])
def test_malformed_authoring_cannot_retry_away_a_recognizable_prerequisite(tmp_path, prose):
    from tests.test_delivery_preparation_agent import _author_delivery_plan

    payload = json.loads(_authoring_output())
    payload["constraints"] = [{"kind": "stop_and_follow_up", "detail": "An external prerequisite is unavailable."}]
    payload["detail"] = "Unexpected authoring field."
    output = json.dumps(payload)
    if prose:
        output = "Result:\n```json\n" + output + "\n```\nExplanation."
    agent, llm = _reviewer([output, json.loads(_authoring_output())])
    audit = []
    with pytest.raises(DeliveryAuthoringProtocolError) as caught:
        _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit)
    assert caught.value.phase == "delivery_prepare_authoring"
    assert llm.generate.call_count == len(audit) == 1


def test_unavailable_audit_preempts_protocol_retry(tmp_path):
    review, arguments = _case(tmp_path)
    agent, llm = _reviewer(["invalid", review])
    with pytest.raises(OSError):
        agent._verify_constraint_continuity(**arguments, audit_recorder=Mock(side_effect=OSError("Audit unavailable")))
    assert llm.generate.call_count == 1


@pytest.mark.parametrize("phase", ["authoring", "verification"])
@pytest.mark.parametrize("stop_field", ["constraints", "constraint_gaps"])
@pytest.mark.parametrize("envelope", ["trailing_comma", "multiple_objects", "truncated", "escaped_fields"])
def test_malformed_envelope_cannot_retry_away_a_prerequisite(tmp_path, phase, stop_field, envelope):
    from tests.test_delivery_preparation_agent import _author_delivery_plan

    review, arguments = _case(tmp_path)
    payload = json.loads(_authoring_output()) if phase == "authoring" else deepcopy(review)
    payload[stop_field] = [{"kind": "stop_and_follow_up", "summary": "Missing external prerequisite."}]
    output = json.dumps(payload)
    if envelope == "trailing_comma":
        output = output[:-1] + ",}"
    elif envelope == "multiple_objects":
        output += "\n" + (_authoring_output() if phase == "authoring" else json.dumps(review))
    elif envelope == "truncated":
        output = output[: output.index('"stop_and_follow_up"') + len('"stop_and_follow_up"')]
    else:
        output = output.replace('"kind"', r'"k\u0069nd"').replace('"stop_and_follow_up"', r'"stop_and_follow_\u0075p"')
        output = "```json\n" + output[:-1] + ",}\n```"
    agent, llm = _reviewer([output, json.loads(_authoring_output()) if phase == "authoring" else review])
    audit = []
    with pytest.raises(DeliveryAuthoringProtocolError) as caught:
        if phase == "authoring":
            _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit)
        else:
            agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert llm.generate.call_count == len(audit) == 1
    assert audit[0]["raw_output"] == output
    assert audit[0]["parsed"]["status"] == "failed"
    assert audit[0]["parsed"]["error_code"] == caught.value.code
    assert public_delivery_preparation_failure(caught.value)[0] == caught.value.code


@pytest.mark.parametrize(
    "payload",
    [
        {"summary": 'Example: {"kind": "stop_and_follow_up"}'},
        {"kind": "repository_ownership", "summary": "stop_and_follow_up"},
        {"detail": "stop_and_follow_up"},
    ],
)
def test_malformed_envelope_does_not_treat_quoted_text_as_a_prerequisite(payload):
    output = json.dumps(payload)[:-1] + ",}"
    assert not delivery_output_has_prerequisite_stop(output)


@pytest.mark.parametrize("phase", ["authoring", "verification"])
@pytest.mark.parametrize("stop_first", [False, True])
def test_partial_stop_object_preempts_retry_even_when_main_object_can_be_extracted(tmp_path, phase, stop_first):
    from tests.test_delivery_preparation_agent import _author_delivery_plan

    review, arguments = _case(tmp_path)
    clean = json.loads(_authoring_output()) if phase == "authoring" else review
    main = json.dumps({**clean, "detail": "Unsupported field"})
    stop = json.dumps({"constraint_gaps": [{"kind": "stop_and_follow_up", "summary": "External input missing."}]})
    output = "\n".join([stop, main] if stop_first else [main, stop])
    agent, llm = _reviewer([output, clean])
    audit = []
    with pytest.raises(DeliveryAuthoringProtocolError) as caught:
        if phase == "authoring":
            _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit)
        else:
            agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert caught.value.code == "delivery_authoring.unknown_field"
    assert llm.generate.call_count == len(audit) == 1
    assert audit[0]["raw_output"] == output
    assert audit[0]["parsed"]["status"] == "failed"


@pytest.mark.parametrize("field", ["unit_contract_gaps", "source_accounting_gaps"])
@pytest.mark.parametrize("failure", ["extra_field", "unknown_identity", "missing_identity", "invalid_summary"])
def test_authority_gap_protocol_correction_preserves_semantic_checks(tmp_path, field, failure):
    review, arguments = _case(tmp_path)
    identity_key, identity = (
        ("unit_id", "foundation")
        if field == "unit_contract_gaps"
        else ("source_fragment_id", review["source_accounting"][0]["source_fragment_id"])
    )
    review[field] = [{identity_key: identity, "summary": "Missing required behavior."}]
    malformed = deepcopy(review)
    gap = malformed[field][0]
    gap["detail"] = "PRIVATE explanation"
    if failure == "unknown_identity":
        gap[identity_key] = "unknown-identity"
    elif failure == "missing_identity":
        del gap[identity_key]
    elif failure == "invalid_summary":
        gap["summary"] = ""
    agent, llm = _reviewer([malformed, review])
    audit = []
    if failure == "extra_field":
        result = agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
        assert getattr(result, field) == review[field]
        assert llm.generate.call_count == len(audit) == 2
        assert [record["protocol_attempt"] for record in audit] == [1, 2]
        assert [record["parsed"]["status"] for record in audit] == ["failed", "parsed"]
        assert audit[0]["parsed"]["error_code"] == "delivery_authoring.unknown_field"
        assert "PRIVATE" not in audit[1]["prompt"]
    else:
        with pytest.raises(DeliveryAuthoringParseError) as caught:
            agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
        assert not isinstance(caught.value, DeliveryAuthoringProtocolError)
        assert llm.generate.call_count == len(audit) == 1
        assert caught.value.code == "delivery_authority.gaps_invalid"


@pytest.mark.parametrize("extra_at", ["top", "constraint", "obligation", "accounting", "gap"])
@pytest.mark.parametrize(
    "change",
    ["constraint", "identity", "provenance", "owners", "rationale", "accounting_reference", "accounting_scope"],
)
def test_unsupported_fields_cannot_mask_verification_authority_changes(tmp_path, extra_at, change):
    review, arguments = _case(tmp_path)
    constraint = DeliveryAuthoringConstraintDraft(
        "storage-rule", "repository_ownership", "Retain the existing storage owner.", ["foundation"], "preserved"
    )
    arguments["constraints"] = [constraint]
    review["constraints"] = [constraint.to_plan_dict()]
    # Keep the supplied source accounting and echoed records mutually consistent.
    from dataclasses import replace

    arguments["source_accounting"] = [
        replace(record, constraint_ids=[constraint.id]) for record in arguments["source_accounting"]
    ]
    review["source_accounting"] = [record.to_verification_dict() for record in arguments["source_accounting"]]
    malformed = deepcopy(review)
    if change == "constraint":
        malformed["constraints"][0]["kind"] = "authoritative_dependency"
    elif change == "identity":
        malformed["obligations"][0]["id"] = "different-outcome"
    elif change == "provenance":
        malformed["obligations"][0]["source_fragment_ids"] = ["unknown-fragment"]
    elif change == "owners":
        malformed["obligations"][0]["unit_ids"] = ["other-unit"]
    elif change == "rationale":
        malformed["source_accounting"][0]["rationale"] = "Changed accounting rationale."
    elif change == "accounting_reference":
        malformed["source_accounting"][0]["constraint_ids"] = []
    else:
        malformed["source_accounting"][0]["checkpoint_ids"] = []
    malformed["unit_contract_gaps"] = [{"unit_id": "foundation", "summary": "Missing behavior."}]
    target = {
        "top": malformed,
        "constraint": malformed["constraints"][0],
        "obligation": malformed["obligations"][0],
        "accounting": malformed["source_accounting"][0],
        "gap": malformed["unit_contract_gaps"][0],
    }[extra_at]
    target["detail"] = "PRIVATE unsupported explanation"
    agent, llm = _reviewer([malformed, review])
    audit = []
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert not isinstance(caught.value, DeliveryAuthoringProtocolError)
    assert llm.generate.call_count == len(audit) == 1
    assert json.loads(audit[0]["raw_output"]) == malformed
    assert audit[0]["parsed"]["error_code"] == caught.value.code
    assert "PRIVATE" not in str(public_delivery_preparation_failure(caught.value))


def test_protocol_veto_preserves_normalization_defaults_and_independent_dispositions(tmp_path):
    review, arguments = _case(tmp_path)
    review["obligations"][0]["disposition"] = "needs_review"
    review["obligations"][0]["id"] = " " + review["obligations"][0]["id"] + " "
    review["source_accounting"][0].update(checkpoint_ids=None, final_gate=True)
    malformed = deepcopy(review)
    malformed["detail"] = "Unexpected field"
    agent, llm = _reviewer([malformed, review])
    audit = []
    result = agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert result.obligations[0].disposition == "needs_review"
    assert llm.generate.call_count == len(audit) == 2


@pytest.mark.parametrize("extra_at", ["none", "top", "gap", "obligation"])
@pytest.mark.parametrize(
    "failure",
    [
        "obligation_source",
        "obligation_owner",
        "obligation_identity",
        "obligation_duplicate",
        "constraint_owner",
        "constraint_identity",
        "constraint_duplicate",
        "context_source",
        "context_owner",
        "unit_contract_identity",
        "accounting_identity",
    ],
)
def test_gap_semantic_failures_preempt_unsupported_fields_everywhere(tmp_path, failure, extra_at):
    clean, arguments = _case(tmp_path)
    review = deepcopy(clean)
    if failure.startswith("obligation_"):
        field = "obligation_gaps"
        review["obligations_complete"] = False
        gap = {
            "reason": "omitted",
            "summary": "Additional required behavior.",
            "source_fragment_ids": review["obligations"][0]["source_fragment_ids"],
            "affected_unit_ids": ["foundation"],
        }
        if failure == "obligation_source":
            gap["source_fragment_ids"] = ["unknown-source"]
            expected_code = "delivery_obligation_verification.gap_source_refs_invalid"
        elif failure == "obligation_owner":
            gap["affected_unit_ids"] = ["unknown-unit"]
            expected_code = "delivery_obligation_verification.gap_units_invalid"
        elif failure == "obligation_identity":
            gap.update(reason="incompletely_assigned", obligation_id="unknown-obligation")
            expected_code = "delivery_obligation_verification.gap_id_invalid"
        else:
            expected_code = "delivery_obligation_verification.gap_duplicate"
    elif failure.startswith("constraint_"):
        field = "constraint_gaps"
        review["constraints_complete"] = False
        gap = {
            "reason": "omitted",
            "kind": "repository_ownership",
            "summary": "An existing owner remains responsible.",
            "affected_unit_ids": ["foundation"],
        }
        if failure == "constraint_owner":
            gap["affected_unit_ids"] = ["unknown-unit"]
            expected_code = "delivery_constraint_verification.gap_unit_unknown"
        elif failure == "constraint_identity":
            gap.update(reason="incompletely_assigned", constraint_id="unknown-constraint")
            expected_code = "delivery_constraint_verification.gap_constraint_unknown"
        else:
            expected_code = "delivery_constraint_verification.gap_duplicate"
    elif failure.startswith("context_"):
        field = "unit_context_gaps"
        review["unit_context_complete"] = False
        gap = {"unit_id": "foundation", "source_literals": ["Invented literal outside source."]}
        expected_code = "delivery_unit_context_verification.literal_not_source_line"
        if failure == "context_owner":
            gap["unit_id"] = "unknown-unit"
            expected_code = "delivery_unit_context_verification.unit_unknown"
    else:
        field, identity_key = (
            ("unit_contract_gaps", "unit_id")
            if failure == "unit_contract_identity"
            else ("source_accounting_gaps", "source_fragment_id")
        )
        gap = {identity_key: "unknown-identity", "summary": "Missing behavior."}
        expected_code = "delivery_authority.gaps_invalid"
    review[field] = [gap]
    if failure.endswith("duplicate"):
        review[field].append(deepcopy(gap))
    if extra_at != "none":
        target = {"top": review, "gap": gap, "obligation": review["obligations"][0]}[extra_at]
        target["detail"] = "PRIVATE unsupported explanation"
    agent, llm = _reviewer([review, clean])
    audit = []
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert not isinstance(caught.value, DeliveryAuthoringProtocolError)
    assert caught.value.code == expected_code
    assert llm.generate.call_count == len(audit) == 1
    assert json.loads(audit[0]["raw_output"]) == review
    assert audit[0]["parsed"]["error_code"] == expected_code
    assert public_delivery_preparation_failure(caught.value)[0] == expected_code


def test_invalid_later_gap_is_not_masked_by_protocol_error_in_an_earlier_gap(tmp_path):
    clean, arguments = _case(tmp_path)
    review = deepcopy(clean)
    review["obligations_complete"] = False
    first = {
        "reason": "omitted",
        "summary": "Additional required behavior.",
        "source_fragment_ids": review["obligations"][0]["source_fragment_ids"],
        "affected_unit_ids": ["foundation"],
        "detail": "Unsupported field",
    }
    second = {**first, "summary": "Another required outcome.", "source_fragment_ids": ["unknown-source"]}
    del second["detail"]
    review["obligation_gaps"] = [first, second]
    agent, llm = _reviewer([review, clean])
    audit = []
    with pytest.raises(DeliveryAuthoringParseError) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert caught.value.code == "delivery_obligation_verification.gap_source_refs_invalid"
    assert llm.generate.call_count == len(audit) == 1


@pytest.mark.parametrize("phase", ["authoring", "verification"])
@pytest.mark.parametrize("padding", [" ", "\t\r\n", "\u00a0"])
@pytest.mark.parametrize("envelope", ["plain", "trailing_comma", "partial_object"])
def test_normalized_prerequisite_stops_preempt_protocol_retry(tmp_path, phase, padding, envelope):
    from tests.test_delivery_preparation_agent import _author_delivery_plan

    review, arguments = _case(tmp_path)
    clean = json.loads(_authoring_output()) if phase == "authoring" else review
    malformed = deepcopy(clean)
    kind = padding + "stop_and_follow_up" + padding
    if phase == "authoring":
        malformed["constraints"] = [
            {
                "id": "external-input",
                "kind": kind,
                "summary": "Wait for the required external input.",
                "unit_ids": ["foundation"],
                "disposition": "preserved",
                "detail": "Unsupported field",
            }
        ]
    else:
        malformed["constraints_complete"] = False
        malformed["constraint_gaps"] = [
            {
                "reason": "omitted",
                "kind": kind,
                "summary": "Wait for the required external input.",
                "affected_unit_ids": ["foundation"],
                "detail": "Unsupported field",
            }
        ]
    output = json.dumps(malformed)
    if envelope == "trailing_comma":
        output = output[:-1] + ",}"
    elif envelope == "partial_object":
        output = (
            json.dumps({**clean, "detail": "Unsupported field"})
            + "\n"
            + json.dumps({"constraint_gaps": [{"kind": kind}]})
        )
    agent, llm = _reviewer([output, clean, review])
    audit = []
    with pytest.raises(DeliveryAuthoringProtocolError):
        if phase == "authoring":
            _author_delivery_plan(agent, tmp_path=tmp_path, audit_records=audit)
        else:
            agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert llm.generate.call_count == len(audit) == 1
    assert audit[0]["raw_output"] == output
    assert audit[0]["parsed"]["status"] == "failed"


@pytest.mark.parametrize("accounting", [None, {}, [None]])
@pytest.mark.parametrize("failure", ["constraint", "obligation", "unit_gap", "source_gap", "none"])
def test_accounting_format_cannot_mask_readable_authority_or_gap_failure(tmp_path, accounting, failure):
    from dataclasses import replace

    clean, arguments = _case(tmp_path)
    constraint = DeliveryAuthoringConstraintDraft(
        "storage-rule", "repository_ownership", "Retain the storage owner.", ["foundation"], "preserved"
    )
    arguments["constraints"] = [constraint]
    arguments["source_accounting"] = [
        replace(record, constraint_ids=[constraint.id]) for record in arguments["source_accounting"]
    ]
    clean["constraints"] = [constraint.to_plan_dict()]
    clean["source_accounting"] = [record.to_verification_dict() for record in arguments["source_accounting"]]
    malformed = deepcopy(clean)
    malformed["source_accounting"] = accounting
    if failure == "constraint":
        malformed["constraints"][0]["summary"] = "Changed governing rule."
        expected_code = "delivery_constraint_verification.constraints_mismatch"
    elif failure == "obligation":
        malformed["obligations"][0]["summary"] = "Changed required outcome."
        expected_code = "delivery_obligation_verification.obligations_mismatch"
    elif failure == "unit_gap":
        malformed["unit_contract_gaps"] = [{"unit_id": "unknown-unit", "summary": "Missing behavior."}]
        expected_code = "delivery_authority.gaps_invalid"
    elif failure == "source_gap":
        malformed["source_accounting_gaps"] = [
            {"source_fragment_id": "unknown-fragment", "summary": "Missing authority."}
        ]
        expected_code = "delivery_authority.gaps_invalid"
    agent, llm = _reviewer([malformed, clean])
    audit = []
    if failure == "none":
        result = agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
        assert result.obligations_complete
        assert llm.generate.call_count == len(audit) == 2
    else:
        with pytest.raises(DeliveryAuthoringParseError) as caught:
            agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
        assert not isinstance(caught.value, DeliveryAuthoringProtocolError)
        assert caught.value.code == expected_code
        assert audit[0]["parsed"]["error_code"] == expected_code
        assert llm.generate.call_count == len(audit) == 1
    assert json.loads(audit[0]["raw_output"]) == malformed


@pytest.mark.parametrize("malformed_first", [False, True])
@pytest.mark.parametrize(
    "change", ["checkpoint_ids", "rationale", "final_gate", "source_fragment_id", "duplicate", "none"]
)
def test_malformed_accounting_entries_cannot_mask_readable_record_failures(tmp_path, malformed_first, change):
    clean, arguments = _case(tmp_path)
    malformed = deepcopy(clean)
    record = malformed["source_accounting"][0]
    if change == "checkpoint_ids":
        record["checkpoint_ids"] = []
    elif change == "rationale":
        record["rationale"] = "Changed private authority rationale."
    elif change == "final_gate":
        record.update(checkpoint_ids=["storage"], final_gate=False)
    elif change == "source_fragment_id":
        record["source_fragment_id"] = "unknown-fragment"
    elif change == "duplicate":
        malformed["source_accounting"].append(deepcopy(record))
    malformed["source_accounting"].insert(0 if malformed_first else len(malformed["source_accounting"]), None)
    agent, llm = _reviewer([malformed, clean])
    audit = []
    if change == "none":
        result = agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
        assert result.source_accounting == arguments["source_accounting"]
        assert llm.generate.call_count == len(audit) == 2
    else:
        with pytest.raises(DeliveryAuthoringParseError) as caught:
            agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
        expected = (
            "source_accounting.fragment_invalid"
            if change in {"source_fragment_id", "duplicate"}
            else "source_accounting.verification_mismatch"
        )
        assert caught.value.code == expected
        assert not isinstance(caught.value, DeliveryAuthoringProtocolError)
        assert llm.generate.call_count == len(audit) == 1
        assert audit[0]["parsed"]["error_code"] == expected
    assert json.loads(audit[0]["raw_output"]) == malformed


def test_repeated_malformed_accounting_entries_never_publish_a_partial_result(tmp_path):
    clean, arguments = _case(tmp_path)
    malformed = deepcopy(clean)
    malformed["source_accounting"].append(None)
    agent, llm = _reviewer([malformed, malformed, clean])
    audit = []
    with pytest.raises(DeliveryAuthoringProtocolError) as caught:
        agent._verify_constraint_continuity(**arguments, audit_recorder=audit.append)
    assert caught.value.code == "source_accounting.invalid"
    assert llm.generate.call_count == len(audit) == 2
    assert [item["protocol_attempt"] for item in audit] == [1, 2]
    assert all(item["parsed"]["status"] == "failed" for item in audit)
