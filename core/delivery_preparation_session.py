"""Private durable reservations for one bounded hierarchical preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from core.delivery_authority import authority_digest
from core.delivery_preparation_tree import (
    failure,
    MAX_PREPARATION_NODES,
    MAX_PREPARATION_PACKET_BYTES,
    MAX_PREPARATION_RESPONSE_BYTES,
)
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_authoring import delivery_output_has_prerequisite_stop
from core.llm_client import LLMReadOnlyViolation

MAX_NODE_CALLS = 12
MAX_PREPARATION_CALLS = MAX_PREPARATION_NODES * MAX_NODE_CALLS
# Even JSON escaping of supplementary Unicode fits the existing per-file bound.
_RESPONSE_PART_CHARS = 64 * 1024
_TERMINAL_CODES = {"prerequisite_stop", "readonly_violation", "hierarchy_authority_changed"}


class PreparationSession:
    def __init__(
        self,
        root: Path,
        directory: Path,
        binding: str,
        generate: Callable[[str], str],
        audit: Callable[[dict[str, Any]], None] | None,
        validate: Callable[[], None],
        context_reader: Callable[[list[str]], dict[str, Any]] | None = None,
    ) -> None:
        self.root, self.directory, self.binding = root, directory, binding
        self.generate, self.audit = generate, audit
        self.validate = validate
        self.context_reader = context_reader
        self.path = directory / "control.json"
        stored = read_repair_state(root, self.path)
        self.state = (
            stored if stored is not None else {"binding": binding, "calls": [], "terminal": None, "contexts": {}}
        )
        if (
            set(self.state) != {"binding", "calls", "terminal", "contexts"}
            or self.state["binding"] != binding
            or not isinstance(self.state["calls"], list)
            or not isinstance(self.state["contexts"], dict)
            or len(self.state["contexts"]) > MAX_PREPARATION_NODES
        ):
            raise failure("hierarchy_stale")
        if self.state["terminal"] is not None and self.state["terminal"] not in _TERMINAL_CODES:
            raise failure("hierarchy_evidence_unavailable")
        for index, call in enumerate(self.state["calls"]):
            if (
                not isinstance(call, dict)
                or set(call) != {"index", "node", "key", "status", "digest"}
                or call["index"] != index
                or not isinstance(call["node"], str)
                or not isinstance(call["key"], str)
                or call["status"] not in {"reserved", "received", "accepted"}
                or (call["status"] != "reserved" and not isinstance(call["digest"], str))
            ):
                raise failure("hierarchy_evidence_unavailable")
        self.check()
        self.validate_inputs()
        self.save()

    def _context_path(self, node: str) -> Path:
        return self.directory / ("context-" + authority_digest(node).removeprefix("sha256:") + ".json")

    def _read_context(self, paths: list[str]) -> dict[str, Any]:
        if (
            self.context_reader is None
            or not isinstance(paths, list)
            or not 1 <= len(paths) <= 8
            or any(not isinstance(p, str) or not p or len(p) > 512 for p in paths)
            or len(set(paths)) != len(paths)
        ):
            raise failure("hierarchy_evidence_unavailable")
        result = self.context_reader(paths)
        files = result.get("files") if isinstance(result, dict) else None
        if (
            not isinstance(files, list)
            or len(files) != len(paths)
            or any(not isinstance(f, dict) or f.get("status") != "read" for f in files)
        ):
            raise failure("hierarchy_evidence_unavailable")
        return result

    def validate_inputs(self) -> None:
        self.validate()
        for node, reference in self.state["contexts"].items():
            if not isinstance(reference, dict) or set(reference) != {"paths", "fingerprint"}:
                raise failure("hierarchy_evidence_unavailable")
            saved = read_repair_state(self.root, self._context_path(node))
            if saved is None or authority_digest(saved) != reference["fingerprint"]:
                raise failure("hierarchy_evidence_unavailable")
            if authority_digest(self._read_context(reference["paths"])) != reference["fingerprint"]:
                raise failure("hierarchy_authority_changed")

    def context(self, node: str, paths: list[str]) -> dict[str, Any]:
        self.check()
        self.validate_inputs()
        previous = self.state["contexts"].get(node)
        result = self._read_context(paths)
        reference = {"paths": paths, "fingerprint": authority_digest(result)}
        if previous is not None and previous != reference:
            raise failure("hierarchy_authority_changed")
        if previous is None:
            write_repair_state(self.root, self._context_path(node), result)
            self.state["contexts"][node] = reference
            self.save()
            if self.audit is not None:
                self.audit(
                    {
                        "phase": "delivery_prepare_hierarchy",
                        "node_id": node,
                        "stage": "context_retrieval",
                        "result": result,
                    }
                )
        return result

    def check(self) -> None:
        if self.state["terminal"] is not None:
            raise failure(self.state["terminal"])

    def save(self) -> None:
        write_repair_state(self.root, self.path, self.state)

    def stop(self, code: str) -> None:
        self.state["terminal"] = code
        self.save()
        raise failure(code)

    def assessment(
        self, node: str, stage: str, prompt: str, *, verdict: str | None = None, error: str | None = None
    ) -> None:
        key = authority_digest({"node": node, "stage": stage, "prompt": prompt})
        path = self.directory / ("assessment-" + key.removeprefix("sha256:") + ".json")
        record = {
            "phase": "delivery_prepare_hierarchy",
            "node_id": node,
            "stage": stage,
            "exchange_fingerprint": key,
            "parsed": {"status": verdict or "failed", "error_code": error},
        }
        existing = read_repair_state(self.root, path)
        if existing is not None:
            if existing != record:
                raise failure("hierarchy_authority_changed")
            return
        if self.audit is not None:
            self.audit(record)
        write_repair_state(self.root, path, record)

    def _response_part_path(self, index: int, part: int) -> Path:
        return self.directory / f"exchange-{index}-response-{part}.json"

    def _audit_received(self, call: dict[str, Any], saved: dict[str, Any]) -> None:
        if call["status"] != "received":
            return
        if self.audit is not None:
            full_record = saved
            if "response_parts" in saved:
                parts = []
                for part, fingerprint in enumerate(saved["response_parts"]):
                    data = read_repair_state(self.root, self._response_part_path(call["index"], part))
                    if (
                        data is None
                        or authority_digest(data) != fingerprint
                        or set(data) != {"text"}
                        or not isinstance(data["text"], str)
                    ):
                        raise failure("hierarchy_evidence_unavailable")
                    parts.append(data["text"])
                full_record = {k: v for k, v in saved.items() if k != "response_parts"}
                full_record["raw_output"] = "".join(parts)
            # A crash may duplicate the audit entry, never the provider call.
            self.audit(full_record)
        call["status"] = "accepted"
        self.save()

    def exchange(self, node: str, stage: str, prompt: str) -> str:
        self.check()
        self.validate_inputs()
        if len(prompt.encode("utf-8")) > MAX_PREPARATION_PACKET_BYTES:
            raise failure("hierarchy_required")
        key = authority_digest({"node": node, "stage": stage, "prompt": prompt})
        matches = [c for c in self.state["calls"] if c["key"] == key]
        for call in matches:
            if call["status"] in {"accepted", "received"}:
                saved = read_repair_state(self.root, self.directory / f"exchange-{call['index']}.json")
                if saved is None or authority_digest(saved) != call["digest"] or saved["prompt"] != prompt:
                    raise failure("hierarchy_evidence_unavailable")
                self._audit_received(call, saved)
                error = saved["parsed"].get("error_code")
                if error:
                    raise failure("hierarchy_required")
                return saved["raw_output"]
        if (
            len(matches) >= 2
            or len(self.state["calls"]) >= MAX_PREPARATION_CALLS
            or sum(c["node"] == node for c in self.state["calls"]) >= MAX_NODE_CALLS
        ):
            raise failure("hierarchy_budget_exhausted")
        index = len(self.state["calls"])
        call = {"index": index, "node": node, "key": key, "status": "reserved", "digest": None}
        self.state["calls"].append(call)
        self.save()  # Reservation precedes provider work and survives interruption.
        record = {
            "phase": "delivery_prepare_hierarchy",
            "node_id": node,
            "stage": stage,
            "round_index": len(matches) + 1,
            "prompt": prompt,
            "raw_output": None,
        }
        try:
            output = self.generate(prompt)
        except BaseException as exc:
            if isinstance(exc, LLMReadOnlyViolation):
                self.state["terminal"] = "readonly_violation"
            elif getattr(exc, "code", None) == "delivery_prepare.hierarchy_authority_changed":
                self.state["terminal"] = self.state["terminal"] or "hierarchy_authority_changed"
            self.save()  # Persist a boundary even if writing the response audit fails.
            record["parsed"] = {"status": "failed", "error_type": type(exc).__name__}
            write_repair_state(self.root, self.directory / f"exchange-{index}.json", record)
            self.save()
            if self.audit is not None:
                self.audit(record)
            raise
        record["raw_output"] = output
        if delivery_output_has_prerequisite_stop(output):
            self.state["terminal"] = "prerequisite_stop"
            self.save()
        try:
            oversized = len(output.encode("utf-8")) > MAX_PREPARATION_RESPONSE_BYTES
        except (AttributeError, UnicodeError):
            oversized = True
        record["parsed"] = (
            {"status": "failed", "error_code": "delivery_prepare.hierarchy_required"}
            if oversized
            else {"status": "received"}
        )
        # Keep complete private evidence even when the output cannot be used as
        # a proposal. Bounded chunks avoid increasing the control-file limit and
        # survive audit failure; the replay record remains a failure marker.
        if oversized:
            fingerprints = [
                authority_digest({"text": output[start : start + _RESPONSE_PART_CHARS]})
                for start in range(0, len(output), _RESPONSE_PART_CHARS)
            ]
            record = {**record, "raw_output": None, "response_parts": fingerprints}
        # Once the response is known, fail closed if its subsequent evidence
        # write is interrupted. Missing evidence must not reopen a provider slot.
        call.update(status="received", digest=authority_digest(record))
        self.save()
        if oversized:
            for part, start in enumerate(range(0, len(output), _RESPONSE_PART_CHARS)):
                write_repair_state(
                    self.root,
                    self._response_part_path(index, part),
                    {"text": output[start : start + _RESPONSE_PART_CHARS]},
                )
        write_repair_state(self.root, self.directory / f"exchange-{index}.json", record)
        # Provider work is complete before revalidating repository evidence.
        # An unavailable input must never turn this response into another call.
        try:
            self.validate_inputs()
        except BaseException as exc:
            if isinstance(exc, LLMReadOnlyViolation):
                self.state["terminal"] = "readonly_violation"
                self.save()
            elif getattr(exc, "code", None) == "delivery_prepare.hierarchy_authority_changed":
                self.state["terminal"] = self.state["terminal"] or "hierarchy_authority_changed"
                self.save()
            self._audit_received(call, record)
            raise
        self._audit_received(call, record)
        self.check()
        if oversized:
            raise failure("hierarchy_required")
        return output
