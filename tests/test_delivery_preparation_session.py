from __future__ import annotations

import json

import pytest

from core.delivery_authoring import DeliveryAuthoringParseError
from core.delivery_preparation_session import PreparationSession
from core.delivery_preparation_tree import MAX_PREPARATION_RESPONSE_BYTES
from core.delivery_repair_storage import _MAX_STATE_BYTES


@pytest.mark.parametrize("size", [MAX_PREPARATION_RESPONSE_BYTES + 1, _MAX_STATE_BYTES + 1])
@pytest.mark.parametrize("character", ["x", "\U0001f600"])
def test_oversized_response_survives_audit_failure_in_bounded_private_parts(tmp_path, size, character):
    root = tmp_path.resolve()
    directory = root / ".sikula/state/delivery-preparation/oversized"
    output = character * size
    calls = []
    audited = []

    def generate(prompt):
        calls.append(prompt)
        return output

    def audit(record):
        if not audited:
            audited.append(record)
            raise OSError("Audit temporarily unavailable")
        audited.append(record)

    def session():
        return PreparationSession(root, directory, "bound", generate, audit, lambda: None)

    with pytest.raises(OSError, match="Audit temporarily unavailable"):
        session().exchange("root", "local_review", "prompt")
    saved = json.loads((directory / "exchange-0.json").read_text())
    assert saved["raw_output"] is None and saved["response_parts"]
    assert all(path.stat().st_size <= _MAX_STATE_BYTES for path in directory.glob("*.json"))
    for _ in range(2):
        with pytest.raises(DeliveryAuthoringParseError) as exc:
            session().exchange("root", "local_review", "prompt")
        assert exc.value.code == "delivery_prepare.hierarchy_required"
    assert calls == ["prompt"]
    assert len(audited) == 2
    assert all(record["raw_output"] == output and "response_parts" not in record for record in audited)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_pending_audit_refuses_missing_or_changed_response_parts(tmp_path, damage):
    root = tmp_path.resolve()
    directory = root / ".sikula/state/delivery-preparation/oversized"
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return "x" * (MAX_PREPARATION_RESPONSE_BYTES + 1)

    def unavailable(record):
        raise OSError("Audit unavailable")

    first = PreparationSession(root, directory, "bound", generate, unavailable, lambda: None)
    with pytest.raises(OSError):
        first.exchange("root", "local_review", "prompt")
    path = directory / "exchange-0-response-0.json"
    if damage == "missing":
        path.unlink()
    else:
        path.write_text(json.dumps({"text": "Different response"}))
    audited = []
    resumed = PreparationSession(root, directory, "bound", generate, audited.append, lambda: None)
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        resumed.exchange("root", "local_review", "prompt")
    assert exc.value.code == "delivery_prepare.hierarchy_evidence_unavailable"
    assert not audited and calls == ["prompt"]


@pytest.mark.parametrize("oversized", [False, True])
def test_response_storage_interruption_cannot_reopen_provider_reservation(tmp_path, monkeypatch, oversized):
    import core.delivery_preparation_session as sessions

    root = tmp_path.resolve()
    directory = root / ".sikula/state/delivery-preparation/interrupted"
    calls = []

    def generate(prompt):
        calls.append(prompt)
        return "x" * (MAX_PREPARATION_RESPONSE_BYTES + 1) if oversized else '{"kind":"revise"}'

    write = sessions.write_repair_state

    def interrupt(root, path, value):
        if path.name.startswith("exchange-"):
            raise KeyboardInterrupt
        write(root, path, value)

    with monkeypatch.context() as patch:
        patch.setattr(sessions, "write_repair_state", interrupt)
        first = PreparationSession(root, directory, "bound", generate, None, lambda: None)
        with pytest.raises(KeyboardInterrupt):
            first.exchange("root", "local_review", "prompt")
    resumed = PreparationSession(root, directory, "bound", generate, None, lambda: None)
    with pytest.raises(DeliveryAuthoringParseError) as exc:
        resumed.exchange("root", "local_review", "prompt")
    assert exc.value.code == "delivery_prepare.hierarchy_evidence_unavailable"
    assert calls == ["prompt"]
