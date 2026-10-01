"""Explicit flat checkpoint declarations and deterministic barrier validation."""

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

    def to_dict(self, *, public: bool = False) -> dict[str, Any]:
        identity = project_delivery_public_identity if public else lambda value: value
        return {
            "id": identity(self.id),
            "unit_ids": [identity(key) for key in self.unit_ids],
            "obligation_ids": [identity(key) for key in self.obligation_ids],
        }


class DeliveryCheckpointError(ValueError):
    pass


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
        raise DeliveryCheckpointError("checkpoints must be a bounded list of flat declarations.")
    active = {unit.id: unit for unit in units if not getattr(unit, "superseded", False)}
    outcomes = {item.id: item for item in obligations}
    result: list[DeliveryCheckpoint] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "unit_ids", "obligation_ids"}:
            raise DeliveryCheckpointError("Each checkpoint requires exactly id, unit_ids, and obligation_ids.")
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
        if any(not set(active[unit_id].depends_on) <= covered for unit_id in covered):
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
        checkpoint = DeliveryCheckpoint(identity, tuple(item["unit_ids"]), tuple(item["obligation_ids"]))
        if not checkpoint_guarded_units(checkpoint, units):
            raise DeliveryCheckpointError("A checkpoint must guard downstream consumers of its completed group.")
        seen.add(identity.casefold())
        result.append(checkpoint)

    # Include barriers in the dependency graph: otherwise overlapping groups can
    # each wait for work that the other checkpoint prevents from starting.
    graph = {"unit:" + key: {"unit:" + dep for dep in unit.depends_on} for key, unit in active.items()}
    for checkpoint in result:
        node = "checkpoint:" + checkpoint.id
        graph[node] = {"unit:" + key for key in checkpoint.unit_ids}
        for key in checkpoint_guarded_units(checkpoint, units):
            graph["unit:" + key].add(node)
    while graph:
        leaves = {key for key, deps in graph.items() if not deps}
        if not leaves:
            raise DeliveryCheckpointError("Checkpoint barriers must not create dependency cycles.")
        graph = {key: deps - leaves for key, deps in graph.items() if key not in leaves}
    return result
