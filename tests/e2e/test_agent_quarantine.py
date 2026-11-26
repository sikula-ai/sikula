"""Standalone isolated review repair removes obsolete files before final delivery."""

from hashlib import sha256
import json
from pathlib import Path
import shlex
import subprocess
import sys
from unittest.mock import patch

import pytest

from core.llm_client import LLMConfig, _call_observed
from core.state import JsonStateStore
from sikula import cmd_cleanup, cmd_run
from tests.e2e.test_run import _args, _cfg, _task
from tests.test_sikula_run import _cleanup_args
from tools.delivery_quarantine_tool import delivery_quarantine_supported


@pytest.mark.skipif(not delivery_quarantine_supported(), reason="reversible quarantine is unavailable")
def test_review_fix_quarantine_removes_new_path_from_tests_summary_and_commit(git_review_project, fake_llm):
    from sikula import cmd_review
    from tests.e2e.test_review import _args as review_args, _cfg as review_config

    project, branch = git_review_project
    cfg = review_config(project)
    cfg.update(run_build=False, run_test_writing=True)
    fake = fake_llm()
    fake._config = LLMConfig(provider="test-provider", model="test-model")
    writes = []
    test_prompts = []

    def review(prompt, cwd):
        if "Your job: verify that a task implementation is complete" in prompt:
            if not writes:
                return "## Issues\n- Add the requested helper."
            if (cwd / "src/obsolete.py").exists():
                return "## Issues\n- Remove the obsolete helper."
        return "APPROVED"

    def write(prompt, cwd):
        def invoke():
            if len(writes) >= 2:
                test_prompts.append(prompt)
                assert "src/obsolete.py" not in prompt
                return [], "No additional tests needed."
            writes.append(prompt)
            if len(writes) == 1:
                (cwd / "src/obsolete.py").write_text("# Obsolete helper\n")
                return ["src/obsolete.py"], "Created helper."
            assert "quarantine_untracked" in prompt
            return [], json.dumps(
                {
                    "sikula_file_operation_schema_version": 1,
                    "operation": "quarantine_untracked",
                    "path": "src/obsolete.py",
                }
            )

        return _call_observed(
            fake._config, operation="run_agent", attempt=1, max_attempts=1, input_chars=len(prompt), fn=invoke
        )

    fake.run_readonly_agent = review
    fake.run_agent = write
    with patch("core.llm_client.create_llm_client", return_value=fake), pytest.raises(SystemExit) as result:
        cmd_review(review_args(branch=branch, description="Review calculator changes", fix=True), cfg)
    assert result.value.code == 0
    store = JsonStateStore(project / ".sikula/state")
    state = store.load(store.list_tasks()[0])
    assert state.done and not state.failed and len(writes) == 2
    assert test_prompts
    assert state.files_changed == ["src/calculator.py"]
    assert state.final_summary["files_changed_count"] == 1
    committed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", branch], cwd=project, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    assert "src/obsolete.py" not in committed


@pytest.mark.skipif(not delivery_quarantine_supported(), reason="reversible quarantine is unavailable")
@pytest.mark.parametrize("repair_role", ["semantic", "security"])
@pytest.mark.parametrize("planned", [False, True])
@pytest.mark.parametrize("recover", [False, True])
def test_standalone_review_quarantine_finishes_with_validation_and_commit(
    git_project, fake_llm, repair_role, planned, recover
):
    task = _task(git_project, "Add subtraction in src/subtraction.py without obsolete helper files.")
    cfg = _cfg(git_project)
    cfg.update(run_build=True, run_tests=True, run_build_per_step=planned)
    python = shlex.quote(sys.executable)
    cfg["build"].update(
        compile_command=f"{python} -m compileall -q src/",
        test_command=f"{python} -m pytest -q -p no:cacheprovider tests_proj/",
    )
    fake = fake_llm(
        generate_response="1. Add subtraction\n2. Remove obsolete helper logic" if planned else "SINGLE_PASS"
    )
    fake._config = LLMConfig(provider="test-provider", model="test-model")
    original_review = fake.run_readonly_agent
    calls = []
    rejected = []

    def review(prompt: str, cwd: Path):
        if "Produce the implementation prompt." in prompt:
            return original_review(prompt, cwd)
        role = "security" if "identify security vulnerabilities" in prompt else "semantic"
        final_review = not planned or any(
            marker in prompt for marker in ("\nFINAL FULL-TASK REVIEW SCOPE:\n", "\nFINAL FULL-TASK SECURITY SCOPE:\n")
        )
        if role == repair_role and final_review and (cwd / "src/obsolete.py").exists():
            rejected.append(role)
            heading = "Security Issues" if role == "security" else "Issues"
            return f"## {heading}\n- Remove src/obsolete.py; leaving an empty file does not resolve the finding."
        return "APPROVED"

    def write(prompt: str, cwd: Path):
        def invoke():
            calls.append(prompt)
            if len(calls) == 1:
                assert "quarantine_untracked" not in prompt
                (cwd / "src/subtraction.py").write_text("def subtract(a, b):\n    return a - b\n")
                (cwd / "src/obsolete.py").write_bytes(b"# Obsolete helper\n")
                return ["src/subtraction.py", "src/obsolete.py"], "Implemented subtraction."
            if planned and len(calls) == 2:
                assert "quarantine_untracked" not in prompt
                temporary = cwd / "src/temporary.py"
                temporary.write_bytes(b"")
                temporary.replace(cwd / "src/obsolete.py")
                return ["src/obsolete.py"], "Removed obsolete logic."
            assert len(calls) == (3 if planned else 2)
            assert "quarantine_untracked" in prompt and rejected == [repair_role]
            return [], json.dumps(
                {
                    "sikula_file_operation_schema_version": 1,
                    "operation": "quarantine_untracked",
                    "path": "src/obsolete.py",
                }
            )

        return _call_observed(
            fake._config,
            operation="run_agent",
            attempt=1,
            max_attempts=1,
            input_chars=len(prompt),
            fn=invoke,
        )

    fake.run_agent = write
    fake.run_readonly_agent = review
    index_before = (git_project / ".git/index").read_bytes()
    store = JsonStateStore(git_project / ".sikula/state")
    with patch("core.llm_client.create_llm_client", return_value=fake):
        if recover:
            save = JsonStateStore.save

            def interrupt_completion(current_store, current):
                if any(record.get("status") == "quarantined" for record in current.delivery_quarantine_records):
                    raise KeyboardInterrupt()
                save(current_store, current)

            with patch.object(JsonStateStore, "save", interrupt_completion), pytest.raises(KeyboardInterrupt):
                cmd_run(_args(task_file=str(task), no_isolate=False), cfg)
            interrupted = store.load(store.list_tasks()[0])
            assert interrupted.delivery_quarantine_records[0]["status"] == "moving"
            # An ordinary resume diagnoses the interrupted move without another
            # provider call. Then use exactly the cleanup and reset CLI it reports.
            with pytest.raises(SystemExit) as blocked:
                cmd_run(_args(task_id=interrupted.task_id), cfg)
            assert blocked.value.code == 1
            cmd_cleanup(_cleanup_args(task_id=interrupted.task_id, force=True, quarantine_only=True), cfg)
            cleaned = store.load(interrupted.task_id)
            assert cleaned.delivery_quarantine_records[0]["recovery_pending"] is True
            with pytest.raises(SystemExit) as result:
                cmd_run(_args(task_id=interrupted.task_id, reset_failed=True), cfg)
        else:
            with pytest.raises(SystemExit) as result:
                cmd_run(_args(task_file=str(task), no_isolate=False), cfg)
    assert result.value.code == 0
    state = store.load(store.list_tasks()[0])
    assert state.done and not state.failed and state.result_commit
    assert not state.delivery_plan_id
    assert state.review_approved and state.security_approved
    assert state.build_status == state.test_status == "success"
    assert len(calls) == (3 if planned else 2)
    assert rejected == [repair_role]
    if planned:
        assert state.plan_completed and state.final_full_task_review_done
        assert state.implement_cycle_records[-1]["scope"] == "final_full_task"
        assert sum(record["phase"] == "build" for record in state.validation_cycle_records) >= 3
    assert state.review_iterations == (1 if repair_role == "semantic" else 0)
    assert state.security_review_iterations == (1 if repair_role == "security" else 0)
    assert "src/obsolete.py" not in state.files_changed
    assert state.final_summary["files_changed_count"] == len(state.files_changed) == 1
    record = state.delivery_quarantine_records[0]
    assert record["status"] == ("cleaned" if recover else "quarantined")
    assert record["requested_by"] == "implementer"
    assert not record.get("recovery_pending")
    entry = (
        git_project
        / ".git/sikula/quarantine"
        / sha256(state.task_id.encode()).hexdigest()
        / record["quarantine_id"]
        / "entry"
    )
    if recover:
        assert not entry.exists()
    else:
        assert entry.read_bytes() == (b"" if planned else b"# Obsolete helper\n")
    committed = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", state.result_commit],
        cwd=git_project,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "src/subtraction.py" in committed and "src/obsolete.py" not in committed
    assert (git_project / ".git/index").read_bytes() == index_before
    assert not (git_project / "src/subtraction.py").exists()
