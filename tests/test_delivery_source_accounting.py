from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
from typing import Any

import pytest

from core.delivery_obligations import DeliveryAuthorityFragment, delivery_authority_fragments
from core.delivery_source_accounting import DeliverySourceAccounting, SourceAccountingError, parse_source_accounting


SOURCE = "\n# Export\n\n- Export permitted rows.\n- Exclude restricted rows.\n"


def _case() -> tuple[list[DeliveryAuthorityFragment], dict[str, set[str]], list[dict[str, Any]]]:
    fragments = delivery_authority_fragments(SOURCE)
    obligation_sources = {"export": {fragments[-2].id}, "filter": {fragments[-1].id}}
    records = []
    for fragment in fragments:
        owners = [key for key, sources in obligation_sources.items() if fragment.id in sources]
        records.append(
            {
                "source_fragment_id": fragment.id,
                "disposition": "mapped" if owners else "context_only",
                "obligation_ids": owners,
                "constraint_ids": [],
                "rationale": "PRIVATE interpretation of the source fragment.",
            }
        )
    return fragments, obligation_sources, records


def _parse(records: list[dict[str, Any]], *, private: bool = True) -> list[DeliverySourceAccounting]:
    fragments, obligations, _ = _case()
    return parse_source_accounting(
        records,
        fragment_ids={fragment.id for fragment in fragments},
        obligation_sources=obligations,
        constraint_ids=set(),
        private_rationales=private,
    )


def test_source_accounting_preserves_all_bytes_and_keeps_rationale_private() -> None:
    fragments, _, records = _case()
    assert "".join(fragment.text for fragment in fragments) == SOURCE
    parsed = _parse(records)
    persisted = [record.to_dict() for record in parsed]
    assert "PRIVATE" not in str(persisted)
    assert all(record["rationale_sha256"].startswith("sha256:") for record in persisted)
    restored = _parse(persisted, private=False)
    assert [record.to_dict() for record in restored] == persisted
    assert all(not record.rationale for record in restored)


@pytest.mark.parametrize("rationale", ["Private rationale: \ud800", "Private rationale: \udfff"])
def test_source_accounting_rejects_non_utf8_rationale(rationale: str) -> None:
    _, _, records = _case()
    records[0]["rationale"] = rationale

    with pytest.raises(SourceAccountingError) as error:
        _parse(records)

    assert error.value.code == "source_accounting.rationale_invalid"
    assert "Private rationale" not in str(error.value)


def test_source_accounting_hashes_valid_unicode_rationale() -> None:
    _, _, records = _case()
    rationale = "Soukromé odůvodnění \U0001f512."
    records[0]["rationale"] = rationale

    parsed = _parse(records)

    assert parsed[0].rationale == rationale
    assert parsed[0].rationale_sha256 == "sha256:" + sha256(rationale.encode("utf-8")).hexdigest()


@pytest.mark.parametrize(
    "mutation",
    [
        "missing",
        "duplicate",
        "unknown_fragment",
        "unknown_obligation",
        "wrong_mapping",
        "unjustified_context",
        "unresolved",
        "wrong_digest",
    ],
)
def test_source_accounting_rejects_incomplete_or_inconsistent_coverage(mutation: str) -> None:
    _, _, initial = _case()
    records = deepcopy(initial)
    if mutation == "missing":
        records.pop()
    elif mutation == "duplicate":
        records.append(deepcopy(records[-1]))
    elif mutation == "unknown_fragment":
        records[-1]["source_fragment_id"] = "source-stale"
    elif mutation == "unknown_obligation":
        records[-1]["obligation_ids"] = ["not-an-outcome"]
    elif mutation == "wrong_mapping":
        records[-1].update(disposition="context_only", obligation_ids=[])
    elif mutation == "unjustified_context":
        records[0]["rationale"] = ""
    elif mutation == "unresolved":
        records[0]["disposition"] = "unresolved"
    else:
        records[0]["rationale_sha256"] = "sha256:" + "0" * 64
    with pytest.raises(SourceAccountingError):
        _parse(records)


def test_one_fragment_can_map_to_multiple_outcomes_and_one_outcome_to_multiple_fragments() -> None:
    fragments, _, records = _case()
    for record in records[-2:]:
        record["obligation_ids"] = ["export", "filter"]
    sources = {key: {fragment.id for fragment in fragments[-2:]} for key in ("export", "filter")}
    result = parse_source_accounting(
        records,
        fragment_ids={fragment.id for fragment in fragments},
        obligation_sources=sources,
        constraint_ids=set(),
        private_rationales=True,
    )
    assert result[-1].obligation_ids == ["export", "filter"]


@pytest.mark.parametrize("private", [True, False])
@pytest.mark.parametrize("coverage", ["none", "partial", "empty-source"])
def test_every_constraint_requires_source_accounting(private: bool, coverage: str) -> None:
    fragments, obligations, records = _case()
    if coverage == "partial":
        records[-1]["constraint_ids"] = ["ownership"]
    if coverage == "empty-source":
        fragments, obligations, records = [], {}, []
    if not private:
        for record in records:
            rationale = record.pop("rationale")
            record["rationale_sha256"] = "sha256:" + sha256(rationale.encode("utf-8")).hexdigest()

    with pytest.raises(SourceAccountingError) as error:
        parse_source_accounting(
            records,
            fragment_ids={fragment.id for fragment in fragments},
            obligation_sources=obligations,
            constraint_ids={"ownership", "security"},
            private_rationales=private,
        )

    assert error.value.code == "source_accounting.constraints_incomplete"


def test_constraints_can_share_fragments_without_obligation_owners() -> None:
    fragments, _, records = _case()
    for record in records:
        record["obligation_ids"] = []
    records[-2]["constraint_ids"] = ["ownership", "security"]
    records[-1]["constraint_ids"] = ["security"]

    parsed = parse_source_accounting(
        records,
        fragment_ids={fragment.id for fragment in fragments},
        obligation_sources={},
        constraint_ids={"ownership", "security"},
        private_rationales=True,
    )

    assert parsed[-2].constraint_ids == ["ownership", "security"]
    assert parsed[-1].constraint_ids == ["security"]
