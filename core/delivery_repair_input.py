"""Candidate-bound repair inputs; private control data, never reconstructed from audit."""

from __future__ import annotations

from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from core.delivery_plan import DeliveryPlan
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_verification import DeliveryVerificationIdentity
from core.delivery_verification_model import DeliveryVerificationRecord
from core.delivery_verification_review import DeliveryIntegrationAssessment, parse_delivery_integration_review


def repair_content_fingerprint(value: Any) -> str:
    return "sha256:" + sha256(json.dumps(value, sort_keys=True, ensure_ascii=True).encode("utf-8")).hexdigest()


def _input_path(directory: Path, gate_id: str, attempt: int) -> Path:
    return directory / f"repair-input-{gate_id.removeprefix('sha256:')}-{attempt}.json"


def store_repair_input(
    root: Path,
    directory: Path,
    identity: DeliveryVerificationIdentity,
    attempt: int,
    assessment: DeliveryIntegrationAssessment,
    project_config: dict[str, Any],
) -> str:
    payload = {
        "schema_version": 1,
        "identity": asdict(identity),
        "attempt": attempt,
        "assessment": assessment.to_dict(),
        "repair_policy_fingerprint": repair_content_fingerprint(project_config),
    }
    write_repair_state(root, _input_path(directory, identity.gate_id, attempt), payload)
    return repair_content_fingerprint(payload)


def load_repair_input(
    root: Path,
    directory: Path,
    record: DeliveryVerificationRecord,
    plan: DeliveryPlan,
    project_config: dict[str, Any],
) -> DeliveryIntegrationAssessment:
    payload = _load_bound_input(root, directory, record)
    if payload.get("repair_policy_fingerprint") != repair_content_fingerprint(project_config):
        raise ValueError("Current structured integration repair input is unavailable.")
    return _parse_assessment(payload, plan)


def repair_input_policy_changed(
    root: Path,
    directory: Path,
    record: DeliveryVerificationRecord,
    plan: DeliveryPlan,
    project_config: dict[str, Any],
) -> bool:
    """Recognize a policy change only in intact, candidate-bound control evidence."""
    payload = _load_bound_input(root, directory, record)
    _parse_assessment(payload, plan)
    return payload.get("repair_policy_fingerprint") != repair_content_fingerprint(project_config)


def _load_bound_input(root: Path, directory: Path, record: DeliveryVerificationRecord) -> dict[str, Any]:
    payload = read_repair_state(root, _input_path(directory, record.gate_id, record.attempt))
    if (
        payload is None
        or payload.get("schema_version") != 1
        or repair_content_fingerprint(payload) != record.repair_input_fingerprint
        or payload.get("attempt") != record.attempt
        or payload.get("identity")
        != {key: getattr(record, key) for key in DeliveryVerificationIdentity.__dataclass_fields__}
    ):
        raise ValueError("Current structured integration repair input is unavailable.")
    return payload


def _parse_assessment(payload: dict[str, Any], plan: DeliveryPlan) -> DeliveryIntegrationAssessment:
    return parse_delivery_integration_review(
        json.dumps(payload.get("assessment")),
        known_unit_ids={unit.id for unit in plan.units if not unit.superseded},
        known_obligation_ids={obligation.id for obligation in plan.obligations},
    )
