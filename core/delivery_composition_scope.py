"""Captured node composition inputs; hierarchy policy lives in the adapter.

These private objects do not grant authority. Retained packets must already have
been restored from independently validated receipts by the plan loader.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import TYPE_CHECKING, Any

from core.delivery_verification_model import DeliveryVerificationRecord, parse_delivery_verification_record
from core.delivery_verification_scope import DeliveryVerificationScope

if TYPE_CHECKING:
    from core.delivery_plan import DeliveryPlan
    from core.delivery_progress import DeliveryStatusResult
    from core.delivery_verification import DeliveryVerificationIdentity


@dataclass(frozen=True)
class DeliveryCompositionScope:
    """One parent and its explicit immediate children, never a delivery traversal."""

    parent: DeliveryVerificationScope
    children: tuple[DeliveryVerificationScope, ...]
    retained_authority_json: str | None = field(default=None, repr=False)
    include_final_checks: bool = False

    def __post_init__(self) -> None:
        ids = [child.node_id for child in self.children]
        if len(ids) != len(set(ids)) or self.parent.node_id in ids:
            raise ValueError("Invalid immediate composition children.")
        for child in self.children:
            if (
                child.plan_id != self.parent.plan_id
                or child.source_task != self.parent.source_task
                or child.security_required != self.parent.security_required
                or not set(child.unit_ids) <= set(self.parent.unit_ids)
                or not set(child.obligation_ids) <= set(self.parent.obligation_ids)
                or not set(child.constraint_ids) <= set(self.parent.constraint_ids)
                or not set(child.source_fragment_ids) <= set(self.parent.source_fragment_ids)
            ):
                raise ValueError("Composition child is outside captured parent authority.")
        if self.retained_authority_json is not None:
            authority = self.retained_authority()
            interfaces = authority["final_authority"]["children"]
            if len(interfaces) != len(ids) or {item["id"] for item in interfaces} != set(ids):
                raise ValueError("Retained authority does not bind the immediate children.")
            covered: set[str] = set()
            inherited: set[str] = set()
            constraints = {item["id"] for item in authority["constraints"]}
            fragments = {item["id"] for item in authority["authority_packet"]["fragments"]}
            for child in self.children:
                if covered.intersection(child.unit_ids) or inherited.intersection(child.obligation_ids):
                    raise ValueError("Overlapping delegated composition authority.")
                covered.update(child.unit_ids)
                inherited.update(child.obligation_ids)
                constraints.update(child.constraint_ids)
                fragments.update(child.source_fragment_ids)
                interface = next(item for item in interfaces if item["id"] == child.node_id)
                if interface["obligation_count"] != len(child.obligation_ids):
                    raise ValueError("Composition child obligation coverage changed.")
            if (
                {item["id"] for item in authority["units"]} != set(self.parent.unit_ids) - covered
                or {item["id"] for item in authority["obligations"]} != set(self.parent.obligation_ids) - inherited
                or constraints != set(self.parent.constraint_ids)
                or set(self.parent.source_fragment_ids) != fragments
            ):
                raise ValueError("Composition must retain complete parent authority.")

    def retained_authority(self) -> dict[str, Any] | None:
        return json.loads(self.retained_authority_json) if self.retained_authority_json is not None else None

    def child(self, node_id: str) -> DeliveryVerificationScope:
        for child in self.children:
            if child.node_id == node_id:
                return child
        raise ValueError("Candidate review is outside the captured immediate children.")


@dataclass(frozen=True)
class DeliveryCompositionAttempt:
    """Accepted references for one captured parent attempt, separate from admission.

    Serialize records on capture so mutations of nested control dictionaries cannot
    silently alter evidence selection while a provider is running.
    """

    scope: DeliveryCompositionScope
    _historical_json: str = field(repr=False)
    _parent_json: str | None = field(default=None, repr=False)

    @classmethod
    def capture(
        cls,
        scope: DeliveryCompositionScope,
        historical: dict[str, DeliveryVerificationRecord],
        parent: DeliveryVerificationRecord | None,
    ) -> DeliveryCompositionAttempt:
        records = {}
        for child in scope.children:
            origin = historical.get(child.node_id)
            if origin is None:
                raise ValueError("Original checkpoint admission is unavailable.")
            records[child.node_id] = origin.to_dict()
        if parent and set((parent.reverification or {}).get("children", {})) - set(records):
            raise ValueError("Accepted candidate evidence is outside the composition scope.")
        return cls(scope, json.dumps(records), json.dumps(parent.to_dict()) if parent else None)

    def origin(self, node_id: str, *, digest: str | None = None) -> DeliveryVerificationRecord:
        self.scope.child(node_id)
        historical = parse_delivery_verification_record(json.loads(self._historical_json)[node_id])
        parent = parse_delivery_verification_record(json.loads(self._parent_json)) if self._parent_json else None
        from core.delivery_reverification import accepted_composition_origin

        return accepted_composition_origin(historical, parent, node_id, digest=digest)

    def assert_record(self, record: DeliveryVerificationRecord) -> None:
        if self._parent_json is None or json.loads(self._parent_json) != record.to_dict():
            raise ValueError("Composition attempt changed.")

    def assert_candidate(self, identity: DeliveryVerificationIdentity) -> None:
        if self._parent_json is not None:
            parent = json.loads(self._parent_json)
            if any(parent.get(key) != value for key, value in vars(identity).items()):
                raise ValueError("Composition candidate changed.")


def delivery_composition_scope(
    plan: DeliveryPlan, parent: DeliveryVerificationScope | None = None
) -> DeliveryCompositionScope | None:
    """Select declared immediate children; graph policy stays out of the common cycle."""
    parent = parent or DeliveryVerificationScope.from_plan(plan)
    from core.delivery_checkpoint_model import checkpoint_children

    children = checkpoint_children(plan.checkpoints, parent.node_id)
    if not children:
        return None
    try:
        return DeliveryCompositionScope(
            parent,
            tuple(DeliveryVerificationScope.from_plan(plan, child.id) for child in children),
            plan.verified_final_gate_authority
            if parent.node_id == "root"
            else plan.verified_composition_authority.get(parent.node_id),
            include_final_checks=parent.node_id == "root",
        )
    except ValueError:
        # Losing one child's attribution receipt restores its complete source
        # packet, which may exceed the parent's independently scoped authority.
        # Such packets cannot compose; use each node's preflighted full review.
        return None


def capture_composition_attempt(
    status: DeliveryStatusResult,
    parent: DeliveryVerificationRecord | None,
    scope: DeliveryCompositionScope | None = None,
) -> DeliveryCompositionAttempt:
    scope = scope or delivery_composition_scope(
        status.plan, DeliveryVerificationScope.from_plan(status.plan, status.verification_node)
    )
    if scope is None:
        raise ValueError("Composition scope is unavailable.")
    return DeliveryCompositionAttempt.capture(scope, status.checkpoint_verifications, parent)
