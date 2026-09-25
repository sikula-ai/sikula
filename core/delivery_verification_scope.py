"""Private, immutable authority for a delivery verification node.

Only the whole-plan root is supported today. Declaring its authority does not
require completed units and does not itself authorize execution or finalization.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any

from core.delivery_plan import DeliveryPlan, DeliveryPlanSourceTask, DeliveryVerificationPolicy


_SECURITY_SENSITIVE_RISK_TAGS = frozenset(
    {"auth_permissions", "execution_boundary", "external_execution_boundary", "privacy", "security_boundary"}
)


@dataclass(frozen=True)
class DeliveryVerificationScope:
    """Declared root coverage, detached from mutable lists in the parsed plan.

    ``(plan_id, node_id)`` names the logical node, not a passing verification.
    The root retains the full source authority, including unmapped/context-only
    fragments; fragment IDs describe accounting, never permission to omit text.
    Context is private prompt data and must not enter public projections.
    """

    plan_id: str
    source_task: DeliveryPlanSourceTask | None = field(repr=False)
    unit_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    constraint_ids: tuple[str, ...]
    source_fragment_ids: tuple[str, ...]
    security_required: bool
    policy: DeliveryVerificationPolicy | None
    _context_json: str = field(repr=False)
    node_id: str = field(default="root", init=False)

    @classmethod
    def from_plan(cls, plan: DeliveryPlan) -> DeliveryVerificationScope:
        """Capture the root of a parsed plan; plan validation remains mandatory."""
        context = {
            "plan_id": plan.plan_id,
            "title": plan.title,
            "units": [unit.to_authoring_dict() for unit in plan.units if not unit.superseded],
            "constraints": [constraint.to_dict() for constraint in plan.constraints],
            "obligations": [obligation.to_context_dict() for obligation in plan.obligations],
            "source_accounting": [record.to_dict() for record in plan.source_accounting]
            if plan.source_accounting is not None
            else None,
            "components": [component.to_dict() for component in plan.components],
        }
        fragment_ids = [record.source_fragment_id for record in plan.source_accounting or ()]
        fragment_ids.extend(ref for obligation in plan.obligations for ref in obligation.source_fragment_ids)
        return cls(
            plan_id=plan.plan_id,
            source_task=plan.source_task,
            unit_ids=tuple(unit["id"] for unit in context["units"]),
            obligation_ids=tuple(obligation.id for obligation in plan.obligations),
            constraint_ids=tuple(constraint.id for constraint in plan.constraints),
            source_fragment_ids=tuple(dict.fromkeys(fragment_ids)),
            # Superseding a sensitive unit must not remove required security review.
            security_required=any(constraint.kind == "security_boundary" for constraint in plan.constraints)
            or any(tag in _SECURITY_SENSITIVE_RISK_TAGS for unit in plan.units for tag in unit.risk_tags),
            policy=plan.verification,
            _context_json=json.dumps(context, ensure_ascii=True),
        )

    def plan_context(self) -> dict[str, Any]:
        """Return a fresh prompt context; callers cannot mutate captured authority."""
        return json.loads(self._context_json)


@dataclass(frozen=True)
class DeliveryVerificationCompletedUnit:
    """Execution-time binding of a completed unit, independent of an attempt."""

    unit_id: str
    commit: str | None
    handoff_fingerprint: str | None

    def to_dict(self) -> dict[str, str | None]:
        return {
            "unit_id": self.unit_id,
            "commit": self.commit,
            "handoff_fingerprint": self.handoff_fingerprint,
        }
