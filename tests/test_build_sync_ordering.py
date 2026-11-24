"""Sync ordering, bounded recovery and replay with real temporary repositories."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tests.test_orchestrator import _make_orchestrator, _save_state, _scoped_delivery_state
from tools.base_tool import Sandbox
from tools.gradle_android_tool import AndroidGradleTool
from tools.node_tool import NodeTool


def _setup(root: Path, *, platform: str = "node", **flags):
    options = dict(
        run_build=True,
        run_tests=False,
        run_checks=False,
        run_test_writing=False,
        run_review=True,
        run_security_review=False,
        run_planner=False,
    )
    options.update(flags)
    orch, agents, build = _make_orchestrator(root, **options)
    source, lock = (
        ("package.json", "package-lock.json") if platform == "node" else ("build.gradle.kts", "gradle.lockfile")
    )
    for name in (source, lock):
        (root / name).write_text("original\n", encoding="utf-8")
    subprocess.run(["git", "add", source, lock], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "build inputs"], cwd=root, check=True, capture_output=True)
    sandbox = Sandbox(project_root=root, allowed_write_paths=["."], allowed_read_paths=["."])
    tool = NodeTool(sandbox, root) if platform == "node" else AndroidGradleTool(sandbox, root)
    build.is_build_config_file = tool.is_build_config_file
    build.is_sync_adoptable_file = tool.is_sync_adoptable_file
    events = []

    def implement(state):
        events.append("implement")
        (root / source).write_text(f"revision {len(agents['implementer'].calls)}\n", encoding="utf-8")
        if source not in state.files_changed:
            state.files_changed.append(source)

    def sync():
        events.append("sync")
        (root / lock).write_bytes((root / source).read_bytes())

    def review(state):
        events.append("review")
        assert (root / lock).read_bytes() == (root / source).read_bytes()
        assert lock in state.files_changed
        state.review_approved = True
        state.review_issues.clear()

    def security(state):
        events.append("security")
        assert (root / lock).read_bytes() == (root / source).read_bytes()
        assert lock in state.files_changed
        state.security_approved = True
        state.review_issues.clear()

    agents["implementer"].side_effect = implement
    agents["implementer"].result_data = {"files_written": [source]}
    agents["reviewer"].side_effect = review
    agents["security_reviewer"].side_effect = security
    build.sync_side_effect = sync
    build.compile_side_effect = lambda: events.append("build")
    return orch, agents, build, source, lock, events


@pytest.mark.parametrize("platform", ["node", "gradle"])
@pytest.mark.parametrize("reviews", ["semantic", "security", "both", "none"])
@pytest.mark.parametrize("planned", [False, True])
def test_config_changes_sync_before_enabled_reviews_or_final_build(tmp_project, platform, reviews, planned):
    orch, _, build, source, lock, events = _setup(
        tmp_project,
        platform=platform,
        run_review=reviews in {"semantic", "both"},
        run_security_review=reviews in {"security", "both"},
        run_build_per_step=False,
    )
    _save_state(
        orch,
        implementation_prompt="Implement",
        plan=["First", "Second"] if planned else [],
        plan_decided=True,
        step_file_tracking_enabled=planned,
    )
    result = orch.run(task_id="t1")
    assert result.done and not result.failed
    assert build.compile_calls == 1
    assert build.sync_calls == (2 if planned and reviews != "none" else 1)
    assert events.index("sync") < events.index("build")
    if reviews != "none":
        assert events.index("sync") < min(events.index(e) for e in ("review", "security") if e in events)
    assert source in result.files_changed and lock in result.files_changed
    assert not result.build_sync_required and result.build_sync_pending is None
    assert any(r["action"] == "sync_outputs_adopted" for r in result.history)


@pytest.mark.parametrize("run_build", [False, True])
def test_ordinary_writes_do_not_trigger_early_sync(tmp_project, run_build):
    orch, agents, build, _, _, events = _setup(tmp_project, run_build=run_build)
    agents["implementer"].side_effect = lambda s: s.files_changed.append("src/main.py")
    agents["implementer"].result_data = {"files_written": ["src/main.py"]}

    def review(state):
        assert build.sync_calls == 0
        events.append("review")
        state.review_approved = True

    agents["reviewer"].side_effect = review
    _save_state(orch, implementation_prompt="Implement")
    result = orch.run(task_id="t1")
    assert result.done
    assert build.sync_calls == int(run_build)


def test_build_disabled_does_not_sync_config_changes(tmp_project):
    orch, agents, build, _, _, _ = _setup(tmp_project, run_build=False)
    agents["reviewer"].side_effect = lambda s: setattr(s, "review_approved", True)
    _save_state(orch, implementation_prompt="Implement")
    result = orch.run(task_id="t1")
    assert result.done and result.build_sync_required
    assert build.sync_calls == build.compile_calls == 0


@pytest.mark.parametrize("role", ["reviewer", "security_reviewer"])
def test_config_repair_adoption_does_not_replenish_review_budget(tmp_project, role):
    orch, agents, build, _, _, events = _setup(
        tmp_project,
        run_security_review=role == "security_reviewer",
        max_review_iterations=1,
        max_security_review_iterations=1,
    )

    def reject(state):
        events.append("reject")
        state.review_issues = ["A real source defect remains"]
        if role == "reviewer":
            state.review_approved = False
        else:
            state.security_approved = False

    agents[role].side_effect = reject
    _save_state(orch, implementation_prompt="Implement")
    result = orch.run(task_id="t1")
    assert result.failed
    assert len(agents[role].calls) == 2
    assert len(agents["implementer"].calls) == build.sync_calls == 2
    assert build.compile_calls == 0
    assert result.review_iterations == 1 if role == "reviewer" else result.security_review_iterations == 1


@pytest.mark.parametrize("moment", ["before", "during", "adoption"])
def test_interrupted_sync_replays_original_adoption_baseline(tmp_project, monkeypatch, moment):
    orch, agents, build, source, lock, events = _setup(tmp_project)
    sync = build.sync_side_effect
    adoption = orch._record_sync_artifact_changes

    def interrupt():
        if moment != "before":
            sync()
        raise KeyboardInterrupt

    if moment == "adoption":
        monkeypatch.setattr(
            orch, "_record_sync_artifact_changes", lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt())
        )
    else:
        build.sync_side_effect = interrupt
    _save_state(orch, implementation_prompt="Implement")
    with pytest.raises(KeyboardInterrupt):
        orch.run(task_id="t1")
    pending = orch._store.load("t1")
    assert pending.build_sync_pending is not None
    assert not pending.build_synced
    assert not agents["reviewer"].calls
    # A completed first command makes the replay a no-op. Its output still needs adoption.
    build.sync_side_effect = sync
    monkeypatch.setattr(orch, "_record_sync_artifact_changes", adoption)
    result = orch.run(task_id="t1")
    assert result.done and result.build_sync_pending is None
    assert len(agents["implementer"].calls) == 1
    assert (tmp_project / lock).read_bytes() == (tmp_project / source).read_bytes()
    assert lock in result.files_changed
    assert build.sync_calls == 2
    assert events.index("review") > events.index("sync")
    adopted = [r for r in result.validation_cycle_records if r["phase"] == "sync"][-1]["metadata"]["sync_outputs"][
        "adopted"
    ]
    assert any(r["path"] == lock for r in adopted)


def test_sync_failure_uses_existing_budget_across_resume(tmp_project):
    orch, agents, build, _, _, _ = _setup(tmp_project, max_iterations=2)
    build.sync_success = False
    agents["fixer"].side_effect = lambda s: s.errors.clear()
    _save_state(orch, implementation_prompt="Implement")
    result = orch.run(task_id="t1")
    assert result.failed and not agents["reviewer"].calls
    assert len(agents["fixer"].calls) == 2
    assert result.build_iterations == 2
    assert build.compile_calls == 0
    # An explicit reset permits another validation, not a new repair budget.
    result.failed = False
    orch._store.save(result)
    result = orch.run(task_id="t1")
    assert result.failed and len(agents["fixer"].calls) == 2


def test_pending_sync_rejects_changed_recovery_policy(tmp_project):
    orch, _, build, _, _, _ = _setup(tmp_project)
    build.sync_side_effect = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    _save_state(orch, implementation_prompt="Implement")
    with pytest.raises(KeyboardInterrupt):
        orch.run(task_id="t1")
    orch._agent_project_config["build"] = {"sync_adopt_paths": ["."]}
    result = orch.run(task_id="t1")
    assert result.failed and build.sync_calls == 1
    assert result.build_sync_pending is not None
    assert any(r["action"] == "sync_recovery_unavailable" for r in result.history)


@pytest.mark.parametrize("mode", ["review_report", "review_fix"])
def test_preexisting_review_diff_does_not_trigger_sync(tmp_project, mode):
    orch, agents, build, source, _, _ = _setup(tmp_project)
    agents["reviewer"].side_effect = lambda s: setattr(s, "review_approved", True)
    state = _save_state(
        orch, implementation_prompt="Review", files_changed=[source], review_mode=mode, review_diff="preexisting diff"
    )
    with patch.object(orch, "_refresh_review_fix_diff", return_value=True):
        result = orch.run(task_id=state.task_id)
    assert result.done and not agents["implementer"].calls
    assert build.sync_calls == build.compile_calls == 0


def test_sync_repair_during_deferred_steps_keeps_final_whole_task_review(tmp_project):
    orch, agents, build, source, _, _ = _setup(tmp_project, run_build_per_step=False, max_iterations=3)
    sync = build.sync_side_effect
    scopes = []
    reviewer = agents["reviewer"].side_effect

    def recoverable_sync():
        build.sync_success = build.sync_calls > 1
        sync()

    def fix(state):
        (tmp_project / source).write_text("fixed configuration\n", encoding="utf-8")
        state.errors.clear()

    def review(state):
        scopes.append(state.active_scope)
        reviewer(state)

    build.sync_side_effect = recoverable_sync
    agents["fixer"].side_effect = fix
    agents["fixer"].result_data = {"files_written": [source]}
    agents["reviewer"].side_effect = review
    _save_state(
        orch,
        implementation_prompt="Implement",
        plan=["First", "Second"],
        plan_decided=True,
        step_file_tracking_enabled=True,
    )
    result = orch.run(task_id="t1")
    assert result.done and result.final_full_task_review_done
    assert scopes == [None, None, "final_full_task"]
    assert build.compile_calls == 1
    assert len(agents["fixer"].calls) == 1


@pytest.mark.parametrize("change", ["head", "snapshot", "build_disabled"])
def test_pending_sync_cannot_rebaseline_or_run_with_disabled_builds(tmp_project, change):
    orch, _, build, _, _, _ = _setup(tmp_project)
    build.sync_side_effect = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    _save_state(orch, implementation_prompt="Implement")
    with pytest.raises(KeyboardInterrupt):
        orch.run(task_id="t1")
    if change == "head":
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "moved authority"],
            cwd=tmp_project,
            check=True,
            capture_output=True,
        )
    elif change == "snapshot":
        state = orch._store.load("t1")
        state.build_sync_pending["before"] = {"../outside": "{}"}
        orch._store.save(state)
    else:
        orch._config.run_build = False
    result = orch.run(task_id="t1")
    assert result.failed and result.build_sync_pending is not None
    assert build.sync_calls == 1


def test_review_fix_syncs_new_writes_but_not_initial_diff(tmp_project):
    orch, agents, build, source, _, _ = _setup(tmp_project, max_review_iterations=1)
    review = agents["reviewer"].side_effect

    def first_reject(state):
        if len(agents["reviewer"].calls) == 1:
            assert build.sync_calls == 0
            state.review_issues = ["Fix the dependency declaration"]
        else:
            review(state)

    agents["reviewer"].side_effect = first_reject
    _save_state(
        orch,
        implementation_prompt="Review",
        files_changed=[source],
        review_mode="review_fix",
        review_diff="preexisting diff",
    )
    with patch.object(orch, "_refresh_review_fix_diff", return_value=True):
        result = orch.run(task_id="t1")
    assert result.done and build.sync_calls == build.compile_calls == 1
    assert len(agents["reviewer"].calls) == 2


def test_post_validation_config_fix_requires_fresh_build_after_early_sync(tmp_project):
    orch, agents, build, source, _, events = _setup(tmp_project, max_iterations=3)
    review = agents["reviewer"].side_effect

    def reject_once(state):
        if len(agents["reviewer"].calls) == 1:
            state.review_issues = ["Fix configuration"]
            state.review_approved = False
        else:
            review(state)

    def compile():
        events.append("build")
        if build.compile_calls == 1:
            state.review_approved = False

    agents["reviewer"].side_effect = reject_once
    build.compile_side_effect = compile
    state = _save_state(
        orch, implementation_prompt="Implement", files_changed=["src/main.py"], review_approved=True, build_synced=True
    )
    # Exercise the same object that the build hook observes, as the outer run does.
    orch._run_build_fix_loop(state, set_done=True)
    assert state.done and not state.failed
    assert source in state.files_changed
    assert build.compile_calls == 2 and build.sync_calls == 1
    assert events.index("build") < events.index("sync") < len(events) - 1
    assert events[-1] == "build"


def test_pending_sync_allows_unrelated_provider_configuration_change(tmp_project):
    orch, _, build, _, _, _ = _setup(tmp_project)
    sync = build.sync_side_effect
    build.sync_side_effect = lambda: (_ for _ in ()).throw(KeyboardInterrupt())
    _save_state(orch, implementation_prompt="Implement")
    with pytest.raises(KeyboardInterrupt):
        orch.run(task_id="t1")
    orch._agent_project_config["llm"] = {"model": "replacement-model"}
    build.sync_side_effect = sync
    result = orch.run(task_id="t1")
    assert result.done and build.sync_calls == 2


def test_final_review_sync_interruption_preserves_consumed_fix_attempt(tmp_project):
    orch, agents, build, _, _, _ = _setup(tmp_project, max_review_iterations=1)
    review = agents["reviewer"].side_effect
    sync = build.sync_side_effect

    def final_reject(state):
        if state.active_scope == "final_full_task":
            state.review_approved = False
            state.review_issues = ["Still incorrect"]
        else:
            review(state)

    def interrupt_final_fix_sync():
        sync()
        if build.sync_calls == 2:
            raise KeyboardInterrupt

    agents["reviewer"].side_effect = final_reject
    build.sync_side_effect = interrupt_final_fix_sync
    _save_state(
        orch, implementation_prompt="Implement", plan=["Only step"], plan_decided=True, step_file_tracking_enabled=True
    )
    with pytest.raises(KeyboardInterrupt):
        orch.run(task_id="t1")
    before = orch._store.load("t1")
    assert before.active_scope == "final_full_task" and before.review_iterations == 1
    result = orch.run(task_id="t1")
    assert result.failed and result.review_iterations == 1
    assert len(agents["implementer"].calls) == 2
    assert build.compile_calls == 0


@pytest.mark.parametrize("include_output", [False, True])
def test_early_sync_keeps_delivery_output_write_scope(tmp_project, include_output):
    paths = ["package.json", "package-lock.json"] if include_output else ["package.json"]
    orch, agents, build, source, lock, _ = _setup(tmp_project, allowed_write_paths=paths)
    state = _scoped_delivery_state(orch, [source, lock] if include_output else [source])
    orch._run_single_pass(state)
    if include_output:
        assert state.done and state.review_approved
        assert lock in state.files_changed
    else:
        assert state.failed and state.delivery_stop_code == "unit_scope_violation"
        assert lock not in state.files_changed
        assert not agents["reviewer"].calls and not agents["fixer"].calls
        assert build.compile_calls == 0


def test_replayed_sync_does_not_adopt_unconfigured_new_output(tmp_project):
    orch, _, build, source, lock, _ = _setup(tmp_project)
    new_lock = "new-generated.lock"
    build.sync_adoptable_files.add(new_lock)
    classifier = build.is_sync_adoptable_file
    build.is_sync_adoptable_file = lambda p: p == new_lock or classifier(p)
    sync = build.sync_side_effect

    def partial():
        sync()
        (tmp_project / new_lock).write_text("generated\n", encoding="utf-8")
        if build.sync_calls == 1:
            raise KeyboardInterrupt

    build.sync_side_effect = partial
    _save_state(orch, implementation_prompt="Implement")
    with pytest.raises(KeyboardInterrupt):
        orch.run(task_id="t1")
    result = orch.run(task_id="t1")
    assert result.done and not (tmp_project / new_lock).exists()
    assert source in result.files_changed and lock in result.files_changed
    assert new_lock not in result.files_changed
