"""Shared quarantine authority in isolated standalone and delivery repair calls."""

from hashlib import sha256
import json
import subprocess

import pytest

from agents.fixer_agent import FixerAgent
from agents.implementer_agent import ImplementerAgent
from tests.test_orchestrator import ObservedWriteClient, _make_orchestrator, _save_state, _scoped_delivery_state
from tools import delivery_quarantine_tool as quarantine_tool


pytestmark = pytest.mark.skipif(
    not quarantine_tool.delivery_quarantine_supported(), reason="reversible quarantine is unavailable"
)

OPERATION = json.dumps(
    {"sikula_file_operation_schema_version": 1, "operation": "quarantine_untracked", "path": "src/scratch.py"}
)


@pytest.fixture
def isolated(tmp_project, tmp_path):
    root = tmp_path / "isolated"
    subprocess.run(
        ["git", "worktree", "add", "-b", "isolated", str(root)], cwd=tmp_project, check=True, capture_output=True
    )
    orch, stubs, build = _make_orchestrator(
        root,
        allowed_write_paths=["src/"],
        project_config={"project": {"build_tool": "python"}, "sandbox": {"allowed_write_paths": ["src/"]}},
    )
    state = _save_state(
        orch,
        implementation_prompt="Implement the requested behavior",
        plan_decided=True,
        worktree_path=str(root),
        worktree_base=str(root),
    )
    return root, orch, state, stubs, build


def _agents(root, orch, actions):
    client = ObservedWriteClient(root, actions)
    orch._agents["implementer"] = ImplementerAgent(
        client, orch._tools, orch._agent_project_config, quarantine_executor=orch._execute_implementer_quarantine
    )
    orch._agents["fixer"] = FixerAgent(
        client, orch._tools, orch._agent_project_config, quarantine_executor=orch._execute_fixer_quarantine
    )
    return client


def _create(root):
    (root / "src/scratch.py").write_bytes(b"obsolete implementation\n")
    return ["src/scratch.py"], "Created implementation."


@pytest.mark.parametrize("delivery", [False, True])
@pytest.mark.parametrize("review", ["semantic", "security"])
def test_review_removal_only_preserves_budgets_and_net_tracking(isolated, delivery, review):
    root, orch, state, stubs, _ = isolated
    if delivery:
        state = _scoped_delivery_state(orch, ["src/"])
        state.worktree_path = state.worktree_base = str(root)

    def rewrite():
        temporary = root / "src/replacement.tmp"
        temporary.write_bytes(b"")
        temporary.replace(root / "src/scratch.py")
        return ["src/scratch.py"], "Moved the implementation."

    client = _agents(root, orch, [lambda: _create(root), rewrite, lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    first_identity = state.delivery_quarantine_candidates[0]["identity"]
    state.errors = ["Remove unused implementation"]
    assert orch._run_agent("fixer", state).success
    assert state.delivery_quarantine_candidates[0]["identity"] != first_identity
    state.errors = []
    state.tests_up_to_date = state.final_full_task_review_done = True
    state.step_file_tracking_enabled = True
    state.step_files_changed = ["src/scratch.py"]
    state.review_iterations = 1
    state.security_review_iterations = 1
    state.build_iterations = 2

    def assess(current):
        if (root / "src/scratch.py").exists():
            current.review_issues = ["Remove obsolete src/scratch.py; an empty file is still obsolete."]
        else:
            current.review_issues = []
            if review == "semantic":
                current.review_approved = True
            else:
                current.security_approved = True

    generation = orch._mutation_generation
    if review == "semantic":
        stubs["reviewer"].side_effect = assess
        orch._run_review_loop(state)
        assert (state.review_iterations, state.security_review_iterations) == (2, 1)
        assert state.review_approved and not state.security_approved
    else:
        stubs["security_reviewer"].side_effect = assess
        orch._run_security_review_and_fix_loop(state)
        assert (state.review_iterations, state.security_review_iterations) == (1, 2)
        assert state.review_approved and state.security_approved
    assert not state.failed
    assert state.build_iterations == 2
    assert not state.tests_up_to_date and not state.final_full_task_review_done
    assert orch._mutation_generation == generation + 1
    assert client.provider_calls == 3
    assert not (root / "src/scratch.py").exists()
    assert "src/scratch.py" not in state.files_changed
    assert "src/scratch.py" not in state.step_files_changed
    record = state.delivery_quarantine_records[-1]
    assert record["status"] == "quarantined" and record["requested_by"] == "implementer"
    assert state.implement_cycle_records[-1]["file_quarantined"]["path"] == "src/scratch.py"
    assert quarantine_tool.task_quarantine_summary(root, state.task_id) == (1, 0)
    assert state.delivery_scope_audit_pending is None


@pytest.mark.parametrize(
    "case", ["preexisting", "external_rewrite", "failed_attempt", "scope_violation", "new_process"]
)
def test_standalone_quarantine_never_adopts_untrusted_files(isolated, case):
    root, orch, state, _, _ = isolated
    if case == "preexisting":
        _create(root)

    def create():
        result = _create(root)
        if case == "failed_attempt":
            raise RuntimeError("provider failure")
        if case == "scope_violation":
            (root / "outside.py").write_text("out of scope\n")
        return result

    client = _agents(root, orch, [create, lambda: ([], OPERATION)])
    first = orch._run_agent("implementer", state)
    if case == "scope_violation":
        assert not first.success and state.failed
        assert not state.delivery_quarantine_candidates
        return
    if case == "external_rewrite":
        (root / "src/scratch.py").write_bytes(b"operator contents\n")
    if case == "new_process":
        orch._delivery_scope_audit._quarantine_session_tasks.clear()
        orch._delivery_scope_audit._quarantine_session_id = "different-process"
        orch._delivery_quarantine_session_id = "different-process"
    original = (root / "src/scratch.py").read_bytes()
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    result = orch._run_agent("implementer", state)
    assert not result.success
    assert "delivery_quarantine.provenance_" in result.message
    assert (root / "src/scratch.py").read_bytes() == original
    assert not state.delivery_quarantine_records
    assert client.provider_calls == 2


@pytest.mark.parametrize("after_rename", [False, True])
@pytest.mark.parametrize("review", ["semantic", "security"])
def test_standalone_interrupted_quarantine_blocks_resume(isolated, monkeypatch, after_rename, review):
    root, orch, state, stubs, _ = isolated
    client = _agents(root, orch, [lambda: _create(root), lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    if review == "security":
        state.review_approved = True
        stubs["security_reviewer"].side_effect = lambda current: setattr(current, "review_issues", ["Remove scratch"])

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt()

    if after_rename:
        save = orch._store.save

        def fail_completion(current):
            if any(record.get("status") == "quarantined" for record in current.delivery_quarantine_records):
                interrupt()
            save(current)

        monkeypatch.setattr(orch._store, "save", fail_completion)
    else:
        monkeypatch.setattr(quarantine_tool, "_create_private_directory", interrupt)
    with pytest.raises(KeyboardInterrupt):
        if review == "semantic":
            orch._run_agent("implementer", state)
        else:
            orch._run_security_review_and_fix_loop(state)
    persisted = orch._store.load(state.task_id)
    assert persisted.delivery_quarantine_records[-1]["status"] == "moving"
    assert persisted.review_iterations == 1
    assert persisted.security_review_iterations == (1 if review == "security" else 0)
    if after_rename:
        monkeypatch.setattr(orch._store, "save", save)
    resumed = orch.run(task_id=state.task_id)
    assert resumed.failed and client.provider_calls == 2
    assert any(event["action"] == "delivery_quarantine_cleanup_required" for event in resumed.history)
    assert (root / "src/scratch.py").exists() is not after_rename
    if after_rename:
        common = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        from pathlib import Path

        entry = (
            root
            / common
            / "sikula/quarantine"
            / sha256(state.task_id.encode()).hexdigest()
            / persisted.delivery_quarantine_records[-1]["quarantine_id"]
            / "entry"
        )
        assert Path(entry).read_bytes() == b"obsolete implementation\n"


def test_quarantine_cannot_reuse_previous_invocation_audit(isolated, monkeypatch):
    root, orch, state, _, _ = isolated
    client = _agents(root, orch, [lambda: _create(root)])
    assert orch._run_agent("implementer", state).success
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    # A client bypassing physical-attempt observation cannot use the previous audit.
    monkeypatch.setattr(client, "run_agent", lambda *args, **kwargs: ([], OPERATION))
    result = orch._run_agent("implementer", state)
    assert not result.success
    assert result.data["file_operation_error"] == "delivery_quarantine.scope_unavailable"
    assert (root / "src/scratch.py").exists()
    assert not state.delivery_quarantine_records


@pytest.mark.parametrize("path", ["src/test_scratch.py", "src/checks/helper.py"])
def test_implementer_cannot_quarantine_test_files_inside_production_root(isolated, path):
    root, orch, state, _, _ = isolated
    orch._config.project_config["sandbox"]["allowed_test_write_paths"] = ["src/checks"]

    def create():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text("def test_value(): assert True\n")
        return [path], "Created test."

    _agents(root, orch, [create, lambda: ([], OPERATION.replace("src/scratch.py", path))])
    state.errors = ["Missing regression test"]
    assert orch._run_agent("fixer", state).success
    assert state.delivery_quarantine_candidates[0]["path"] == path
    state.review_issues = ["Remove obsolete test"]
    state.review_iterations = 1
    result = orch._run_agent("implementer", state)
    assert not result.success and result.data["file_operation_error"] == "delivery_quarantine.scope_unavailable"
    assert (root / path).exists()
    assert not state.delivery_quarantine_records


def test_resume_after_completed_move_does_not_reintroduce_absent_paths(isolated, monkeypatch):
    root, orch, state, _, _ = isolated
    _agents(root, orch, [lambda: _create(root), lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    state.step_file_tracking_enabled = True
    state.step_files_changed = ["src/scratch.py"]
    save = orch._store.save

    def interrupt(current):
        save(current)
        if any(record.get("status") == "quarantined" for record in current.delivery_quarantine_records):
            raise KeyboardInterrupt()

    monkeypatch.setattr(orch._store, "save", interrupt)
    with pytest.raises(KeyboardInterrupt):
        orch._run_agent("implementer", state)
    persisted = orch._store.load(state.task_id)
    assert persisted.delivery_quarantine_records[-1]["status"] == "quarantined"
    assert persisted.delivery_scope_audit_pending is None
    monkeypatch.setattr(orch._store, "save", save)
    assert not orch._audit_interrupted_delivery_scope(persisted)
    assert persisted.files_changed == persisted.step_files_changed == []
    assert persisted.review_iterations == 1
    assert persisted.delivery_scope_audit_pending is None


@pytest.mark.parametrize("triage", [False, True])
def test_standalone_fixer_quarantine_uses_its_own_attempt(isolated, triage):
    root, orch, state, _, _ = isolated
    actions = [lambda: _create(root)]
    if triage:
        actions.append(
            lambda: (
                [],
                "TEST FAILURE TRIAGE:\nclassification: production_defect\n"
                "contract_affected: obsolete file\nchosen_fix: production_code\n" + OPERATION,
            )
        )
        state.test_errors = ["Architecture test rejects obsolete file"]
    else:
        state.errors = ["Remove empty source file"]
    actions.append(lambda: ([], OPERATION))
    client = _agents(root, orch, actions)
    assert orch._run_agent("implementer", state).success
    assert orch._run_agent("fixer", state).success
    assert client.provider_calls == (3 if triage else 2)
    assert not (root / "src/scratch.py").exists()
    assert len(state.delivery_quarantine_records) == 1
    assert state.delivery_quarantine_records[0]["requested_by"] == "fixer"
    if triage:
        assert "file_quarantined" not in state.fix_cycle_records[0]
        assert state.fix_cycle_records[1]["triage_pass"] == "production_confirmed"


@pytest.mark.parametrize("outcome", ["recovered", "repeated_violation", "restore_failure"])
@pytest.mark.parametrize("failure_source", ["test", "check"])
def test_standalone_test_only_fixer_preserves_rollback_and_retry(isolated, monkeypatch, outcome, failure_source):
    root, orch, state, _, _ = isolated
    for config in (orch._config.project_config, orch._agent_project_config):
        config["sandbox"]["allowed_test_write_paths"] = ["tests/"]
    production = root / "src/main.py"
    original = production.read_bytes()
    test = root / "tests/quarantine_test.py"
    test.parent.mkdir(exist_ok=True)
    test.write_bytes(b"assert False\n")
    triage = "TEST FAILURE TRIAGE:\nclassification: malformed_test\ncontract_affected: none\nchosen_fix: test_code\n"

    def violate():
        production.write_bytes(b"# rejected production edit\n")
        test.write_bytes(b"# rejected test edit\n")
        return ["src/main.py", "tests/quarantine_test.py"], triage

    def retry():
        assert production.read_bytes() == original
        assert test.read_bytes() == b"assert False\n"
        assert not state.delivery_quarantine_candidates
        assert orch._delivery_scope_audit._verified_provider_attempt_policy is None
        if outcome == "repeated_violation":
            return violate()
        test.write_bytes(b"assert True\n")
        return ["tests/quarantine_test.py"], triage

    client = _agents(root, orch, [lambda: _create(root), violate, retry])
    assert orch._run_agent("implementer", state).success
    assert state.delivery_quarantine_candidates
    if failure_source == "test":
        state.test_errors = ["assertion failed"]
    else:
        state.check_errors = ["tests/quarantine_test.py:1: assertion failed"]
    if outcome == "restore_failure":
        monkeypatch.setattr(
            "agents.fixer_agent.restore_validation_artifacts",
            lambda *_: ["src/main.py: permission denied"],
        )
    result = orch._run_agent("fixer", state)
    assert result.success == (outcome == "recovered"), result
    assert state.failed == (outcome != "recovered")
    assert client.provider_calls == (2 if outcome == "restore_failure" else 3)
    first = state.fix_cycle_records[0]
    assert first["fixer_output"] == triage
    assert first["test_only_scope_violation"]["retry"] == (outcome != "restore_failure")
    assert not state.delivery_quarantine_candidates
    assert not state.delivery_quarantine_records
    assert state.delivery_scope_audit_pending is None
    if outcome == "restore_failure":
        assert first["test_only_scope_violation"]["restore_errors"]
    else:
        assert production.read_bytes() == original
        assert test.read_bytes() == (b"assert True\n" if outcome == "recovered" else b"assert False\n")
        assert state.fix_cycle_records[1]["triage_pass"] == "test_only_retry"
        if outcome == "repeated_violation":
            assert not state.fix_cycle_records[1]["test_only_scope_violation"]["retry"]


@pytest.mark.parametrize("validation", ["deferred", "per_step", "disabled"])
@pytest.mark.parametrize("review", ["semantic", "security"])
def test_delivery_step_cannot_complete_after_its_only_change_is_quarantined(isolated, validation, review):
    root, orch, state, stubs, _ = isolated
    state = _scoped_delivery_state(orch, ["src/"])
    state.worktree_path = state.worktree_base = str(root)
    state.plan = ["Earlier implementation", "Implement current step"]
    state.current_step = 1
    state.step_file_tracking_enabled = True
    (root / "src/earlier.py").write_text("# Earlier step remains in the final diff\n")
    state.files_changed = ["src/earlier.py"]
    orch._config.run_build = validation != "disabled"
    orch._config.run_build_per_step = validation == "per_step"
    client = _agents(root, orch, [lambda: _create(root), lambda: ([], OPERATION)])

    def assess(current):
        current.review_issues = ["Remove obsolete scratch file"] if (root / "src/scratch.py").exists() else []
        if review == "semantic":
            current.review_approved = not current.review_issues
        else:
            current.security_approved = not current.review_issues

    stubs["reviewer" if review == "semantic" else "security_reviewer"].side_effect = assess
    orch._run_step_loop(state)
    assert state.failed and not state.done and not state.plan_completed
    assert state.current_step == 1 and state.step_files_changed == []
    assert state.files_changed == ["src/earlier.py"]
    assert client.provider_calls == 2
    assert state.delivery_quarantine_records[-1]["status"] == "quarantined"
    assert state.history[-1]["action"] == "delivery_no_change_unclassified"
    assert not any(event["action"] == "step_done" for event in state.history)


@pytest.mark.parametrize("triage", [False, True])
def test_fixer_quarantine_accepts_equivalent_overlapping_scopes(isolated, triage):
    root, orch, state, _, build = isolated
    build.is_test_only_change = lambda *args: False
    # Production scope is src/; its normalized audit root is src. The test root
    # deliberately retains the alternate spelling in the captured policy.
    orch._config.project_config["sandbox"]["allowed_test_write_paths"] = ["src/"]
    orch._agent_project_config["sandbox"]["allowed_test_write_paths"] = ["src/"]
    actions = [lambda: _create(root)]
    if triage:
        actions.append(
            lambda: (
                [],
                "TEST FAILURE TRIAGE:\nclassification: production_defect\n"
                "contract_affected: obsolete file\nchosen_fix: production_code\n",
            )
        )
    actions.append(lambda: ([], OPERATION))
    client = _agents(root, orch, actions)
    assert orch._run_agent("implementer", state).success
    if triage:
        state.test_errors = ["Architecture test rejects obsolete file"]
    else:
        state.check_errors = ["Static analysis rejects obsolete implementation"]
    result = orch._run_agent("fixer", state)
    assert result.success, result
    assert client.provider_calls == (3 if triage else 2)
    assert not (root / "src/scratch.py").exists()


@pytest.mark.parametrize("failure", ["malformed", "denied", "completed_move"])
@pytest.mark.parametrize("test_writing", [False, True])
def test_review_fix_resume_validates_retained_implementer_changes(isolated, monkeypatch, failure, test_writing):
    from sikula import _reset_failed_state

    root, orch, state, _, _ = isolated
    (root / "src/foreign.py").write_bytes(b"# Existing file\n")
    state.review_mode = "review_fix"
    state.review_base_branch = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    state.review_diff = "existing branch changes"

    def repair():
        (root / "src/main.py").write_bytes(b"# Retained implementation edit\n")
        output = OPERATION
        if failure == "malformed":
            output = output.replace("quarantine_untracked", "delete")
        elif failure == "denied":
            output = output.replace("src/scratch.py", "src/foreign.py")
        return ["src/main.py"], output

    _agents(root, orch, [lambda: _create(root), repair])
    assert orch._run_agent("implementer", state).success
    # The earlier work is already validated; only this repair must invalidate it.
    state.review_validation_pending = False
    state.tests_up_to_date = True
    state.review_iterations = 1
    state.review_issues = ["Correct implementation and remove scratch"]
    if failure == "completed_move":
        save = orch._store.save

        def interrupt_after_save(current):
            save(current)
            if any(record.get("status") == "quarantined" for record in current.delivery_quarantine_records):
                raise KeyboardInterrupt()

        with monkeypatch.context() as context:
            context.setattr(orch._store, "save", interrupt_after_save)
            with pytest.raises(KeyboardInterrupt):
                orch._run_agent("implementer", state)
    else:
        result = orch._run_agent("implementer", state)
        assert not result.success and result.data["file_operation_error"]
    stopped = orch._store.load(state.task_id)
    assert stopped.review_validation_pending
    stopped.failed = True
    orch._store.save(stopped)
    _reset_failed_state(state.task_id, {}, orch._store)
    assert orch._store.load(state.task_id).review_validation_pending

    resumed, agents, build = _make_orchestrator(
        root,
        allowed_write_paths=["src/"],
        run_review=True,
        run_security_review=True,
        run_build=True,
        run_tests=True,
        run_test_writing=test_writing,
    )
    assert not resumed._session_code_changed
    result = resumed.run(task_id=state.task_id)
    assert result.done and not result.failed
    assert bool(agents["test_writer"].calls) == test_writing
    assert build.compile_calls > 0 and build.test_calls > 0
    assert result.build_status == result.test_status == "success"
    assert not result.review_validation_pending


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
@pytest.mark.parametrize("checkpoint", ["moving", "quarantined"])
@pytest.mark.parametrize("delivery", [False, True])
def test_interrupted_quarantine_preserves_other_repair_writes_for_build_sync(
    isolated, monkeypatch, actor, checkpoint, delivery
):
    from sikula import _reset_failed_state

    root, orch, state, _, build = isolated
    if delivery:
        state = _scoped_delivery_state(orch, ["src/"])
        state.worktree_path = state.worktree_base = str(root)
    config_path = "src/build.conf"
    build.build_config_files = {config_path}

    def repair():
        (root / config_path).write_bytes(b"changed build configuration\n")
        # Audit evidence, not the provider's path list, must survive interruption.
        return [], OPERATION

    _agents(root, orch, [lambda: _create(root), repair])
    assert orch._run_agent("implementer", state).success
    if not delivery:
        state.review_mode = "review_fix"
        state.review_base_branch = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        state.review_diff = "existing branch changes"
    state.build_synced = state.tests_up_to_date = True
    state.review_validation_pending = False
    state.review_iterations = state.security_review_iterations = 1
    state.review_issues = ["Repair configuration and remove scratch"]
    state.errors = ["Repair production configuration"]
    save = orch._store.save

    def interrupt_at_checkpoint(current):
        save(current)
        if any(record.get("status") == checkpoint for record in current.delivery_quarantine_records):
            raise KeyboardInterrupt()

    with monkeypatch.context() as context:
        context.setattr(orch._store, "save", interrupt_at_checkpoint)
        with pytest.raises(KeyboardInterrupt):
            orch._run_agent(actor, state)
    stopped = orch._store.load(state.task_id)
    assert stopped.build_sync_required and not stopped.build_synced
    assert config_path in stopped.files_changed
    assert (stopped.review_iterations, stopped.security_review_iterations) == (1, 1)
    if checkpoint == "moving":
        # A pre-rename intent remains blocked; it must still retain sync invalidation.
        assert (root / "src/scratch.py").exists()
        assert orch.run(task_id=state.task_id).failed
        return
    assert not (root / "src/scratch.py").exists()
    assert "src/scratch.py" not in stopped.files_changed
    if delivery:
        recovered = orch.recover_interrupted_delivery_scope(state.task_id)
        assert not recovered.failed
        assert recovered.build_sync_required and not recovered.build_synced
        assert config_path in recovered.files_changed
        return
    stopped.failed = True
    orch._store.save(stopped)
    _reset_failed_state(state.task_id, {}, orch._store)
    resumed, agents, build = _make_orchestrator(
        root,
        allowed_write_paths=["src/"],
        run_review=True,
        run_security_review=True,
        run_build=True,
        run_tests=True,
        run_test_writing=True,
    )
    build.build_config_files = {config_path}

    for role in ("reviewer", "security_reviewer"):

        def review_after_sync(current, approve=agents[role].side_effect):
            assert build.sync_calls == 1
            assert current.build_synced and not current.build_sync_required
            approve(current)

        agents[role].side_effect = review_after_sync
    result = resumed.run(task_id=state.task_id)
    assert result.done and not result.failed
    assert build.sync_calls == 1 and build.compile_calls > 0 and build.test_calls > 0


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
@pytest.mark.parametrize("delivery", [False, True])
def test_scope_stop_retains_completed_response_only_in_private_audit(isolated, caplog, actor, delivery):
    root, orch, state, _, _ = isolated
    if delivery:
        state = _scoped_delivery_state(orch, ["src/"])
        state.worktree_path = state.worktree_base = str(root)
    private_output = "PRIVATE_COMPLETED_SCOPE_RESPONSE\n" + OPERATION

    def repair():
        (root / "outside.py").write_bytes(b"Rejected write\n")
        return ["outside.py"], private_output

    client = _agents(root, orch, [lambda: _create(root), repair])
    assert orch._run_agent("implementer", state).success
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    state.errors = ["Production check failed"]
    result = orch._run_agent(actor, state)
    assert not result.success and result.message == "unit_scope_violation"
    assert client.provider_calls == 2
    stopped = orch._store.load(state.task_id)
    assert stopped.failed
    cycles = stopped.implement_cycle_records if actor == "implementer" else stopped.fix_cycle_records
    record = cycles[-1]
    assert record[f"{actor}_output"] == private_output
    assert record[f"{actor}_prompt"]
    assert record["files_written"] == ["outside.py"]
    assert not stopped.delivery_quarantine_records
    assert (root / "src/scratch.py").exists()
    public_diagnostics = json.dumps(stopped.history) + str(result) + caplog.text
    assert "PRIVATE_COMPLETED_SCOPE_RESPONSE" not in public_diagnostics


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
@pytest.mark.parametrize("interrupt", [False, True])
@pytest.mark.parametrize("validation_succeeds", [False, True])
def test_scope_stop_persists_retained_writes_before_review_fix_resume(
    isolated, monkeypatch, actor, interrupt, validation_succeeds
):
    from sikula import _reset_failed_state

    root, orch, state, _, _ = isolated
    state.review_mode = "review_fix"
    state.review_base_branch = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    state.review_diff = "existing branch changes"
    (root / "src/original.py").write_bytes(b"# Original branch change\n")
    state.files_changed = ["src/original.py"]
    state.review_approved = state.security_approved = True
    state.tests_up_to_date = state.final_full_task_review_done = True
    state.review_iterations = state.security_review_iterations = 1
    state.build_iterations = 2
    state.review_issues = ["Repair implementation"]
    state.errors = ["Production check failed"]

    def repair():
        (root / "src/main.py").write_bytes(b"# Retained source edit\n")
        (root / "outside.py").write_bytes(b"# Rejected but retained edit\n")
        # The observer must use actual mutations, not provider-reported paths.
        return [], "Repaired."

    _agents(root, orch, [repair])
    save = orch._store.save

    def interrupt_after_stop(current):
        save(current)
        if current.failed:
            raise KeyboardInterrupt()

    with monkeypatch.context() as context:
        if interrupt:
            context.setattr(orch._store, "save", interrupt_after_stop)
            with pytest.raises(KeyboardInterrupt):
                orch._run_agent(actor, state)
        else:
            result = orch._run_agent(actor, state)
            assert not result.success and result.message == "unit_scope_violation"
    stopped = orch._store.load(state.task_id)
    assert stopped.failed and stopped.review_validation_pending
    assert not stopped.tests_up_to_date and not stopped.final_full_task_review_done
    assert not stopped.review_approved and not stopped.security_approved
    assert set(stopped.files_changed) == {"src/original.py", "src/main.py", "outside.py"}
    assert (stopped.review_iterations, stopped.security_review_iterations, stopped.build_iterations) == (1, 1, 2)
    assert not stopped.delivery_quarantine_candidates
    assert (root / "outside.py").exists()
    assert any(record["action"] == "unit_scope_violation" for record in stopped.history)

    _reset_failed_state(state.task_id, {}, orch._store)
    resumed, agents, build = _make_orchestrator(
        root,
        allowed_write_paths=["src/"],
        run_review=True,
        run_security_review=True,
        run_build=True,
        run_tests=True,
        run_test_writing=True,
        max_iterations=1,
    )
    build.test_success = validation_succeeds
    result = resumed.run(task_id=state.task_id)
    assert agents["test_writer"].calls
    assert build.compile_calls > 0 and build.test_calls > 0
    assert result.done is validation_succeeds
    assert result.failed is not validation_succeeds
    assert result.review_validation_pending is not validation_succeeds


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
def test_scope_stop_tracks_step_writes_and_invalidates_build_sync(isolated, actor):
    root, orch, state, _, build = isolated
    state.plan = ["Implement current step"]
    state.step_file_tracking_enabled = True
    state.step_files_changed = ["src/earlier.py"]
    state.build_synced = state.tests_up_to_date = True
    build.build_config_files = {"build.conf"}
    state.errors = ["Production check failed"]

    def repair():
        (root / "src/main.py").write_bytes(b"# Retained source edit\n")
        (root / "build.conf").write_bytes(b"changed configuration\n")
        return [], "Repaired."

    _agents(root, orch, [repair])
    assert not orch._run_agent(actor, state).success
    stopped = orch._store.load(state.task_id)
    assert stopped.failed and stopped.build_sync_required
    assert not stopped.build_synced and not stopped.tests_up_to_date
    assert set(stopped.step_files_changed) == {"src/earlier.py", "src/main.py", "build.conf"}
    assert set(stopped.files_changed) == {"src/main.py", "build.conf"}
    assert not stopped.delivery_quarantine_candidates


@pytest.mark.parametrize(
    "test_root,configured_root,allowed",
    [
        ("integrationtests/", "integrationtests/", True),
        ("commonTest/", "commonTest/", True),
        ("integrationtests/", "tests/", False),
        ("src/shared/", "src/shared/", False),
    ],
)
def test_standalone_fixer_audit_uses_captured_test_scope(isolated, test_root, configured_root, allowed):
    root, orch, state, _, build = isolated
    # A broad/mixed source root must not authorize arbitrary production edits
    # just because it is configured as test-write scope.
    fixture_path = test_root + "fixture.json"
    for config in (orch._config.project_config, orch._agent_project_config):
        config["sandbox"]["allowed_test_write_paths"] = [configured_root]
    if not allowed:
        orch._config.allowed_write_paths = ["src/main.py"]
        for config in (orch._config.project_config, orch._agent_project_config):
            config["sandbox"]["allowed_write_paths"] = ["src/main.py"]
    build.is_test_only_change = lambda *args: False

    def repair():
        (root / "src/main.py").write_bytes(b"# Corrected production code\n")
        fixture = root / fixture_path
        fixture.parent.mkdir(parents=True, exist_ok=True)
        fixture.write_bytes(b"{}\n")
        return ["src/main.py", fixture_path], "Repaired code and fixtures."

    client = _agents(root, orch, [repair])
    state.check_errors = ["Mixed source and fixture validation failure"]
    result = orch._run_agent("fixer", state)
    assert result.success == allowed, result
    assert client.provider_calls == 1
    if allowed:
        assert set(result.data["files_written"]) == {"src/main.py", fixture_path}
    else:
        assert state.failed and result.message == "unit_scope_violation"


def test_review_validation_remains_pending_after_compile_passes_but_tests_fail(isolated):
    from sikula import _reset_failed_state

    root, orch, state, _, _ = isolated
    state.review_diff = "branch changes"
    state.files_changed = ["src/main.py"]
    state.review_validation_pending = True
    orch._store.save(state)
    failing, _, build = _make_orchestrator(
        root, run_build=True, run_tests=True, run_test_writing=False, max_iterations=1
    )
    build.test_success = False
    result = failing.run(task_id=state.task_id)
    assert result.failed and not result.done
    assert result.build_status == "success" and result.test_status == "failed"
    assert failing._store.load(state.task_id).review_validation_pending
    _reset_failed_state(state.task_id, {}, failing._store)

    resumed, _, build = _make_orchestrator(root, run_build=True, run_tests=True, run_test_writing=False)
    result = resumed.run(task_id=state.task_id)
    assert result.done and not result.failed and not result.review_validation_pending
    assert build.compile_calls > 0 and build.test_calls > 0


@pytest.mark.parametrize("failure", ["malformed", "denied"])
@pytest.mark.parametrize("review", ["semantic", "security"])
def test_failed_implementer_quarantine_with_writes_invalidates_tests_on_resume(isolated, failure, review):
    from sikula import _reset_failed_state

    root, orch, state, stubs, _ = isolated
    (root / "src/foreign.py").write_text("# Pre-existing file\n")

    def repair():
        (root / "src/updated.py").write_text("# Retained production change\n")
        output = (
            OPERATION.replace("quarantine_untracked", "delete")
            if failure == "malformed"
            else (OPERATION.replace("src/scratch.py", "src/foreign.py"))
        )
        return ["src/updated.py"], output

    _agents(root, orch, [lambda: _create(root), repair])
    assert orch._run_agent("implementer", state).success
    state.tests_up_to_date = state.final_full_task_review_done = True

    def assess(current):
        current.review_issues = [] if (root / "src/updated.py").exists() else ["Correct production and remove scratch"]
        if review == "semantic":
            current.review_approved = not current.review_issues
        else:
            current.security_approved = not current.review_issues

    stubs["reviewer" if review == "semantic" else "security_reviewer"].side_effect = assess
    if review == "semantic":
        orch._run_review_loop(state)
    else:
        orch._run_security_review_and_fix_loop(state)
    failed = orch._store.load(state.task_id)
    assert failed.failed and not failed.tests_up_to_date
    assert not failed.review_approved and not failed.security_approved and not failed.final_full_task_review_done
    assert "src/updated.py" in failed.files_changed
    assert (root / "src/scratch.py").exists() and (root / "src/foreign.py").exists()

    def refresh_tests(current):
        assert "src/updated.py" in current.files_changed
        current.tests_up_to_date = True

    stubs["test_writer"].side_effect = refresh_tests
    _reset_failed_state(state.task_id, {"project": {"root_path": str(root)}}, orch._store)
    resumed = orch.run(task_id=state.task_id)
    assert resumed.done and not resumed.failed
    assert len(stubs["test_writer"].calls) == 1


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
def test_legacy_delivery_step_without_provenance_cannot_quarantine(isolated, actor):
    root, orch, _, _, _ = isolated
    state = _scoped_delivery_state(orch, ["src/"])
    state.worktree_path = state.worktree_base = str(root)
    state.plan = ["Resumed legacy step"]
    state.step_file_tracking_enabled = False
    _agents(root, orch, [lambda: _create(root), lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    state.review_issues = ["Remove obsolete scratch"]
    state.review_iterations = 1
    state.errors = ["Obsolete scratch"]
    result = orch._run_agent(actor, state)
    assert not result.success and result.data["file_operation_error"] == "delivery_quarantine.scope_unavailable"
    assert (root / "src/scratch.py").exists()
    assert not state.delivery_quarantine_records


def test_security_quarantine_does_not_grant_another_exhausted_semantic_repair(isolated):
    root, orch, state, stubs, _ = isolated
    client = _agents(root, orch, [lambda: _create(root), lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    state.review_approved = True
    state.review_iterations = orch._config.max_review_iterations
    stubs["security_reviewer"].side_effect = lambda current: setattr(current, "review_issues", ["Remove scratch"])
    stubs["reviewer"].side_effect = lambda current: setattr(
        current, "review_issues", ["Further semantic repair needed"]
    )
    orch._run_security_review_and_fix_loop(state)
    assert state.failed and state.review_iterations == orch._config.max_review_iterations
    assert state.security_review_iterations == 1
    assert client.provider_calls == 2
    assert state.delivery_quarantine_records[-1]["status"] == "quarantined"


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
@pytest.mark.parametrize("edit", ["add", "replace", "delete", "nested"])
def test_standalone_authorized_ignore_edit_revokes_only_quarantine_authority(isolated, actor, edit):
    root, orch, state, _, build = isolated
    orch._config.allowed_write_paths = ["src/", ".gitignore"]
    orch._agent_project_config["sandbox"]["allowed_write_paths"] = ["src/", ".gitignore"]
    ignore = root / ("src/.gitignore" if edit == "nested" else ".gitignore")
    if edit in {"replace", "delete"}:
        ignore.write_text("*.log\n")
    (root / "untouched.log").write_text("Pre-existing log\n")
    (root / "build").mkdir()
    (root / "build/cache.bin").write_bytes(b"Unchanged build output")
    build.ephemeral_build_components = {"build"}

    def change():
        if edit == "delete":
            ignore.unlink()
        else:
            ignore.write_text("*.log\nbuild/\n")
        return [ignore.relative_to(root).as_posix()], "Updated ignore rules."

    _agents(root, orch, [change])
    state.errors = ["Update ignore rules"]
    result = orch._run_agent(actor, state)
    assert result.success and not state.failed, result
    assert not state.delivery_quarantine_candidates
    assert any(event["action"] == "quarantine_authority_unavailable" for event in state.history)


@pytest.mark.parametrize("outside", ["outside.log", "nested/.gitignore", "info/exclude"])
def test_changed_ignore_authority_cannot_certify_quarantine(isolated, outside):
    root, orch, state, _, build = isolated
    orch._config.allowed_write_paths = ["src/", ".gitignore"]
    orch._agent_project_config["sandbox"]["allowed_write_paths"] = ["src/", ".gitignore"]
    build.ephemeral_build_components = {"build"}

    def change():
        (root / ".gitignore").write_text("*.log\nbuild/\nnested/\n")
        target = root / outside
        if outside == "info/exclude":
            target = (
                root
                / subprocess.run(
                    ["git", "rev-parse", "--git-path", "info/exclude"],
                    cwd=root,
                    check=True,
                    capture_output=True,
                    text=True,
                ).stdout.strip()
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("Unauthorized change\n")
        return [".gitignore"], "Updated ignore rules."

    _agents(root, orch, [change])
    result = orch._run_agent("implementer", state)
    assert result.success and not state.failed
    assert not state.delivery_quarantine_candidates
    assert any(event["action"] == "quarantine_authority_unavailable" for event in state.history)
    assert orch._delivery_scope_audit._verified_provider_attempt_policy is None


@pytest.mark.parametrize("ignored", [False, True])
@pytest.mark.parametrize("tracked", [False, True])
def test_standalone_audit_preserves_existing_build_output_exemption(isolated, ignored, tracked):
    root, orch, state, _, build = isolated
    (root / ".gitignore").write_text("build/\n" if ignored else "")
    build.ephemeral_build_components = {"build"}
    if tracked:
        (root / "build").mkdir()
        (root / "build/cache.bin").write_bytes(b"Tracked asset")
        subprocess.run(["git", "add", "-f", "build/cache.bin"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Tracked build asset"], cwd=root, check=True, capture_output=True)

    def implement():
        (root / "build").mkdir(exist_ok=True)
        (root / "build/cache.bin").write_bytes(b"Generated output")
        return _create(root)

    _agents(root, orch, [implement])
    result = orch._run_agent("implementer", state)
    assert result.success is (ignored and not tracked)
    assert state.failed is not (ignored and not tracked)
    assert "build/cache.bin" not in {record["path"] for record in state.delivery_quarantine_candidates}


def test_ignore_edit_with_quarantine_request_is_denied_without_a_pending_delivery_audit(isolated):
    root, orch, state, _, _ = isolated
    orch._config.allowed_write_paths = ["src/", ".gitignore"]
    orch._agent_project_config["sandbox"]["allowed_write_paths"] = ["src/", ".gitignore"]

    def change():
        (root / ".gitignore").write_text("*.log\n")
        return [".gitignore"], OPERATION

    _agents(root, orch, [lambda: _create(root), change])
    assert orch._run_agent("implementer", state).success
    assert state.delivery_quarantine_candidates
    state.review_issues = ["Remove scratch and update ignore rules"]
    state.review_iterations = 1
    result = orch._run_agent("implementer", state)
    assert not result.success and result.data["file_operation_error"] == "delivery_quarantine.scope_unavailable"
    persisted = orch._store.load(state.task_id)
    assert not persisted.delivery_quarantine_candidates
    assert persisted.delivery_scope_audit_pending is None
    assert (root / "src/scratch.py").exists()
    assert ".gitignore" in persisted.files_changed


@pytest.mark.parametrize("actor", ["implementer", "fixer"])
@pytest.mark.parametrize("original_deletion", [False, True])
@pytest.mark.parametrize("interrupt", [False, True])
def test_review_fix_quarantine_preserves_only_original_changes(
    isolated, monkeypatch, actor, original_deletion, interrupt
):
    root, orch, state, _, _ = isolated
    (root / "src/branch.py").write_text("Original branch change\n")
    if original_deletion:
        (root / "src/scratch.py").write_text("Original file\n")
    subprocess.run(["git", "add", "src"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Original review changes"], cwd=root, check=True, capture_output=True)
    if original_deletion:
        (root / "src/scratch.py").unlink()
        subprocess.run(["git", "add", "src"], cwd=root, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Remove original file"], cwd=root, check=True, capture_output=True)
    state.review_mode = "review_fix"
    state.files_changed = ["src/branch.py"] + (["src/scratch.py"] if original_deletion else [])
    expected = list(state.files_changed)
    _agents(root, orch, [lambda: _create(root), lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    state.errors = ["Remove scratch"]
    if interrupt:
        save = orch._store.save

        def completed(current):
            save(current)
            if any(record.get("status") == "quarantined" for record in current.delivery_quarantine_records):
                raise KeyboardInterrupt()

        monkeypatch.setattr(orch._store, "save", completed)
        with pytest.raises(KeyboardInterrupt):
            orch._run_agent(actor, state)
        state = orch._store.load(state.task_id)
        monkeypatch.setattr(orch._store, "save", save)
        assert not orch._audit_interrupted_delivery_scope(state)
    else:
        assert orch._run_agent(actor, state).success
    assert not (root / "src/scratch.py").exists()
    assert state.files_changed == expected
    assert state.delivery_quarantine_records[-1]["review_preexisting_change"] is original_deletion


@pytest.mark.parametrize("ignore_path", [".gitignore", "build/.gitignore"])
def test_ignore_edit_with_build_outputs_revokes_all_quarantine_authority(isolated, ignore_path):
    root, orch, state, _, build = isolated
    orch._config.allowed_write_paths = ["src/", ".gitignore"]
    orch._agent_project_config["sandbox"]["allowed_write_paths"] = ["src/", ".gitignore"]
    build.ephemeral_build_components = {"build"}
    (root / ".gitignore").write_text("build/\n")
    (root / "build").mkdir()

    def change():
        (root / ignore_path).write_text("build/\n*.log\n")
        (root / "build/cache.bin").write_bytes(b"Generated during the attempt")
        (root / "src/another.py").write_text("# another new source\n")
        return [ignore_path, "src/another.py"], "Updated ignore rules and implementation."

    _agents(root, orch, [lambda: _create(root), change, lambda: ([], OPERATION)])
    assert orch._run_agent("implementer", state).success
    assert len(state.delivery_quarantine_candidates) == 1
    assert orch._run_agent("implementer", state).success
    assert not state.failed
    assert not state.delivery_quarantine_candidates
    state.review_issues = ["Remove scratch"]
    state.review_iterations = 1
    denied = orch._run_agent("implementer", state)
    assert not denied.success and "provenance_missing" in denied.message
    assert (root / "src/scratch.py").exists()
    assert (root / "src/another.py").exists()


@pytest.mark.parametrize("failure", ["policy", "before", "after", "inventory"])
@pytest.mark.parametrize("request_operation", [False, True])
def test_unavailable_standalone_observation_revokes_authority_without_blocking_ordinary_work(
    isolated, monkeypatch, failure, request_operation
):
    from core.delivery_scope_audit import DeliveryScopeSnapshotError

    root, orch, state, _, _ = isolated

    def change():
        (root / "src/another.py").write_text("# retained implementation\n")
        return ["src/another.py"], OPERATION if request_operation else "Implemented."

    client = _agents(root, orch, [lambda: _create(root), change])
    assert orch._run_agent("implementer", state).success
    assert state.delivery_quarantine_candidates
    state.review_issues = ["Review repair"]
    state.review_iterations = 1
    audit = orch._delivery_scope_audit
    capture = audit._capture_delivery_scope_snapshot
    count = 0

    def unavailable(*args, **kwargs):
        raise DeliveryScopeSnapshotError("Unavailable test evidence")

    def snapshot(*args, **kwargs):
        nonlocal count
        count += 1
        if count == (1 if failure == "before" else 2):
            unavailable()
        return capture(*args, **kwargs)

    if failure == "policy":
        monkeypatch.setattr(orch, "_delivery_scope_audit_policy", unavailable)
    elif failure == "inventory":
        monkeypatch.setattr("core.delivery_scope_audit.delivery_scope_file_identities", unavailable)
    else:
        monkeypatch.setattr(audit, "_capture_delivery_scope_snapshot", snapshot)
    result = orch._run_agent("implementer", state)
    assert client.provider_calls == 2
    assert result.success is not request_operation
    if request_operation:
        assert result.data["file_operation_error"] == "delivery_quarantine.scope_unavailable"
    else:
        assert not state.failed
    assert not state.delivery_quarantine_candidates
    assert not state.delivery_quarantine_records
    assert state.delivery_scope_audit_pending is None
    assert (root / "src/scratch.py").exists()
    assert (root / "src/another.py").exists()


def test_standalone_observation_reads_only_sparse_content_once_per_provider_boundary(isolated, monkeypatch):
    from pathlib import Path
    from core import validation_artifacts

    root, orch, state, _, _ = isolated
    for index in range(200):
        (root / "src" / f"clean{index}.py").write_text("# unchanged source\n")
    subprocess.run(["git", "add", "src"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "Clean sources"], cwd=root, check=True, capture_output=True)
    read = validation_artifacts._snapshot_delivery_scope_regular_file
    capture = orch._delivery_scope_audit._capture_delivery_scope_snapshot
    snapshots = 0

    def guarded_read(path, **kwargs):
        assert not Path(path).name.startswith("clean")
        return read(path, **kwargs)

    def snapshot(*args, **kwargs):
        nonlocal snapshots
        snapshots += 1
        return capture(*args, **kwargs)

    monkeypatch.setattr(validation_artifacts, "_snapshot_delivery_scope_regular_file", guarded_read)
    monkeypatch.setattr(orch._delivery_scope_audit, "_capture_delivery_scope_snapshot", snapshot)
    _agents(root, orch, [lambda: _create(root)])
    assert orch._run_agent("implementer", state).success
    assert snapshots == 2
    assert len(state.delivery_quarantine_candidates) == 1
    assert state.delivery_scope_audit_pending is None
    assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_audit_before") is None
    assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_attempt_before") is None
