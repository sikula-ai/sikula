"""Derive bounded review routes without reserving work or granting authority."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, TYPE_CHECKING

from core.delivery_composition_scope import DeliveryCompositionScope
from core.delivery_verification_scope import DeliveryVerificationScope

if TYPE_CHECKING:
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent

# Covers the fixed parser-error correction suffix, never previous provider output.
REVIEW_CORRECTION_HEADROOM = 4096


@dataclass(frozen=True)
class DeliveryReviewRoute:
    full_bytes: int
    composition_bytes: int | None
    full_available: bool
    composition_available: bool

    @property
    def ready(self) -> bool:
        return self.full_available or self.composition_available

    @property
    def packet_bytes(self) -> int:
        # A fitting fallback can still be invoked after composition uncertainty.
        # When it is unavailable, report the supported local route instead.
        return self.full_bytes if self.full_available or not self.composition_available else self.composition_bytes


def preview_composition_context(scope: DeliveryCompositionScope | None, role: str) -> dict[str, Any] | None:
    """Size checked local authority before execution evidence exists.

    This is an exact-tree sizing preview only. Candidate preflight must build the
    real packet (or a validated refresh preview) from immutable child evidence.
    """
    from core.delivery_composition import (
        COMPOSITION_POLICY,
        MAX_COMPOSITION_CHILDREN,
        MAX_COMPOSITION_DIRECT_UNITS,
        MAX_COMPOSITION_PACKET_BYTES,
    )
    import json

    context = scope.retained_authority() if scope else None
    if context is None or not 0 < len(scope.children) <= MAX_COMPOSITION_CHILDREN:
        return None
    if any(not child.obligation_ids for child in scope.children):
        return None
    if len(context["units"]) > MAX_COMPOSITION_DIRECT_UNITS:
        return None
    children = []
    for child in context["final_authority"]["children"]:
        children.append(
            {
                **child,
                "origin_commit": "0" * 64,
                "origin_tree": "0" * 64,
                "evidence_fingerprint": "sha256:" + "0" * 64,
                "historical_outcome": "satisfied",
                "security_outcome": "approved",
                "exact_tree": True,
                "delta": "",
            }
        )
    context["final_authority"] = {"policy": context["final_authority"]["policy"]}
    context["checkpoint_composition"] = {
        "policy": COMPOSITION_POLICY,
        "children": children,
        "compact": True,
        "review_kind": role,
    }
    if not scope.include_final_checks:
        context["verification_node"] = {"id": scope.parent.node_id}
    return context if len(json.dumps(context).encode()) <= MAX_COMPOSITION_PACKET_BYTES else None


def review_route(
    scope: DeliveryVerificationScope,
    cfg: dict[str, Any],
    cwd: Path,
    source: str,
    role: str,
    *,
    context: dict[str, Any] | None = None,
    validation: dict[str, Any] | None = None,
    include_final_checks: bool | None = None,
    reviewer: DeliveryIntegrationReviewAgent | None = None,
) -> DeliveryReviewRoute:
    """Size complete protocols for one role, including rules and correction."""
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
    from core.delivery_verification import delivery_verification_prompt_is_bounded
    from core.delivery_verification_validation import preview_delivery_validation_summary

    reviewer = reviewer or DeliveryIntegrationReviewAgent(None, cfg)
    args = dict(
        cwd=cwd,
        review_kind=role,
        source_task=source,
        validation_summary=validation
        if validation is not None
        else preview_delivery_validation_summary(
            cfg,
            project_root=cwd,
            include_final_checks=scope.node_id == "root" if include_final_checks is None else include_final_checks,
        ),
        candidate_commit="0" * 64,
        candidate_tree="0" * 64,
        known_obligation_ids=set(scope.obligation_ids) if role == "semantic" else set(),
    )
    full = reviewer._prompt(plan_context=scope.plan_context(), **args) + " " * REVIEW_CORRECTION_HEADROOM
    compact = (
        reviewer._prompt(plan_context=context, **args) + " " * REVIEW_CORRECTION_HEADROOM
        if context is not None
        else None
    )
    full_bytes = len(full.encode("utf-8"))
    compact_bytes = len(compact.encode("utf-8")) if compact is not None else None
    full_available = delivery_verification_prompt_is_bounded(full)
    return DeliveryReviewRoute(
        full_bytes,
        compact_bytes,
        full_available,
        compact is not None
        and delivery_verification_prompt_is_bounded(compact)
        and (full_available or bool(context.get("final_authority")))
        and (not full_available or compact_bytes < full_bytes),
    )
