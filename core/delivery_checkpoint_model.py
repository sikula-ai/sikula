"""Checkpoint ownership hierarchy and deterministic dependency barriers."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from core.delivery_public_metadata import project_delivery_public_identity, is_safe_delivery_public_metadata


MAX_DELIVERY_CHECKPOINTS = 256
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}\Z")


@dataclass(frozen=True)
class DeliveryCheckpoint:
    id: str
    unit_ids: tuple[str, ...]
    obligation_ids: tuple[str, ...]
    integration_context: str | None = None
    parent_id: str = "root"

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        identity = project_delivery_public_identity if public else lambda value: value
        data = {
            "id": identity(self.id),
            "unit_ids": [identity(key) for key in self.unit_ids],
            "obligation_ids": [identity(key) for key in self.obligation_ids],
        }
        if self.integration_context is not None and not public:
            data["integration_context"] = self.integration_context
        if self.parent_id != "root":
            data["parent_id"] = identity(self.parent_id)
        return data


class DeliveryCheckpointError(ValueError):
    pass


def checkpoint_children(checkpoints: list[DeliveryCheckpoint], node_id: str = "root") -> list[DeliveryCheckpoint]:
    return [node for node in checkpoints if node.parent_id == node_id]


def checkpoint_input_units(unit_ids: tuple[str, ...], units: list[Any]) -> set[str]:
    """Owned work plus transitive prerequisites, without transferring ownership."""
    by_id = {unit.id: unit for unit in units}
    inputs: set[str] = set()
    pending = list(unit_ids)
    while pending:
        key = pending.pop()
        if key in inputs:
            continue
        if key not in by_id:
            raise DeliveryCheckpointError("Checkpoint prerequisite is unavailable.")
        inputs.add(key)
        pending.extend(by_id[key].depends_on)
    return inputs


def checkpoint_postorder(checkpoints: list[DeliveryCheckpoint]) -> list[DeliveryCheckpoint]:
    """Stable children-first order; reject cycles even for unparsed callers."""
    remaining = list(checkpoints)
    ordered = []
    while remaining:
        parents = {node.parent_id for node in remaining}
        leaves = [node for node in remaining if node.id not in parents]
        if not leaves:
            raise DeliveryCheckpointError("Checkpoint hierarchy contains a cycle.")
        ordered.extend(leaves)
        remaining = [node for node in remaining if node not in leaves]
    return ordered


def checkpoint_guarded_units(checkpoint: DeliveryCheckpoint, units: list[Any]) -> set[str]:
    """Direct consumers form the barrier; ordinary dependencies propagate it."""
    covered = set(checkpoint.unit_ids)
    return {
        unit.id
        for unit in units
        if not getattr(unit, "superseded", False) and unit.id not in covered and covered.intersection(unit.depends_on)
    }


def parse_delivery_checkpoints(value: Any, units: list[Any], obligations: list[Any]) -> list[DeliveryCheckpoint]:
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_DELIVERY_CHECKPOINTS:
        raise DeliveryCheckpointError("checkpoints must be a bounded list of declarations.")
    hierarchical = any(isinstance(item, dict) and item.get("parent_id", "root") != "root" for item in value)
    active = {unit.id: unit for unit in units if not getattr(unit, "superseded", False)}
    outcomes = {item.id: item for item in obligations}
    result: list[DeliveryCheckpoint] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) - {"integration_context", "parent_id"} != {
            "id",
            "unit_ids",
            "obligation_ids",
        }:
            raise DeliveryCheckpointError(
                "Each checkpoint requires id, unit_ids, obligation_ids and optional integration_context/parent_id."
            )
        identity = item["id"]
        if (
            not isinstance(identity, str)
            or not _ID.fullmatch(identity)
            or identity.casefold() == "root"
            or identity.casefold() in seen
            or not is_safe_delivery_public_metadata(identity)
        ):
            raise DeliveryCheckpointError(
                "Checkpoint IDs must be unique safe slugs, at most 80 characters; root is reserved."
            )
        for key in ("unit_ids", "obligation_ids"):
            entries = item[key]
            if (
                not isinstance(entries, list)
                or not entries
                or any(not isinstance(entry, str) for entry in entries)
                or len(set(entries)) != len(entries)
            ):
                raise DeliveryCheckpointError(
                    "Checkpoint coverage must contain nonempty unique unit and obligation IDs."
                )
        covered = set(item["unit_ids"])
        if not covered <= active.keys() or covered == active.keys():
            raise DeliveryCheckpointError(
                "A checkpoint must cover a proper subset of active units; the root covers the full plan."
            )
        if not hierarchical and any(not set(active[unit_id].depends_on) <= covered for unit_id in covered):
            raise DeliveryCheckpointError("Checkpoint unit_ids must include every prerequisite of its covered units.")
        for obligation_id in item["obligation_ids"]:
            obligation = outcomes.get(obligation_id)
            owners = set(obligation.unit_ids) if obligation is not None else set()
            later_repairs = {
                key
                for key in owners - covered
                if key in active
                and getattr(active[key], "repair_node", None) not in {None, identity}
                and covered <= set(active[key].depends_on)
            }
            if obligation is None or not owners - later_repairs <= covered:
                raise DeliveryCheckpointError(
                    "Checkpoint obligations must be known and have all contributors inside the covered group."
                )
        integration_context = item.get("integration_context")
        if integration_context is not None:
            try:
                valid_context = (
                    isinstance(integration_context, str)
                    and bool(integration_context.strip())
                    and len(integration_context.encode("utf-8")) <= 4096
                )
            except UnicodeError:
                valid_context = False
            if not valid_context:
                raise DeliveryCheckpointError(
                    "Checkpoint integration_context must be nonempty UTF-8 text within 4096 bytes."
                )
        parent_id = item.get("parent_id", "root")
        if not isinstance(parent_id, str) or (parent_id != "root" and not _ID.fullmatch(parent_id)):
            raise DeliveryCheckpointError("Checkpoint parent_id must identify a node or root.")
        checkpoint = DeliveryCheckpoint(
            identity, tuple(item["unit_ids"]), tuple(item["obligation_ids"]), integration_context, parent_id
        )
        if not checkpoint_guarded_units(checkpoint, units):
            raise DeliveryCheckpointError("A checkpoint must guard downstream consumers of its completed group.")
        seen.add(identity.casefold())
        result.append(checkpoint)

    by_id = {node.id: node for node in result}
    for node in result:
        if node.parent_id != "root":
            parent = by_id.get(node.parent_id)
            if parent is None or parent.id == node.id:
                raise DeliveryCheckpointError("Checkpoint parent must be a distinct declared node.")
            if not set(node.unit_ids) <= set(parent.unit_ids) or not set(node.obligation_ids) <= set(
                parent.obligation_ids
            ):
                raise DeliveryCheckpointError("Checkpoint child coverage must be contained in its parent.")
    checkpoint_postorder(result)
    if hierarchical:
        for parent_id in ("root", *by_id):
            owned_units: set[str] = set()
            owned_outcomes: set[str] = set()
            for child in checkpoint_children(result, parent_id):
                if owned_units.intersection(child.unit_ids) or owned_outcomes.intersection(child.obligation_ids):
                    raise DeliveryCheckpointError("Sibling checkpoints must not duplicate owned units or outcomes.")
                owned_units.update(child.unit_ids)
                owned_outcomes.update(child.obligation_ids)

    # Include barriers in the dependency graph: otherwise overlapping groups can
    # each wait for work that the other checkpoint prevents from starting.
    graph = {"unit:" + key: {"unit:" + dep for dep in unit.depends_on} for key, unit in active.items()}
    for checkpoint in result:
        node = "checkpoint:" + checkpoint.id
        graph[node] = {"unit:" + key for key in checkpoint.unit_ids}
        graph[node].update("checkpoint:" + child.id for child in checkpoint_children(result, checkpoint.id))
        for key in checkpoint_guarded_units(checkpoint, units):
            graph["unit:" + key].add(node)
    while graph:
        leaves = {key for key, deps in graph.items() if not deps}
        if not leaves:
            raise DeliveryCheckpointError("Checkpoint barriers must not create dependency cycles.")
        graph = {key: deps - leaves for key, deps in graph.items() if key not in leaves}
    return result
