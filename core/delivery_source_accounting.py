"""Bounded, source-bound coverage records; semantic rationales stay in private audit."""

from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import re
from typing import Any

from core.delivery_obligations import MAX_DELIVERY_AUTHORITY_FRAGMENTS
from core.delivery_public_metadata import project_delivery_public_identity


@dataclass(frozen=True)
class DeliverySourceAccounting:
    source_fragment_id: str
    disposition: str
    obligation_ids: list[str] = field(default_factory=list)
    constraint_ids: list[str] = field(default_factory=list)
    rationale_sha256: str = ""
    rationale: str = ""
    # None is global authority; [] reserves the fragment for the final gate.
    checkpoint_ids: list[str] | None = None
    final_gate: bool = True

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        identity = project_delivery_public_identity if public else str
        data = {
            "source_fragment_id": self.source_fragment_id,
            "disposition": self.disposition,
            "obligation_ids": [identity(value) for value in self.obligation_ids],
            "constraint_ids": [identity(value) for value in self.constraint_ids],
            "rationale_sha256": self.rationale_sha256,
        }
        if self.checkpoint_ids is not None:
            data["checkpoint_ids"] = [identity(value) for value in self.checkpoint_ids]
        if not self.final_gate:
            data["final_gate"] = False
        return data

    def to_verification_dict(self) -> dict[str, Any]:
        data = self.to_dict()
        data.pop("rationale_sha256")  # Hashes are computed by Sikula, never by the model.
        data["rationale"] = self.rationale
        return data


class SourceAccountingError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = "source_accounting." + code
        super().__init__(message)


def parse_source_accounting(
    value: Any,
    *,
    fragment_ids: set[str],
    obligation_sources: dict[str, set[str]],
    constraint_ids: set[str],
    private_rationales: bool,
    allow_unresolved: bool = False,
) -> list[DeliverySourceAccounting]:
    """Validate exhaustive source and constraint coverage and obligation provenance."""
    if not isinstance(value, list) or len(value) > MAX_DELIVERY_AUTHORITY_FRAGMENTS:
        raise SourceAccountingError("invalid", "Source accounting must be a bounded list.")
    fields = {
        "source_fragment_id",
        "disposition",
        "obligation_ids",
        "constraint_ids",
        "rationale_sha256",
        "checkpoint_ids",
        "final_gate",
    }
    if private_rationales:
        fields.add("rationale")
    records: list[DeliverySourceAccounting] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - fields:
            raise SourceAccountingError("invalid", "Source accounting contains unsupported fields.")
        fragment = item.get("source_fragment_id")
        if not isinstance(fragment, str) or fragment not in fragment_ids or fragment in seen:
            raise SourceAccountingError("fragment_invalid", "Source fragments must be known and unique.")
        seen.add(fragment)
        disposition = item.get("disposition")
        if not isinstance(disposition, str) or disposition not in {"mapped", "context_only", "unresolved"}:
            raise SourceAccountingError("disposition_invalid", "Source accounting disposition is invalid.")
        if disposition == "unresolved" and not allow_unresolved:
            raise SourceAccountingError("unresolved", "Unresolved source accounting blocks publication.")
        obligations = _references(item.get("obligation_ids"), set(obligation_sources))
        constraints = _references(item.get("constraint_ids"), constraint_ids)
        if (disposition == "mapped") != bool(obligations or constraints):
            raise SourceAccountingError(
                "mapping_invalid", "Only mapped fragments may carry non-empty requirement references."
            )
        rationale = item.get("rationale", "")
        if private_rationales:
            if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 2000:
                raise SourceAccountingError(
                    "rationale_invalid", "Every source decision needs a bounded private rationale."
                )
            try:
                rationale_bytes = rationale.encode("utf-8")
            except UnicodeEncodeError:
                raise SourceAccountingError("rationale_invalid", "Source rationale must be valid UTF-8 text.") from None
            digest = "sha256:" + sha256(rationale_bytes).hexdigest()
            if "rationale_sha256" in item and item["rationale_sha256"] != digest:
                raise SourceAccountingError("rationale_invalid", "Source rationale fingerprint does not match.")
        else:
            digest = item.get("rationale_sha256")
            if not isinstance(digest, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise SourceAccountingError("rationale_invalid", "Source rationale fingerprint is required.")
        for obligation_id, sources in obligation_sources.items():
            if (obligation_id in obligations) != (fragment in sources):
                raise SourceAccountingError(
                    "mapping_invalid", "Source accounting and obligation provenance must agree."
                )
        checkpoint_ids = item.get("checkpoint_ids")
        if checkpoint_ids is not None:
            if (
                not isinstance(checkpoint_ids, list)
                or len(checkpoint_ids) > 256
                or any(
                    not isinstance(key, str)
                    or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", key)
                    or key.casefold() == "root"
                    for key in checkpoint_ids
                )
                or len(set(checkpoint_ids)) != len(checkpoint_ids)
            ):
                raise SourceAccountingError(
                    "checkpoint_invalid", "Checkpoint attribution requires unique bounded checkpoint IDs."
                )
            checkpoint_ids = list(checkpoint_ids)
        final_gate = item.get("final_gate", True)
        if type(final_gate) is not bool or (not final_gate and (checkpoint_ids is None or len(checkpoint_ids) != 1)):
            raise SourceAccountingError(
                "checkpoint_invalid", "Delegated final authority requires one owning checkpoint."
            )
        records.append(
            DeliverySourceAccounting(
                fragment, disposition, obligations, constraints, digest, rationale, checkpoint_ids, final_gate
            )
        )
    if seen != fragment_ids:
        raise SourceAccountingError(
            "incomplete", "Every authoritative source fragment needs an explicit accounting record."
        )
    if {constraint_id for record in records for constraint_id in record.constraint_ids} != constraint_ids:
        raise SourceAccountingError(
            "constraints_incomplete", "Every declared constraint must reference at least one source fragment."
        )
    return records


def _references(value: Any, allowed: set[str]) -> list[str]:
    if not isinstance(value, list) or len(value) > 256:
        raise SourceAccountingError("reference_invalid", "Source accounting references must be bounded lists.")
    if any(not isinstance(item, str) or item not in allowed for item in value) or len(value) != len(set(value)):
        raise SourceAccountingError("reference_invalid", "Source accounting references must be known and unique.")
    return list(value)
