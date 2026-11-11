"""Quarantine authority transitions across provider attempts, on all platforms."""

from dataclasses import replace
from unittest.mock import Mock

import pytest

from core.delivery_scope_audit import DeliveryScopeAudit
from core.state import TaskState
from core.validation_artifacts import FileSnapshot


@pytest.fixture
def owned_file():
    audit = DeliveryScopeAudit(
        config=Mock(), store=Mock(), tools={}, agents=lambda: {}, quarantine_session_id="session"
    )
    state = TaskState(task_id="quarantine", task_description="Remove an agent-created scratch file")
    original = FileSnapshot("untracked", True, None, 0o644, digest="sha256:" + "a" * 64, identity=(1, 10))
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={},
        after={"src/scratch.py": original},
        preexisting_file_identities=set(),
        trusted=True,
    )
    return audit, state, original


@pytest.mark.parametrize(
    "changes",
    [
        {"identity": (1, 20)},
        {"digest": "sha256:" + "b" * 64},
        {"mode": 0o600},
        {"status": "ignored"},
        {"symlink_target": "other.py"},
    ],
    ids=["identity", "contents", "mode", "status", "symlink"],
)
def test_rewrite_cannot_reauthorize_a_file_changed_before_the_attempt(owned_file, changes):
    audit, state, original = owned_file
    before = replace(original, **changes)
    after = replace(original, identity=(1, 30), digest="sha256:" + "c" * 64)
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={"src/scratch.py": before},
        after={"src/scratch.py": after},
        preexisting_file_identities=set(),
        trusted=True,
    )
    assert state.delivery_quarantine_candidates == []


@pytest.mark.parametrize("foreign_status", ["untracked", "ignored", "modified"])
def test_moving_an_existing_foreign_file_over_owned_path_revokes_authority(owned_file, foreign_status):
    audit, state, original = owned_file
    foreign = replace(original, status=foreign_status, identity=(1, 20))
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={"src/scratch.py": original, "src/foreign.py": foreign},
        after={"src/scratch.py": replace(foreign, status="untracked")},
        preexisting_file_identities=set(),
        trusted=True,
    )
    assert state.delivery_quarantine_candidates == []


@pytest.mark.parametrize("case", ["preexisting", "new_session", "failed_attempt"])
def test_rewrite_cannot_grant_missing_or_untrusted_authority(owned_file, case):
    audit, state, original = owned_file
    if case == "preexisting":
        state.delivery_quarantine_candidates = []
    elif case == "new_session":
        audit = DeliveryScopeAudit(
            config=Mock(), store=Mock(), tools={}, agents=lambda: {}, quarantine_session_id="new-session"
        )
        audit.start_quarantine_session(state)
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={"src/scratch.py": original},
        after={"src/scratch.py": replace(original, identity=(1, 20))},
        preexisting_file_identities=set(),
        trusted=case != "failed_attempt",
    )
    assert state.delivery_quarantine_candidates == []


@pytest.mark.parametrize(
    "changes",
    [{"status": "modified"}, {"symlink_target": "other.py"}, {"exists": False}, {"identity": None}, {"digest": None}],
    ids=["tracked", "symlink", "absent", "identity_unavailable", "contents_unavailable"],
)
def test_rewrite_requires_a_valid_untracked_file_after_the_attempt(owned_file, changes):
    audit, state, original = owned_file
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={"src/scratch.py": original},
        after={"src/scratch.py": replace(original, **changes)},
        preexisting_file_identities=set(),
        trusted=True,
    )
    assert state.delivery_quarantine_candidates == []


@pytest.mark.parametrize("identity", [(1, 10), (1, 20)], ids=["in_place", "atomic"])
def test_successful_rewrite_refreshes_provenance(owned_file, identity):
    audit, state, original = owned_file
    rewritten = replace(original, identity=identity, digest="sha256:" + "b" * 64, mode=0o600)
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={"src/scratch.py": original},
        after={"src/scratch.py": rewritten},
        preexisting_file_identities=set(),
        trusted=True,
    )
    assert state.delivery_quarantine_candidates == [
        {
            "path": "src/scratch.py",
            "identity": list(identity),
            "digest": rewritten.digest,
            "mode": 0o600,
            "session_id": "session",
        }
    ]


@pytest.mark.parametrize("owned", [False, True])
def test_missing_complete_inventory_cannot_authorize_new_inode(owned_file, owned):
    audit, state, original = owned_file
    if not owned:
        state.delivery_quarantine_candidates = []
    audit._update_delivery_quarantine_candidates(
        state,
        [("src/scratch.py", "src/scratch.py")],
        before={"src/scratch.py": original} if owned else {},
        after={"src/scratch.py": replace(original, identity=(1, 30))},
        trusted=True,
    )
    assert state.delivery_quarantine_candidates == []
