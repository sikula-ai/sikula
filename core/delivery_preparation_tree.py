"""Exact local preparation proposals and deterministic assembly of checked trees."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from copy import deepcopy
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any

from core.delivery_authoring import (
    DeliveryAuthoringDraft,
    DeliveryAuthoringParseError,
    DeliveryAuthoringUnitDraft,
    DeliveryAuthoringConstraintDraft,
    DeliveryAuthoringObligationDraft,
    DeliveryConstraintVerification,
    _parse_units,
    _parse_constraints,
    _parse_obligations,
    _validate_dependencies,
    _bounded_authoring_string,
    parse_delivery_authoring_output,
    delivery_output_has_prerequisite_stop,
)
from core.delivery_authority import authority_digest, preparation_authority_fingerprint, _enclosing_fragments
from core.delivery_plan import DeliveryPlanSourceTask, MAX_DELIVERY_UNIT_ID_LENGTH
from core.delivery_checkpoint_model import _ID as _CHECKPOINT_ID
from core.delivery_obligations import delivery_authority_fragment_map
from core.delivery_public_metadata import contains_delivery_source_excerpt

MAX_PREPARATION_CHILDREN = 8
MAX_PREPARATION_LOCAL_UNITS = 8
MAX_PREPARATION_DEPTH = 4
MAX_PREPARATION_NODES = 64
MAX_PREPARATION_PACKET_BYTES = 512 * 1024
MAX_PREPARATION_RESPONSE_BYTES = 128 * 1024
MAX_PREPARATION_REVIEW_FEEDBACK_BYTES = 4096
_ID = re.compile(r"[a-z][a-z0-9_-]*\Z")


def failure(code: str) -> DeliveryAuthoringParseError:
    error = DeliveryAuthoringParseError(
        "delivery_prepare." + code, "Hierarchical preparation cannot continue; inspect its private audit."
    )
    error.phase = "delivery_prepare_hierarchy"
    return error


def object_output(output: str) -> dict[str, Any]:
    from core.delivery_authoring import _object_pairs_without_duplicates

    try:
        size = len(output.encode("utf-8"))
    except (AttributeError, UnicodeError):
        raise failure("hierarchy_output_invalid") from None
    if size > MAX_PREPARATION_RESPONSE_BYTES:
        raise failure("hierarchy_required")
    try:
        value = json.loads(output, object_pairs_hook=_object_pairs_without_duplicates)
        if not isinstance(value, dict):
            raise ValueError()
        # JSON escapes can encode lone surrogates even when the response itself
        # is UTF-8. Reject them through the audited parser path before hashing
        # or measuring any decoded authority fields.
        json.dumps(value, ensure_ascii=False).encode("utf-8")
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise failure("hierarchy_output_invalid") from None


def refs(value: Any, allowed: set[str]) -> list[str]:
    if (
        not isinstance(value, list)
        or any(not isinstance(v, str) or v not in allowed for v in value)
        or len(set(value)) != len(value)
    ):
        raise failure("hierarchy_authority_changed")
    return value


class PreparationProtocolError(DeliveryAuthoringParseError):
    """A local review format error with no readable contrary authority or gap."""

    def __init__(self) -> None:
        super().__init__("delivery_prepare.hierarchy_output_invalid", "Correct the declared local review JSON format.")
        self.phase = "delivery_prepare_hierarchy"


class PreparationDraftCorrection(DeliveryAuthoringParseError):
    """An unapproved local proposal needs correction under unchanged source authority."""

    def __init__(self, node: PreparationNode, corrections: list[str]) -> None:
        super().__init__(
            "delivery_prepare.hierarchy_draft_invalid",
            "The local proposal needs contract, delegation, accounting or public summary corrections.",
        )
        self.phase = "delivery_prepare_hierarchy"
        self.node = node
        # Private feedback carries only local references and deterministic rules.
        # Keep all authoritative input in the next packet, not in error history.
        self.feedback = (
            "\n".join(dict.fromkeys(corrections))
            .encode("utf-8")[:MAX_PREPARATION_REVIEW_FEEDBACK_BYTES]
            .decode("utf-8", errors="ignore")
        )


def parse_local_review(output: str, fingerprint: str) -> str:
    # Inspect the entire envelope, not just one extracted object. A readable
    # rejection/mismatch must not disappear behind an unrelated formatting error.
    if delivery_output_has_prerequisite_stop(output):
        raise failure("prerequisite_stop")
    gap = False
    previous: list[str] = []
    for match in re.finditer(r'"(?:\\.|[^"\\])*"|[^\s]', output):
        token = match.group()
        if (
            len(previous) == 3
            and previous[0] in {"{", ","}
            and previous[1].startswith('"')
            and previous[2] == ":"
            and token.startswith('"')
        ):
            try:
                key, value = json.loads(previous[1]), json.loads(token)
            except ValueError:
                pass
            else:
                if key == "input_fingerprint" and value != fingerprint:
                    raise failure("hierarchy_authority_changed")
                if key == "kind" and value.strip() != "approved":
                    gap = True
                if key == "feedback" and value.strip():
                    gap = True
        previous = [*previous[-2:], token]
    try:
        data = object_output(output)
    except DeliveryAuthoringParseError:
        if gap:
            raise
        raise PreparationProtocolError() from None
    if data.get("input_fingerprint", fingerprint) != fingerprint:
        raise failure("hierarchy_authority_changed")
    valid = (
        set(data) == {"kind", "input_fingerprint", "feedback"}
        and data.get("kind") in ("approved", "revise")
        and isinstance(data.get("feedback"), str)
        and len(data["feedback"].encode("utf-8")) <= MAX_PREPARATION_REVIEW_FEEDBACK_BYTES
        and bool(data["feedback"].strip()) == (data["kind"] == "revise")
    )
    if not valid:
        if gap:
            raise failure("hierarchy_output_invalid")
        raise PreparationProtocolError()
    return "" if data["kind"] == "approved" else data["feedback"]


@dataclass(frozen=True)
class PreparationNode:
    key: str
    proposal: dict[str, Any]
    units: list[DeliveryAuthoringUnitDraft]
    obligations: list[DeliveryAuthoringObligationDraft]
    constraints: list[DeliveryAuthoringConstraintDraft]
    fragments: tuple[str, ...]


def remap_source_references(proposal: dict[str, Any], references: dict[str, str]) -> dict[str, Any]:
    """Translate only declared reference fields; never guess or rewrite source text."""
    result = deepcopy(proposal)

    def reference(value: Any) -> Any:
        if isinstance(value, str):
            if value not in references:
                raise failure("hierarchy_authority_changed")
            return references[value]
        return value  # Structural validation remains the parser's responsibility.

    for section in ("children", "obligations", "source_accounting"):
        records = result.get(section)
        if not isinstance(records, list):
            continue
        for record in records:
            if not isinstance(record, dict):
                continue
            if section == "source_accounting":
                if "source_fragment_id" in record:
                    record["source_fragment_id"] = reference(record["source_fragment_id"])
            elif isinstance(record.get("source_fragment_ids"), list):
                record["source_fragment_ids"] = [reference(value) for value in record["source_fragment_ids"]]
    return result


def parse_node(
    output: str,
    *,
    key: str,
    source: str,
    fragment_ids: tuple[str, ...],
    root: Path,
    source_references: dict[str, str] | None = None,
    prerequisite_ids: frozenset[str] = frozenset(),
) -> PreparationNode:
    if delivery_output_has_prerequisite_stop(output):
        raise failure("prerequisite_stop")
    data = object_output(output)
    if source_references is not None:
        data = remap_source_references(data, source_references)
    corrections: list[str] = []
    if set(data) != {"title", "children", "units", "constraints", "obligations", "source_accounting"}:
        raise failure("hierarchy_output_invalid")
    if not isinstance(data["title"], str) or not data["title"].strip() or len(data["title"]) > 200:
        raise failure("hierarchy_output_invalid")
    data["title"] = _bounded_authoring_string(data, "title", "title")
    children, raw_units = data["children"], data["units"]
    if (
        not isinstance(children, list)
        or len(children) > MAX_PREPARATION_CHILDREN
        or not isinstance(raw_units, list)
        or len(raw_units) > MAX_PREPARATION_LOCAL_UNITS
        or not raw_units
        and len(children) < 2
    ):
        raise failure("hierarchy_required")
    local_ids: set[str] = set()
    prefix = "" if key == "root" else key + "."
    for item in [*children, *raw_units]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("id"), str)
            or not _ID.fullmatch(item["id"])
            or not _CHECKPOINT_ID.fullmatch(prefix + item["id"])
            or item["id"] in local_ids
        ):
            raise failure("hierarchy_output_invalid")
        local_ids.add(item["id"])
    # Parent-declared prerequisites are attached to every descendant unit during
    # assembly. Accept exact redundant mentions, but do not turn external inputs
    # into local owners or repeat them as unresolved local dependency edges.
    for item in [*children, *raw_units]:
        dependencies = refs(item.get("depends_on", []), local_ids | prerequisite_ids)
        item["depends_on"] = [ref for ref in dependencies if ref in local_ids]
    for child in children:
        if child["id"] == "root":
            raise failure("hierarchy_output_invalid")
        if set(child) != {"id", "integration_context", "source_fragment_ids", "depends_on"}:
            raise failure("hierarchy_output_invalid")
        context = child["integration_context"]
        if not isinstance(context, str) or not context.strip() or len(context.encode("utf-8")) > 4096:
            raise failure("hierarchy_required")
        selected = refs(child["source_fragment_ids"], set(fragment_ids))
        if not selected:
            raise failure("hierarchy_authority_changed")
        missing = _enclosing_fragments(source, set(selected)) - set(selected)
        if missing:
            corrections.append(
                f"Child {child['id']} must also receive enclosing source fragments: {', '.join(sorted(missing))}. "
                "Update its source_fragment_ids and the matching accounting child_ids/dispositions."
            )
        refs(child["depends_on"], local_ids)
    # Existing unit parsing owns Markdown, asset, scope and budget validation.
    markdown_corrections: list[tuple[str, DeliveryAuthoringParseError]] = []
    unit_ids = {item["id"] for item in raw_units}
    units = (
        _parse_units(
            [
                {**item, "depends_on": [v for v in refs(item.get("depends_on"), local_ids) if v in unit_ids]}
                for item in raw_units
            ],
            project_root=root,
            allow_asset_paths=True,
            markdown_corrections=markdown_corrections,
        )
        if raw_units
        else []
    )
    units = [replace(unit, depends_on=raw["depends_on"]) for unit, raw in zip(units, raw_units)]
    for unit_id, error in markdown_corrections:
        corrections.append(
            f"Unit {unit_id}: {error.message} Use explicit ## section headings with nonempty content. "
            "Put the configured command bullets directly under ## Verification; "
            "a standalone colon-ended line starts another section."
        )
    graph_units = [*units, *(DeliveryAuthoringUnitDraft(c["id"], c["id"], c["depends_on"], "") for c in children)]
    _validate_dependencies(graph_units)
    # The current executable checkpoint format requires a downstream consumer.
    # Check this at the local declaration, before authoring its descendants.
    # Nested groups inherit consumers of their enclosing group after dependency
    # expansion; they need not invent additional local integration units.
    if key == "root" and any(not any(child["id"] in u.depends_on for u in graph_units) for child in children):
        raise failure("hierarchy_authority_changed")
    # Validate identities, provenance and ownership across all records before
    # deferring paraphrasing errors. A copied summary must not hide an unknown
    # source/owner reference that is ineligible for correction.
    constraints = _parse_constraints(data, unit_ids=local_ids, source_task_description=None)
    obligations = _parse_obligations(
        data["obligations"],
        unit_ids=local_ids,
        source_task_description=None,
        required=not children,
        known_source_fragment_ids=set(fragment_ids),
    )
    for kind, item in [("constraints", c) for c in constraints] + [("obligations", o) for o in obligations]:
        # Requirements do not name files or checkpoints. Apply their existing
        # published ID bound after prefixing, not the short child-path limit.
        # Existing requirement parsing already checks legal ID characters.
        # Only reserve the namespace separator used by hierarchical assembly.
        if "." in item.id or len(prefix + item.id) > MAX_DELIVERY_UNIT_ID_LENGTH:
            raise failure("hierarchy_output_invalid")
        if contains_delivery_source_excerpt(item.summary, source):
            corrections.append(
                f"Rewrite {kind}[id={item.id}].summary: it copies source text and cannot remain unchanged. "
                "Use different wording for the same outcome/rule; adding a prefix to a copied sentence "
                "is not a paraphrase. Retain exact required literals in private contracts and interfaces."
            )
    records = data["source_accounting"]
    if not isinstance(records, list) or len(records) != len(fragment_ids):
        raise failure("hierarchy_authority_changed")
    seen = set()
    rules = {c.id: c for c in constraints}
    outcomes = {o.id: o for o in obligations}
    mapped_rules = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {
            "source_fragment_id",
            "disposition",
            "obligation_ids",
            "constraint_ids",
            "child_ids",
            "rationale",
        }:
            raise failure("hierarchy_output_invalid")
        fragment = record["source_fragment_id"]
        if not isinstance(fragment, str) or fragment not in fragment_ids or fragment in seen:
            raise failure("hierarchy_authority_changed")
        seen.add(fragment)
        own = refs(record["obligation_ids"], set(outcomes))
        hard = refs(record["constraint_ids"], set(rules))
        delegated = refs(record["child_ids"], {c["id"] for c in children})
        expected_children = [c["id"] for c in children if fragment in c["source_fragment_ids"]]
        if set(delegated) != set(expected_children):
            corrections.append(
                f"Accounting for {fragment}: child_ids is {json.dumps(delegated)}, but the current child "
                f"source_fragment_ids require {json.dumps(expected_children)}. Make these fields agree "
                "without removing authority a child needs."
            )
        expected_outcomes = [o.id for o in obligations if fragment in o.source_fragment_ids]
        if set(own) != set(expected_outcomes):
            corrections.append(
                f"Accounting for {fragment}: obligation_ids is {json.dumps(own)}, but the current obligation "
                f"source_fragment_ids require {json.dumps(expected_outcomes)}. Make these fields agree "
                "in both directions while preserving each outcome's source authority."
            )
        disposition = "mapped" if own or hard else "delegated" if delegated else "context_only"
        if record["disposition"] != disposition:
            corrections.append(
                f"Accounting for {fragment}: disposition must be {disposition} for its current references "
                "(mapped for local obligation/constraint refs, otherwise delegated for child_ids, "
                "otherwise context_only)."
            )
        rationale = record["rationale"]
        if not isinstance(rationale, str) or not rationale.strip() or len(rationale.encode("utf-8")) > 2000:
            raise failure("hierarchy_output_invalid")
        mapped_rules.update(hard)
        for child in children:
            if any(child["id"] in outcomes[o].unit_ids for o in own) or any(
                child["id"] in rules[c].unit_ids for c in hard
            ):
                if fragment not in child["source_fragment_ids"]:
                    corrections.append(
                        f"Child {child['id']} owns an outcome/rule mapped to {fragment}; delegate that fragment "
                        "and its enclosing context to the child and align accounting."
                    )
    if mapped_rules != set(rules):
        corrections.append("Every local constraint must be mapped from at least one source_accounting record.")
    from core.delivery_prepare_writer import _canonicalize_markdown_headings
    from core.delivery_asset_assignment import DeliveryAssetAssignmentUnit, render_delivery_asset_assignments

    rendered = render_delivery_asset_assignments(
        source,
        [
            DeliveryAssetAssignmentUnit(u.id, _canonicalize_markdown_headings(u.task_markdown), u.asset_paths)
            for u in units
        ],
        source_task_path=None,
        project_root=root,
        project_config=None,
        # Other groups may own the remaining assets. Publication still requires
        # complete assignment across the assembled plan.
        require_complete_assignment=False,
        preserve_rendered_assignments=True,
    )
    units = [replace(u, task_markdown=rendered[u.id].strip()) for u in units]
    data["units"] = [unit_payload(u) for u in units]
    data["constraints"] = [asdict(c) for c in constraints]
    data["obligations"] = [asdict(o) for o in obligations]
    node = PreparationNode(key, data, units, obligations, constraints, fragment_ids)
    if corrections:
        raise PreparationDraftCorrection(node, corrections)
    return node


def unit_payload(unit: DeliveryAuthoringUnitDraft) -> dict[str, Any]:
    data = asdict(unit)
    if unit.budget is not None:
        data["budget"] = unit.budget.to_dict()
    return {k: v for k, v in data.items() if v is not None}


@dataclass(frozen=True)
class AcceptedPreparation:
    node: PreparationNode
    children: tuple[AcceptedPreparation, ...]
    receipt: str


def assemble_preparation(
    tree: AcceptedPreparation, *, source: str, task_path: str, plan_id: str, root: Path, output_dir: Path
) -> DeliveryAuthoringDraft:
    """Expand reviewed owner/dependency references on the host, never in a parent prompt."""
    units, obligations, constraints, checkpoints = [], [], [], []
    fragment_records: dict[str, list[dict[str, Any]]] = {key: [] for key in delivery_authority_fragment_map(source)}
    fragment_nodes: dict[str, list[str]] = {key: [] for key in fragment_records}

    def visit(current: AcceptedPreparation) -> tuple[list[str], list[str]]:
        node = current.node
        if current.receipt != authority_digest(
            {"proposal": node.proposal, "children": [c.receipt for c in current.children]}
        ):
            raise failure("hierarchy_authority_changed")
        prefix = "" if node.key == "root" else node.key + "."
        child_owners = {}
        child_outcomes = []
        for child in current.children:
            owned, outcomes = visit(child)
            child_owners[child.node.key.rsplit(".", 1)[-1]] = owned
            child_outcomes.extend(outcomes)
        owners = {**child_owners, **{u.id: [prefix + u.id] for u in node.units}}

        def expand(keys: list[str]) -> list[str]:
            return list(dict.fromkeys(v for k in keys for v in owners[k]))

        for child in node.proposal["children"]:
            deps = expand(child["depends_on"])
            for i, unit in enumerate(units):
                if unit["id"] in child_owners[child["id"]]:
                    units[i] = {**unit, "depends_on": list(dict.fromkeys([*unit["depends_on"], *deps]))}
        for unit in node.units:
            data = unit_payload(unit)
            data.update(id=prefix + unit.id, depends_on=expand(unit.depends_on))
            units.append({k: v for k, v in data.items() if v is not None})
        for collection, parsed in ((obligations, node.obligations), (constraints, node.constraints)):
            collection.extend(
                {**asdict(item), "id": prefix + item.id, "unit_ids": expand(item.unit_ids)} for item in parsed
            )
        owned = expand(list(owners))
        outcome_ids = child_outcomes + [prefix + o.id for o in node.obligations]
        if node.key != "root":
            parent = node.key.rsplit(".", 1)[0] if "." in node.key else "root"
            checkpoints.append(
                {
                    "id": node.key,
                    "parent_id": parent,
                    "unit_ids": owned,
                    "obligation_ids": outcome_ids,
                    "integration_context": "",
                }
            )
        for record in node.proposal["source_accounting"]:
            key = record["source_fragment_id"]
            fragment_records[key].append(
                {
                    **record,
                    "obligation_ids": [prefix + k for k in record["obligation_ids"]],
                    "constraint_ids": [prefix + k for k in record["constraint_ids"]],
                }
            )
            if node.key != "root":
                fragment_nodes[key].append(node.key)
        for child in node.proposal["children"]:
            child_key = prefix + child["id"]
            next(c for c in checkpoints if c["id"] == child_key)["integration_context"] = child["integration_context"]
        return owned, outcome_ids

    visit(tree)
    accounting = []
    root_accounting = {r["source_fragment_id"]: r for r in tree.node.proposal["source_accounting"]}
    for key, records in fragment_records.items():
        outcomes = [v for r in records for v in r["obligation_ids"]]
        rules = [v for r in records for v in r["constraint_ids"]]
        initial = root_accounting[key]
        accounting.append(
            {
                "source_fragment_id": key,
                "disposition": "mapped" if outcomes or rules else "context_only",
                "obligation_ids": outcomes,
                "constraint_ids": rules,
                "rationale": initial["rationale"],
                "checkpoint_ids": sorted(fragment_nodes[key]),
                "final_gate": initial["disposition"] != "delegated" or len(initial["child_ids"]) != 1,
            }
        )
    payload = {
        "plan_id": plan_id,
        "title": tree.node.proposal["title"],
        "units": units,
        "constraints": constraints,
        "obligations": obligations,
        "checkpoints": checkpoints,
        "source_accounting": accounting,
    }
    draft = parse_delivery_authoring_output(
        json.dumps(payload),
        expected_plan_id=plan_id,
        project_root=root,
        output_dir=output_dir,
        source_task_description=source,
        require_obligations=True,
        require_source_accounting=True,
    )
    draft.source_task = DeliveryPlanSourceTask(task_path, "sha256:" + sha256(source.encode()).hexdigest())
    binding = preparation_authority_fingerprint(
        draft.source_task.sha256,
        draft.checkpoints,
        draft.units,
        draft.constraints,
        draft.obligations,
        draft.source_accounting or [],
    )
    draft.constraint_verification = DeliveryConstraintVerification(
        True,
        draft.constraints,
        obligations=draft.obligations,
        source_accounting=draft.source_accounting,
        checkpoint_authority_complete=True,
        final_gate_authority_complete=True,
        checkpoint_authority_input=binding,
    )
    return draft
