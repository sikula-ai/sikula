"""Tests for core/orchestrator.py — pipeline orchestration logic."""

from __future__ import annotations

import os
import subprocess
import stat
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import pytest

import core.orchestrator as orchestrator_module
import core.delivery_scope_audit as delivery_scope_audit_module
from core import llm_client as llm_client_module
from agents.base_agent import AgentResult
from agents.fixer_agent import FixerAgent
from tests.conftest import StubLLMClient
from core.delivery_write_scope import DeliveryWriteScopeError, apply_delivery_write_scope_to_config
from core.llm_client import LLMConfig, LLMTransientError
from core.orchestrator import (
    Orchestrator,
    OrchestratorConfig,
    _build_tool,
    _fmt_elapsed,
)
from core.state import JsonStateStore, TaskState
from core.structured_output import (
    DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP,
    DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT,
    DELIVERY_DISPOSITION_SCHEMA_VERSION,
    DeliveryDisposition,
)
from core.test_execution_gate_audit import detect_new_test_execution_gates
from core.validation_artifacts import DeliveryScopeSnapshotError, delivery_scope_git_binding
from tools.base_tool import Sandbox, ToolResult
from tools.cargo_tool import CargoTool
from tools.gradle_android_tool import AndroidGradleTool
from tools.node_tool import NodeTool
from tools.python_tool import PythonTool


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _git_head(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD^{commit}"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_ignore_fingerprint(path: Path) -> str:
    return delivery_scope_git_binding(path).ignore_fingerprint


def _git_scope_binding_fields(path: Path) -> dict[str, str]:
    binding = delivery_scope_git_binding(path)
    return {
        "git_baseline": binding.baseline,
        "git_dir": binding.git_dir,
        "git_common_dir": binding.common_dir,
        "git_ignore_fingerprint": binding.ignore_fingerprint,
        "git_ref_fingerprint": binding.ref_fingerprint,
    }


def _git_dir(path: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "--absolute-git-dir"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@dataclass
class StubAgent:
    name: str = ""
    calls: list = field(default_factory=list)
    side_effect: Callable[[TaskState], None] | None = None
    result_data: dict | None = None  # when set, overrides auto-detected files_written
    result_success: bool = True
    result_message: str | None = None
    raise_exception: Exception | None = None
    llm: object | None = None
    active_test_write_paths: list[str] = field(default_factory=list)

    def delivery_scope_active_test_write_paths(self, state: TaskState) -> list[str]:
        return list(self.active_test_write_paths)

    def run(self, state: TaskState) -> AgentResult:
        if self.raise_exception:
            raise self.raise_exception
        self.calls.append(state)
        files_before = list(state.files_changed)
        if self.side_effect:
            self.side_effect(state)
        if state.failed:
            return AgentResult(success=False, message=f"{self.name} failed")
        data = (
            self.result_data
            if self.result_data is not None
            else {"files_written": [f for f in state.files_changed if f not in files_before]}
        )
        return AgentResult(success=self.result_success, message=self.result_message or f"{self.name} ok", data=data)


class StubBuildTool:
    def __init__(self) -> None:
        self.sync_success = True
        self.compile_success = True
        self.test_success = True
        self.check_success = True
        self.presync_success = True
        self.compile_results: list[bool] = []
        self.check_results: dict[str, list[bool]] = {}
        self.build_config_files: set[str] = set()
        self.sync_calls = 0
        self.compile_calls = 0
        self.test_calls = 0
        self.presync_calls = 0
        self.check_calls: list[str] = []
        self.check_configs: list[tuple[str, dict]] = []
        self.compile_side_effect: Callable[[], None] | None = None
        self.test_side_effect: Callable[[], None] | None = None
        self.presync_side_effect: Callable[[], None] | None = None
        self.check_side_effects: dict[str, Callable[[], None]] = {}
        self.sync_side_effect: Callable[[], None] | None = None
        self.sync_metadata: dict = {}
        self.sync_adoptable_files: set[str] = set()
        self.ephemeral_build_components: set[str] = set()

    def sync(self) -> ToolResult:
        self.sync_calls += 1
        if self.sync_side_effect:
            self.sync_side_effect()
        return ToolResult(
            success=self.sync_success,
            output="",
            error="" if self.sync_success else "sync failed",
            metadata=dict(self.sync_metadata),
        )

    def generate_sources(self) -> ToolResult:
        self.presync_calls += 1
        if self.presync_side_effect:
            self.presync_side_effect()
        return ToolResult(
            success=self.presync_success, output="", error="" if self.presync_success else "presync failed"
        )

    def compile_check(self) -> ToolResult:
        self.compile_calls += 1
        if self.compile_side_effect:
            self.compile_side_effect()
        if self.compile_results:
            success = self.compile_results.pop(0)
        else:
            success = self.compile_success
        return ToolResult(success=success, output="", error="" if success else "compile failed")

    def run_tests(self) -> ToolResult:
        self.test_calls += 1
        if self.test_side_effect:
            self.test_side_effect()
        return ToolResult(success=self.test_success, output="", error="" if self.test_success else "tests failed")

    def run_check(self, name: str, config: dict) -> ToolResult:
        self.check_calls.append(name)
        self.check_configs.append((name, config))
        if name in self.check_side_effects:
            self.check_side_effects[name]()
        if name in self.check_results and self.check_results[name]:
            success = self.check_results[name].pop(0)
        else:
            success = self.check_success
        error_msg = "" if success else f"{name} failed"
        return ToolResult(success=success, output=error_msg, error=error_msg)

    def is_build_config_file(self, path: str) -> bool:
        return path in self.build_config_files

    def is_sync_adoptable_file(self, path: str) -> bool:
        return path in self.sync_adoptable_files

    def is_ephemeral_build_path(self, path: str) -> bool:
        return any(part in self.ephemeral_build_components for part in Path(path).parts)


class RetryReportingAgent:
    def __init__(self) -> None:
        self.llm = StubLLMClient()

    def run(self, state: TaskState) -> AgentResult:
        self.llm._retry_observer(
            {
                "provider": "codex",
                "model": "gpt-5.3-codex",
                "operation": "run_agent",
                "attempt": 1,
                "max_attempts": 4,
                "delay_s": 30,
                "error": "temporary provider failure",
                "error_type": "RuntimeError",
            }
        )
        state.record("test_writer", "test_write", "files changed: []")
        return AgentResult(success=True, message="ok", data={"files_written": []})


class UsageReportingAgent:
    def __init__(self) -> None:
        self.llm = StubLLMClient()

    def run(self, state: TaskState) -> AgentResult:
        self.llm._usage_observer(
            {
                "provider": "codex",
                "model": "gpt-5.3-codex",
                "operation": "run_agent",
                "attempt": 1,
                "max_attempts": 4,
                "outcome": "success",
                "elapsed_s": 1.25,
                "input_chars": 200,
                "output_chars": 20,
                "reported_tokens": {"input_tokens": 50, "output_tokens": 5},
                "prompt": "PRIVATE_PROMPT",
            }
        )
        state.record("implementer", "implement", "done")
        return AgentResult(success=True, message="ok", data={"files_written": []})


class ProviderCallingAgent:
    def __init__(self, llm, *, calls_per_run: int = 1, swallow_provider_errors: bool = False) -> None:
        self.llm = llm
        self.calls_per_run = calls_per_run
        self.swallow_provider_errors = swallow_provider_errors
        self.calls: list[TaskState] = []

    def delivery_scope_active_test_write_paths(self, state: TaskState) -> list[str]:
        return []

    def run(self, state: TaskState) -> AgentResult:
        self.calls.append(state)
        for _ in range(self.calls_per_run):
            try:
                self.llm.run_agent("write files", Path(state.worktree_path or self.llm.cwd))
            except RuntimeError:
                if not self.swallow_provider_errors:
                    raise
        return AgentResult(success=True, message="ok", data={"files_written": []})


class ObservedWriteClient(StubLLMClient):
    def __init__(self, cwd: Path, actions: list[Callable[[], tuple[list[str], str]]]) -> None:
        super().__init__()
        self.cwd = cwd
        self.actions = actions
        self.provider_call_requests = 0
        self.provider_calls = 0
        self._config = LLMConfig(provider="test-provider", model="test-model")

    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        action = self.actions[self.provider_call_requests]
        self.provider_call_requests += 1

        def invoke() -> tuple[list[str], str]:
            self.provider_calls += 1
            return action()

        return llm_client_module._call_observed(
            self._config,
            operation="run_agent",
            attempt=1,
            max_attempts=1,
            input_chars=len(prompt),
            fn=invoke,
        )


class RetryingObservedWriteClient(ObservedWriteClient):
    def run_agent(self, prompt: str, cwd: Path) -> tuple[list[str], str]:
        last_error: LLMTransientError | None = None
        for attempt, action in enumerate(self.actions, start=1):
            self.provider_call_requests += 1

            def invoke() -> tuple[list[str], str]:
                self.provider_calls += 1
                return action()

            try:
                return llm_client_module._call_observed(
                    self._config,
                    operation="run_agent",
                    attempt=attempt,
                    max_attempts=len(self.actions),
                    input_chars=len(prompt),
                    fn=invoke,
                )
            except LLMTransientError as exc:
                last_error = exc
        assert last_error is not None
        raise last_error


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class TestPathClassification:
    def test_test_path_marker_requires_explicit_marker_or_separator(self):
        assert orchestrator_module._path_looks_like_test_artifact(
            "feature/countries/src/integrationTest/kotlin/CountriesModule.kt"
        )
        assert orchestrator_module._path_looks_like_test_artifact(
            "feature/countries/src/integration-test/kotlin/CountriesModule.kt"
        )
        assert not orchestrator_module._path_looks_like_test_artifact("feature/latest/src/main/App.ts")
        assert not orchestrator_module._path_looks_like_test_artifact("feature/contest/src/main/App.ts")

    def test_test_audit_candidates_include_source_files_for_inline_tests(self):
        assert orchestrator_module._path_looks_like_test_audit_candidate("src/lib.rs")
        assert orchestrator_module._path_looks_like_test_audit_candidate("src/main/kotlin/InlineTests.kt")
        assert orchestrator_module._path_looks_like_test_audit_candidate("tests/clientMain.test.ts")
        assert not orchestrator_module._path_looks_like_test_audit_candidate("assets/logo.png")


class TestDeliveryWriteScopeRuntimeConfig:
    def test_explicit_scope_replaces_only_production_write_policy(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["core", "agents"],
                "allowed_test_write_paths": ["tests"],
                "allowed_read_paths": ["."],
            },
        }
        state = TaskState(
            task_id="scope1",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="unit_explicit",
            delivery_declared_write_paths=["core/state.py"],
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["core/state.py"],
            delivery_effective_write_exact_file_paths=[],
        )

        apply_delivery_write_scope_to_config(project_config, state)

        assert project_config["sandbox"] == {
            "allowed_write_paths": ["core/state.py"],
            "allowed_test_write_paths": ["tests"],
            "allowed_read_paths": ["."],
        }

    def test_repository_default_uses_persisted_effective_scope(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["."],
                "allowed_test_write_paths": ["tests"],
            },
        }
        state = TaskState(
            task_id="scope2",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="repository_default",
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["persisted-production"],
            delivery_effective_write_exact_file_paths=[],
        )

        apply_delivery_write_scope_to_config(project_config, state)

        assert project_config["sandbox"]["allowed_write_paths"] == ["persisted-production"]
        assert project_config["sandbox"]["allowed_test_write_paths"] == ["tests"]

    def test_empty_persisted_scope_disables_runtime_production_writes(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["."]},
        }
        state = TaskState(
            task_id="scope-empty",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="repository_default",
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=[],
            delivery_effective_write_exact_file_paths=[],
        )

        runtime_paths = apply_delivery_write_scope_to_config(project_config, state)

        assert runtime_paths is not None
        assert runtime_paths.effective_paths == ()
        assert project_config["sandbox"]["allowed_write_paths"] == []

    def test_current_config_can_further_narrow_persisted_scope(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["core/state.py"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        state = TaskState(
            task_id="scope5",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="unit_explicit",
            delivery_declared_write_paths=["core"],
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["core"],
            delivery_effective_write_exact_file_paths=[],
        )

        apply_delivery_write_scope_to_config(project_config, state)

        assert project_config["sandbox"]["allowed_write_paths"] == ["core/state.py"]
        assert project_config["sandbox"]["allowed_test_write_paths"] == ["tests"]

    def test_existing_runtime_binding_rejects_retargeted_internal_scope_alias(self, tmp_path: Path):
        (tmp_path / "core").mkdir()
        (tmp_path / "docs").mkdir()
        alias = tmp_path / "scope-alias"
        alias.symlink_to("core", target_is_directory=True)
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["scope-alias"]},
        }
        state = TaskState(
            task_id="scope-retarget",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="repository_default",
            delivery_declared_write_paths=[],
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["scope-alias"],
            delivery_effective_write_exact_file_paths=[],
            delivery_runtime_write_scope_binding={
                "schema_version": 1,
                "status": "bound",
                "roots": [{"path": "scope-alias", "resolved_path": "core", "exact_file": False}],
            },
        )
        alias.unlink()
        alias.symlink_to("docs", target_is_directory=True)

        with pytest.raises(DeliveryWriteScopeError) as exc_info:
            apply_delivery_write_scope_to_config(project_config, state)

        assert exc_info.value.code == "delivery_write_scope.runtime_binding_changed"

    def test_existing_runtime_binding_remains_the_upper_bound_when_config_broadens(self, tmp_path: Path):
        (tmp_path / "core").mkdir()
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["."]},
        }
        binding = {
            "schema_version": 1,
            "status": "bound",
            "roots": [{"path": "core/state.py", "resolved_path": "core/state.py", "exact_file": False}],
        }
        state = TaskState(
            task_id="scope-broaden",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="unit_explicit",
            delivery_declared_write_paths=["core"],
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["core"],
            delivery_effective_write_exact_file_paths=[],
            delivery_runtime_write_scope_binding=binding,
        )

        apply_delivery_write_scope_to_config(project_config, state)

        assert project_config["sandbox"]["allowed_write_paths"] == ["core/state.py"]
        assert state.delivery_runtime_write_scope_binding is binding

    def test_disjoint_current_config_fails_closed(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["agents"]},
        }
        state = TaskState(
            task_id="scope6",
            task_description="delivery child",
            delivery_plan_id="plan",
            delivery_unit_id="unit",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="unit_explicit",
            delivery_declared_write_paths=["core"],
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["core"],
            delivery_effective_write_exact_file_paths=[],
        )

        with pytest.raises(DeliveryWriteScopeError) as exc_info:
            apply_delivery_write_scope_to_config(project_config, state)

        assert exc_info.value.code == "delivery_write_scope.runtime_intersection_invalid"

    def test_legacy_state_keeps_current_config(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["legacy-production"]},
        }
        state = TaskState(task_id="scope3", task_description="legacy child")

        apply_delivery_write_scope_to_config(project_config, state)

        assert project_config["sandbox"]["allowed_write_paths"] == ["legacy-production"]

    def test_modern_scope_must_be_bound_to_delivery_child(self, tmp_path: Path):
        project_config = {
            "project": {"root_path": str(tmp_path), "build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["core"]},
        }
        state = TaskState(
            task_id="scope4",
            task_description="unbound state",
            delivery_write_scope_schema_version=2,
            delivery_write_scope_mode="unit_explicit",
            delivery_declared_write_paths=["core"],
            delivery_declared_write_exact_file_paths=[],
            delivery_effective_write_paths=["core"],
            delivery_effective_write_exact_file_paths=[],
        )

        with pytest.raises(DeliveryWriteScopeError) as exc_info:
            apply_delivery_write_scope_to_config(project_config, state)

        assert exc_info.value.code == "delivery_write_scope.snapshot_unbound"


def _make_orchestrator(
    tmp_path: Path,
    **config_kwargs,
) -> tuple[Orchestrator, dict[str, StubAgent], StubBuildTool]:
    project_config = config_kwargs.pop("project_config", {"project": {"build_tool": "python"}})
    allowed_write_paths = config_kwargs.pop("allowed_write_paths", ["."])
    config = OrchestratorConfig(
        project_root=tmp_path,
        allowed_write_paths=allowed_write_paths,
        allowed_read_paths=["."],
        project_config=project_config,
        **config_kwargs,
    )
    store = JsonStateStore(tmp_path / "state")
    orch = Orchestrator(config=config, llm=StubLLMClient(), state_store=store)

    stubs: dict[str, StubAgent] = {
        name: StubAgent(name=name)
        for name in ["analyst", "planner", "implementer", "reviewer", "security_reviewer", "test_writer", "fixer"]
    }

    # Default reviewer/security_reviewer stubs approve so that tests not focused on
    # review behaviour don't fail due to the "no decision" guard added in Bug 2 fix.
    # Tests that care about rejection override these side_effects explicitly.
    def _default_reviewer(state: TaskState) -> None:
        state.review_approved = True
        state.review_issues = []

    def _default_security_reviewer(state: TaskState) -> None:
        state.security_approved = True
        state.review_issues = []

    stubs["reviewer"].side_effect = _default_reviewer
    stubs["security_reviewer"].side_effect = _default_security_reviewer

    orch._agents = stubs  # type: ignore[assignment]

    build = StubBuildTool()
    orch._tools["build"] = build

    return orch, stubs, build


def _save_state(orch: Orchestrator, **kwargs) -> TaskState:
    state = TaskState(task_id="t1", task_description="test task", **kwargs)
    orch._store.save(state)
    return state


def _scoped_delivery_state(orch: Orchestrator, effective_paths: list[str]) -> TaskState:
    return _save_state(
        orch,
        implementation_prompt="implement scoped delivery unit",
        plan_decided=True,
        delivery_plan_id="delivery-plan",
        delivery_unit_id="scoped-unit",
        delivery_write_scope_schema_version=2,
        delivery_write_scope_mode="unit_explicit",
        delivery_declared_write_paths=list(effective_paths),
        delivery_declared_write_exact_file_paths=[],
        delivery_effective_write_paths=list(effective_paths),
        delivery_effective_write_exact_file_paths=[],
    )


def _delivery_disposition(disposition: str) -> DeliveryDisposition:
    actions = {
        DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP: "external_dependency_follow_up",
        DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT: "delivery_amend_prepare",
    }
    return DeliveryDisposition(
        schema_version=DELIVERY_DISPOSITION_SCHEMA_VERSION,
        disposition=disposition,
        summary="The delivery unit cannot continue within its current boundary.",
        recommended_action=actions[disposition],
    )


class TestDeliveryProductionScopeAudit:
    def test_presync_out_of_scope_write_is_terminal_before_analysis(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "generated" / "api.py"
        orch, _, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def generate_outside_scope() -> None:
            escaped.parent.mkdir()
            escaped.write_text("generated outside scope\n", encoding="utf-8")

        build.presync_side_effect = generate_outside_scope
        state = _scoped_delivery_state(orch, ["src/allowed"])

        orch._run_presync(state)

        assert build.presync_calls == 1
        assert escaped.exists()
        assert state.failed is True
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.presync_done is True
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["audit_boundary"] == "tool_mutation"
        assert audit["metadata"]["tool_phase"] == "presync"
        assert audit["metadata"]["violation_paths"] == ["generated/api.py"]

    def test_sync_cannot_adopt_tracked_output_outside_unit_scope(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        lockfile = tmp_project / "Cargo.lock"
        lockfile.write_text("version = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "Cargo.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "track lockfile"], cwd=tmp_project, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        build.sync_adoptable_files.add("Cargo.lock")
        build.sync_side_effect = lambda: lockfile.write_text("version = 2\n", encoding="utf-8")
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._sync(state)

        assert result is False
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.files_changed == []
        assert lockfile.read_text(encoding="utf-8") == "version = 2\n"
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["tool_phase"] == "sync"
        assert audit["metadata"]["violation_paths"] == ["Cargo.lock"]

    def test_sync_can_adopt_tracked_output_inside_unit_scope(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        generated = allowed / "generated.py"
        generated.write_text("version = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "src/allowed/generated.py"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "track generated source"], cwd=tmp_project, check=True, capture_output=True
        )
        orch, _, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        build.sync_adoptable_files.add("src/allowed/generated.py")
        build.sync_side_effect = lambda: generated.write_text("version = 2\n", encoding="utf-8")
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._sync(state)

        assert result is True
        assert state.delivery_stop_code is None
        assert state.files_changed == ["src/allowed/generated.py"]
        audits = [record for record in state.validation_cycle_records if record.get("phase") == "delivery_scope_audit"]
        assert audits[-1]["status"] == "passed"
        assert audits[-1]["metadata"]["tool_phase"] == "sync"

    def test_sync_ignores_declared_ephemeral_dependency_tree_outside_unit_scope(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        dependency_file = tmp_project / "node_modules" / "package" / "index.js"
        (tmp_project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        orch, _, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        build.ephemeral_build_components.add("node_modules")

        def install_dependencies() -> None:
            dependency_file.parent.mkdir(parents=True)
            dependency_file.write_text("module.exports = {}\n", encoding="utf-8")

        build.sync_side_effect = install_dependencies
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._sync(state)

        assert result is True
        assert dependency_file.exists()
        assert state.delivery_stop_code is None
        audit = next(
            record for record in reversed(state.validation_cycle_records) if record["phase"] == "delivery_scope_audit"
        )
        assert audit["status"] == "passed"
        assert audit["metadata"]["changed_paths"] == []

    def test_node_sync_ignores_yarn_berry_disposable_install_state(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        (tmp_project / ".gitignore").write_text(
            ".yarn/install-state.gz\n.yarn/cache/\n.yarn/unplugged/\n",
            encoding="utf-8",
        )
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        node_tool = NodeTool(
            sandbox=Sandbox(project_root=tmp_project, allowed_write_paths=["."], allowed_read_paths=["."]),
            project_root=tmp_project,
        )

        def install_dependencies() -> ToolResult:
            for path in (
                tmp_project / ".yarn" / "install-state.gz",
                tmp_project / ".yarn" / "cache" / "package.zip",
                tmp_project / ".yarn" / "unplugged" / "package" / "index.js",
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("disposable\n", encoding="utf-8")
            return ToolResult(success=True, output="")

        node_tool.sync = install_dependencies  # type: ignore[method-assign]
        orch._tools["build"] = node_tool
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._sync(state)

        assert result is True
        assert state.delivery_stop_code is None
        audit = next(
            record for record in reversed(state.validation_cycle_records) if record["phase"] == "delivery_scope_audit"
        )
        assert audit["status"] == "passed"
        assert audit["metadata"]["changed_paths"] == []

    def test_node_sync_keeps_tracked_yarn_zero_install_cache_auditable(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        cache_file = tmp_project / ".yarn" / "cache" / "zero-install.zip"
        cache_file.parent.mkdir(parents=True)
        cache_file.write_text("tracked before\n", encoding="utf-8")
        (tmp_project / ".gitignore").write_text(".yarn/cache/\n", encoding="utf-8")
        subprocess.run(
            ["git", "add", ".gitignore"],
            cwd=tmp_project,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "add", "-f", ".yarn/cache/zero-install.zip"],
            cwd=tmp_project,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "track zero-install cache"],
            cwd=tmp_project,
            check=True,
            capture_output=True,
        )
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        node_tool = NodeTool(
            sandbox=Sandbox(project_root=tmp_project, allowed_write_paths=["."], allowed_read_paths=["."]),
            project_root=tmp_project,
        )

        def update_zero_install_cache() -> ToolResult:
            cache_file.write_text("tracked after\n", encoding="utf-8")
            return ToolResult(success=True, output="")

        node_tool.sync = update_zero_install_cache  # type: ignore[method-assign]
        orch._tools["build"] = node_tool
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._sync(state)

        assert result is False
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == [".yarn/cache/zero-install.zip"]

    def test_check_autofix_out_of_scope_write_stops_before_recheck(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "src" / "main.py"
        orch, _, build = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            run_checks=True,
            project_config={
                "project": {"build_tool": "python"},
                "build": {
                    "checks": [
                        {
                            "name": "ruff",
                            "command": "ruff check .",
                            "fix_command": "ruff check . --fix",
                        }
                    ]
                },
            },
        )
        build.check_results["ruff"] = [False, True]
        build.check_side_effects["ruff_autofix"] = lambda: escaped.write_text(
            "# changed outside scope\n",
            encoding="utf-8",
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_checks(state, "1/1")

        assert result is False
        assert build.check_calls == ["ruff", "ruff_autofix"]
        assert state.delivery_stop_code == "unit_scope_violation"
        assert escaped.read_text(encoding="utf-8") == "# changed outside scope\n"
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["tool_phase"] == "check_autofix"
        assert audit["metadata"]["violation_paths"] == ["src/main.py"]

    def test_tool_scope_snapshot_failure_blocks_mutation_call(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        monkeypatch.setattr(
            delivery_scope_audit_module,
            "snapshot_delivery_scope_files",
            lambda *args, **kwargs: (_ for _ in ()).throw(DeliveryScopeSnapshotError("unavailable")),
        )

        result = orch._sync(state)

        assert result is False
        assert build.sync_calls == 0
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"] == {
            "code": "delivery_scope_audit_unavailable",
            "agent": "tool:sync",
            "audit_boundary": "tool_mutation",
            "tool_phase": "sync",
        }

    def test_interrupted_tool_mutation_is_audited_before_pipeline_resume(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        escaped = tmp_project / "Cargo.lock"
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        policy = orch._delivery_scope_audit_policy(state, "tool:sync", active_test_write_paths=())
        orch._set_delivery_scope_audit_pending(state, "tool:sync", policy=policy)
        orch._delivery_scope_audit_snapshot(state, "tool:sync", policy=policy)
        escaped.write_text("partial interrupted sync output\n", encoding="utf-8")
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.delivery_scope_audit_pending is None
        assert pipeline_calls == []
        assert not any(stub.calls for stub in stubs.values())
        audit = result.validation_cycle_records[-1]
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["audit_boundary"] == "tool_mutation"
        assert audit["metadata"]["tool_phase"] == "sync"
        assert audit["metadata"]["violation_paths"] == ["Cargo.lock"]

    def test_provider_workspace_setup_runs_before_scope_baseline(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        settings = tmp_project / ".gemini" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("readonly\n", encoding="utf-8")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        class PreparingLLM(StubLLMClient):
            def prepare_write_agent_workspace(self, cwd: Path) -> None:
                assert cwd == tmp_project
                settings.write_text("implementer\n", encoding="utf-8")

        def write_in_scope(_state: TaskState) -> None:
            assert settings.read_text(encoding="utf-8") == "implementer\n"
            settings.write_text("implementer\n", encoding="utf-8")
            (allowed / "kept.py").write_text("in scope\n", encoding="utf-8")

        stubs["implementer"].llm = PreparingLLM()
        stubs["implementer"].side_effect = write_in_scope
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is True
        audit = state.validation_cycle_records[-1]
        assert audit["status"] == "passed"
        assert audit["metadata"]["changed_paths"] == ["src/allowed/kept.py"]
        assert audit["metadata"]["violation_count"] == 0

    def test_provider_workspace_setup_failure_blocks_agent(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        class FailingWorkspaceLLM(StubLLMClient):
            def prepare_write_agent_workspace(self, cwd: Path) -> None:
                raise OSError("settings directory is read-only")

        stubs["implementer"].llm = FailingWorkspaceLLM()
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "provider workspace setup failed: settings directory is read-only"
        assert stubs["implementer"].calls == []
        assert state.failed is True
        assert state.history[-1]["action"] == "workspace_setup_failed"
        persisted = orch._store.load(state.task_id)
        assert persisted is not None
        assert persisted.history[-1]["action"] == "workspace_setup_failed"

    def test_non_os_workspace_setup_failure_is_recorded_and_saved(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        class FailingWorkspaceLLM(StubLLMClient):
            def prepare_write_agent_workspace(self, cwd: Path) -> None:
                raise RuntimeError("provider setup hook failed")

        stubs["implementer"].llm = FailingWorkspaceLLM()
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "provider workspace setup failed: provider setup hook failed"
        assert stubs["implementer"].calls == []
        assert state.failed is True
        persisted = orch._store.load(state.task_id)
        assert persisted is not None
        assert persisted.failed is True

    def test_each_provider_call_is_audited_before_fixer_can_make_another(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        outside = tmp_project / "build" / "escaped.txt"
        second_call_started = False

        def first_call() -> tuple[list[str], str]:
            outside.parent.mkdir()
            outside.write_text("out of scope\n", encoding="utf-8")
            return [], "first"

        def second_call() -> tuple[list[str], str]:
            nonlocal second_call_started
            second_call_started = True
            outside.unlink()
            return [], "second"

        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        client = ObservedWriteClient(tmp_project, [first_call, second_call])
        agent = ProviderCallingAgent(client, calls_per_run=2, swallow_provider_errors=True)
        stubs["fixer"] = agent  # type: ignore[assignment]
        orch._agents["fixer"] = agent
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("fixer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert client.provider_calls == 1
        assert client.provider_call_requests == 2
        assert second_call_started is False
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["audit_boundary"] == "provider_attempt"
        assert audit["metadata"]["provider_attempt"] == 1
        assert audit["metadata"]["violation_paths"] == ["build/escaped.txt"]

    def test_scope_violation_stops_provider_internal_retry(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        outside = tmp_project / "ignored-output" / "escaped.txt"
        exclude = tmp_project / ".git" / "info" / "exclude"
        exclude.write_text(exclude.read_text(encoding="utf-8") + "\nignored-output/\n", encoding="utf-8")
        retry_started = False

        def first_attempt() -> tuple[list[str], str]:
            outside.parent.mkdir()
            outside.write_text("partial\n", encoding="utf-8")
            raise LLMTransientError("retry me")

        def retry_attempt() -> tuple[list[str], str]:
            nonlocal retry_started
            retry_started = True
            outside.unlink()
            return [], "recovered"

        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        client = RetryingObservedWriteClient(tmp_project, [first_attempt, retry_attempt])
        agent = ProviderCallingAgent(client)
        stubs["implementer"] = agent  # type: ignore[assignment]
        orch._agents["implementer"] = agent
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert client.provider_calls == 1
        assert retry_started is False
        assert state.validation_cycle_records[-1]["metadata"]["audit_boundary"] == "provider_attempt"

    def test_provider_cannot_hide_ephemeral_write_by_mutating_info_exclude(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        escaped = tmp_project / "target" / "escaped"

        def mutate_ignore_authority() -> tuple[list[str], str]:
            exclude = tmp_project / ".git" / "info" / "exclude"
            exclude.write_text(exclude.read_text(encoding="utf-8") + "\ntarget/\n", encoding="utf-8")
            escaped.parent.mkdir()
            escaped.write_text("out of scope\n", encoding="utf-8")
            return [], "hidden"

        orch, stubs, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        build.ephemeral_build_components.add("target")
        client = ObservedWriteClient(tmp_project, [mutate_ignore_authority])
        agent = ProviderCallingAgent(client)
        stubs["implementer"] = agent  # type: ignore[assignment]
        orch._agents["implementer"] = agent
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert client.provider_calls == 1
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    def test_clean_transient_provider_attempt_is_audited_before_retry(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()

        def transient_attempt() -> tuple[list[str], str]:
            raise LLMTransientError("retry me")

        def successful_attempt() -> tuple[list[str], str]:
            (allowed / "kept.py").write_text("in scope\n", encoding="utf-8")
            return ["src/allowed/kept.py"], "done"

        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        client = RetryingObservedWriteClient(tmp_project, [transient_attempt, successful_attempt])
        agent = ProviderCallingAgent(client)
        stubs["implementer"] = agent  # type: ignore[assignment]
        orch._agents["implementer"] = agent
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is True
        assert client.provider_calls == 2
        attempt_audits = [
            record
            for record in state.validation_cycle_records
            if record.get("metadata", {}).get("audit_boundary") == "provider_attempt"
        ]
        assert [record["status"] for record in attempt_audits] == ["passed", "passed"]
        assert [record["metadata"]["provider_attempt"] for record in attempt_audits] == [1, 2]

    def test_provider_attempt_snapshot_failure_blocks_provider_execution(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        client = ObservedWriteClient(tmp_project, [lambda: ([], "must not run")])
        agent = ProviderCallingAgent(client)
        stubs["implementer"] = agent  # type: ignore[assignment]
        orch._agents["implementer"] = agent
        state = _scoped_delivery_state(orch, ["src/allowed"])
        original_snapshot = delivery_scope_audit_module.snapshot_delivery_scope_files
        calls = 0

        def fail_attempt_baseline(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DeliveryScopeSnapshotError("unavailable")
            return original_snapshot(*args, **kwargs)

        monkeypatch.setattr(delivery_scope_audit_module, "snapshot_delivery_scope_files", fail_attempt_baseline)

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert client.provider_call_requests == 1
        assert client.provider_calls == 0
        assert state.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    def test_scope_audit_rejects_project_outside_authoritative_worktree(self, tmp_path: Path):
        project = tmp_path / "project"
        worktree = tmp_path / "unrelated-worktree"
        (project / "src" / "allowed").mkdir(parents=True)
        worktree.mkdir()
        orch, stubs, _ = _make_orchestrator(project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.worktree_base = str(worktree)
        state.worktree_path = str(project)

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "delivery_scope_audit_unavailable"
        assert stubs["implementer"].calls == []
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_scope_audit_covers_siblings_of_nested_project_root(self, tmp_path: Path):
        repository = tmp_path / "repository"
        project = repository / "packages" / "app"
        allowed = project / "src" / "allowed"
        sibling = repository / "shared" / "escaped.py"
        allowed.mkdir(parents=True)
        sibling.parent.mkdir(parents=True)
        subprocess.run(["git", "init"], cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=repository, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repository, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repository, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "init"], cwd=repository, check=True, capture_output=True
        )
        orch, stubs, _ = _make_orchestrator(project, allowed_write_paths=["src/allowed"])

        def write_across_project_boundary(_state: TaskState) -> None:
            (allowed / "kept.py").write_text("in scope\n", encoding="utf-8")
            sibling.write_text("outside nested project\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_across_project_boundary
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.worktree_base = str(repository)
        state.worktree_path = str(project)

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert state.delivery_stop_code == "unit_scope_violation"
        audit = state.validation_cycle_records[-1]
        assert audit["status"] == "failed"
        assert audit["metadata"]["changed_paths"] == ["src/allowed/kept.py"]
        assert audit["metadata"]["outside_project_count"] == 1
        assert audit["metadata"]["outside_project_paths"] == ["shared/escaped.py"]
        assert audit["metadata"]["violation_paths"] == []
        assert audit["metadata"]["violation_count"] == 1

    def test_broad_test_root_does_not_retain_generated_content(self, tmp_project: Path):
        generated = tmp_project / "app" / "build" / "generated.bin"
        generated.parent.mkdir(parents=True)
        generated.write_bytes(b"generated" * 1024)
        project_config = {
            "project": {"build_tool": "gradle-android"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["app/"],
            },
        }
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])

        snapshot = orch._delivery_scope_audit_snapshot(state, "fixer")

        assert snapshot is not None
        assert snapshot["app/build/generated.bin"].content is None

    def test_scope_audit_finishes_before_agent_active_operation_clears(
        self,
        tmp_project: Path,
        monkeypatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=60,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])
        original_audit = orch._audit_delivery_scope_after_mutation

        def assert_active_operation(*args, **kwargs) -> bool:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "agent"
            assert loaded.active_operation["agent"] == "implementer"
            return original_audit(*args, **kwargs)

        monkeypatch.setattr(orch, "_audit_delivery_scope_after_mutation", assert_active_operation)

        result = orch._run_agent("implementer", state)

        loaded = orch._store.load(state.task_id)
        assert result.success is True
        assert loaded is not None
        assert loaded.active_operation is None

    @pytest.mark.parametrize("recovery_phase", ["load", "compare", "save"])
    @pytest.mark.parametrize("exception_type", [KeyboardInterrupt, SystemExit, BaseException])
    def test_interrupted_scope_recovery_preserves_pending_evidence(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        recovery_phase: str,
        exception_type: type[BaseException],
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        policy = orch._delivery_scope_audit_policy(state, "implementer")
        orch._set_delivery_scope_audit_pending(state, "implementer", policy=policy)
        orch._delivery_scope_audit_snapshot(state, "implementer", policy=policy)
        pending_before = deepcopy(state.delivery_scope_audit_pending)
        snapshot_before = orch._store.load_text_snapshot(state.task_id, "delivery_scope_audit_before")
        original_load = orch._store.load_text_snapshot
        original_detect = delivery_scope_audit_module.detect_validation_artifacts
        original_save = orch._store.save
        interrupted = False

        def interrupt_load(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise exception_type()
            return original_load(*args, **kwargs)

        def interrupt_compare(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise exception_type()
            return original_detect(*args, **kwargs)

        def interrupt_save(*args, **kwargs):
            nonlocal interrupted
            if not interrupted:
                interrupted = True
                raise exception_type()
            return original_save(*args, **kwargs)

        if recovery_phase == "load":
            monkeypatch.setattr(orch._store, "load_text_snapshot", interrupt_load)
        elif recovery_phase == "compare":
            monkeypatch.setattr(delivery_scope_audit_module, "detect_validation_artifacts", interrupt_compare)
        else:
            monkeypatch.setattr(orch._store, "save", interrupt_save)

        with pytest.raises(exception_type):
            orch.recover_interrupted_delivery_scope(state.task_id)

        preserved = orch._store.load(state.task_id)
        assert preserved is not None
        assert preserved.delivery_scope_audit_pending == pending_before
        assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_audit_before") == snapshot_before
        assert not any(record.get("phase") == "delivery_scope_audit" for record in preserved.validation_cycle_records)

        recovered = orch.recover_interrupted_delivery_scope(state.task_id)

        assert recovered.failed is False
        assert recovered.delivery_scope_audit_pending is None
        assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_audit_before") is None
        audit = recovered.validation_cycle_records[-1]
        assert audit["status"] == "passed"
        assert audit["metadata"]["resume_recovery"] is True

    @pytest.mark.parametrize("agent_name", ["implementer", "fixer"])
    def test_provider_report_cannot_hide_partial_out_of_scope_writes(
        self,
        tmp_project: Path,
        agent_name: str,
    ):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )

        def write_outside_scope(_state: TaskState) -> None:
            (allowed / "kept.py").write_text("in scope\n", encoding="utf-8")
            outside = tmp_project / "docs" / "escaped.md"
            outside.parent.mkdir()
            outside.write_text("outside scope\n", encoding="utf-8")

        stubs[agent_name].side_effect = write_outside_scope
        stubs[agent_name].result_data = {"files_written": []}
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent(agent_name, state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.failed is True
        assert state.delivery_stop_code == "unit_scope_violation"
        assert (allowed / "kept.py").exists()
        assert (tmp_project / "docs" / "escaped.md").exists()
        audit = state.validation_cycle_records[-1]
        assert audit["phase"] == "delivery_scope_audit"
        assert audit["status"] == "failed"
        assert audit["metadata"] == {
            "code": "unit_scope_violation",
            "agent": agent_name,
            "changed_count": 2,
            "production_changed_count": 2,
            "violation_count": 1,
            "declared_count": 1,
            "effective_count": 1,
            "changed_paths": ["docs/escaped.md", "src/allowed/kept.py"],
            "violation_paths": ["docs/escaped.md"],
            "declared_paths": ["src/allowed"],
            "effective_paths": ["src/allowed"],
            "recommended_action": "delivery_amend_prepare",
        }

    def test_provider_commit_changes_git_authority_and_fails_closed(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "docs" / "escaped.md"
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def commit_outside_scope(_state: TaskState) -> None:
            escaped.parent.mkdir()
            escaped.write_text("committed outside scope\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_project, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "provider commit"],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )
            (allowed / "dirty.py").write_text("dirty in scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = commit_outside_scope
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.delivery_stop_code == "unit_scope_violation"
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"] == {
            "code": "delivery_scope_audit_unavailable",
            "agent": "implementer",
        }

    def test_provider_commit_and_revert_changes_git_authority_and_fails_closed(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "docs" / "escaped.md"
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def commit_and_revert_outside_scope(_state: TaskState) -> None:
            escaped.parent.mkdir()
            escaped.write_text("committed outside scope\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_project, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "provider commit"],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "rm", "docs/escaped.md"], cwd=tmp_project, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "provider revert"],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )
            (allowed / "dirty.py").write_text("dirty in scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = commit_and_revert_outside_scope
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"] == {
            "code": "delivery_scope_audit_unavailable",
            "agent": "implementer",
        }

    def test_provider_commit_and_hard_reset_to_baseline_fails_closed(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "docs" / "discarded.md"
        baseline = _git_head(tmp_project)
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def commit_and_discard_outside_scope(_state: TaskState) -> None:
            escaped.parent.mkdir()
            escaped.write_text("discarded outside scope\n", encoding="utf-8")
            subprocess.run(["git", "add", "docs/discarded.md"], cwd=tmp_project, check=True, capture_output=True)
            subprocess.run(
                ["git", "commit", "-m", "discarded provider commit"],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "reset", "--hard", baseline], cwd=tmp_project, check=True, capture_output=True)

        stubs["implementer"].side_effect = commit_and_discard_outside_scope
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert len(stubs["implementer"].calls) == 1
        assert not escaped.exists()
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"] == {
            "code": "delivery_scope_audit_unavailable",
            "agent": "implementer",
        }

    def test_provider_index_flag_cannot_hide_out_of_scope_write(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "docs" / "escaped.md"
        escaped.parent.mkdir()
        escaped.write_text("before\n", encoding="utf-8")
        subprocess.run(["git", "add", "docs/escaped.md"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "track escaped path"], cwd=tmp_project, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def hide_outside_scope(_state: TaskState) -> None:
            subprocess.run(
                ["git", "update-index", "--assume-unchanged", "docs/escaped.md"],
                cwd=tmp_project,
                check=True,
                capture_output=True,
            )
            escaped.write_text("hidden outside scope\n", encoding="utf-8")
            (allowed / "dirty.py").write_text("dirty in scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = hide_outside_scope
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.delivery_stop_code == "unit_scope_violation"
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["changed_paths"] == ["docs/escaped.md", "src/allowed/dirty.py"]
        assert audit["metadata"]["violation_paths"] == ["docs/escaped.md"]

    @pytest.mark.parametrize("agent_name", ["implementer", "fixer"])
    def test_gitignored_out_of_scope_write_is_a_terminal_violation(
        self,
        tmp_project: Path,
        agent_name: str,
    ):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        (tmp_project / ".gitignore").write_text(".env\n", encoding="utf-8")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def write_ignored_file(_state: TaskState) -> None:
            (tmp_project / ".env").write_text("PRIVATE_CONFIGURATION=changed\n", encoding="utf-8")

        stubs[agent_name].side_effect = write_ignored_file
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent(agent_name, state)

        assert result.success is False
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == [".env"]

    def test_existing_nested_symlink_cannot_escape_active_write_scope(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        outside = tmp_project.parent / f"{tmp_project.name}-outside-symlink"
        outside.mkdir()
        try:
            (allowed / "link").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "delivery_scope_audit_unavailable"
        assert stubs["implementer"].calls == []
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_existing_out_of_scope_symlink_cannot_escape_project(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        out_of_scope = tmp_project / "docs"
        out_of_scope.mkdir()
        outside = tmp_project.parent / f"{tmp_project.name}-outside-project"
        outside.mkdir()
        try:
            (out_of_scope / "link").symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def write_through_out_of_scope_symlink(_state: TaskState) -> None:
            (out_of_scope / "link" / "escaped.txt").write_text("outside project\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_through_out_of_scope_symlink
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "delivery_scope_audit_unavailable"
        assert stubs["implementer"].calls == []
        assert not (outside / "escaped.txt").exists()
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_new_nested_symlink_escape_stops_after_provider(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        outside = tmp_project.parent / f"{tmp_project.name}-new-outside-symlink"
        outside.mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def create_escaping_symlink(_state: TaskState) -> None:
            try:
                (allowed / "link").symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")

        stubs["implementer"].side_effect = create_escaping_symlink
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert len(stubs["implementer"].calls) == 1
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_ephemeral_tree_symlink_escape_stops_after_provider(self, tmp_project: Path):
        (tmp_project / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        outside = tmp_project.parent / f"{tmp_project.name}-ephemeral-symlink"
        outside.mkdir()
        orch, stubs, build = _make_orchestrator(tmp_project, allowed_write_paths=["."])
        build.ephemeral_build_components.add("node_modules")

        def write_through_ephemeral_symlink(_state: TaskState) -> None:
            dependency_root = tmp_project / "node_modules"
            dependency_root.mkdir()
            link = dependency_root / "escape"
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"symlink creation is unavailable: {exc}")
            (link / "escaped.txt").write_text("outside project\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_through_ephemeral_symlink
        state = _scoped_delivery_state(orch, ["."])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert len(stubs["implementer"].calls) == 1
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_nested_symlink_cannot_escape_to_another_project_path(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        forbidden = tmp_project / "src" / "forbidden"
        allowed.mkdir()
        forbidden.mkdir()
        try:
            (allowed / "link").symlink_to("../forbidden", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert stubs["implementer"].calls == []
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_nested_symlink_within_active_write_scope_is_allowed(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        target = allowed / "real"
        target.mkdir(parents=True)
        try:
            (allowed / "link").symlink_to("real", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])

        def write_through_safe_symlink(_state: TaskState) -> None:
            (allowed / "link" / "kept.py").write_text("in scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_through_safe_symlink
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is True
        assert state.validation_cycle_records[-1]["metadata"]["violation_count"] == 0

    def test_internal_symlink_scope_root_allows_writes_to_resolved_target(self, tmp_project: Path):
        target = tmp_project / "core"
        target.mkdir()
        alias = tmp_project / "core-alias"
        try:
            alias.symlink_to("core", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["core-alias"])

        def write_through_scope_alias(_state: TaskState) -> None:
            (alias / "kept.py").write_text("in scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_through_scope_alias
        state = _scoped_delivery_state(orch, ["core-alias"])

        result = orch._run_agent("implementer", state)

        assert result.success is True
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["changed_paths"] == ["core/kept.py"]
        assert audit["metadata"]["violation_count"] == 0

    def test_internal_symlink_scope_root_retarget_is_rejected(self, tmp_project: Path):
        (tmp_project / "core").mkdir()
        (tmp_project / "other").mkdir()
        alias = tmp_project / "core-alias"
        try:
            alias.symlink_to("core", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["core-alias"])

        def retarget_scope_alias(_state: TaskState) -> None:
            alias.unlink()
            alias.symlink_to("other", target_is_directory=True)

        stubs["implementer"].side_effect = retarget_scope_alias
        state = _scoped_delivery_state(orch, ["core-alias"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_internal_symlink_scope_root_retarget_before_baseline_is_rejected(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_project / "core").mkdir()
        (tmp_project / "other").mkdir()
        alias = tmp_project / "core-alias"
        try:
            alias.symlink_to("core", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["core-alias"])
        original_set_pending = orch._set_delivery_scope_audit_pending

        def set_pending_then_retarget(state: TaskState, name: str, *, policy=None) -> None:
            original_set_pending(state, name, policy=policy)
            alias.unlink()
            alias.symlink_to("other", target_is_directory=True)

        monkeypatch.setattr(orch, "_set_delivery_scope_audit_pending", set_pending_then_retarget)
        state = _scoped_delivery_state(orch, ["core-alias"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "delivery_scope_audit_unavailable"
        assert stubs["implementer"].calls == []
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_internal_symlink_scope_root_replaced_by_directory_is_rejected(self, tmp_project: Path):
        (tmp_project / "core").mkdir()
        alias = tmp_project / "core-alias"
        try:
            alias.symlink_to("core", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["core-alias"])

        def replace_scope_alias(_state: TaskState) -> None:
            alias.unlink()
            alias.mkdir()
            (alias / "new.py").write_text("outside resolved scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = replace_scope_alias
        state = _scoped_delivery_state(orch, ["core-alias"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == [
            "core-alias",
            "core-alias/new.py",
        ]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX treats backslashes as filename characters")
    def test_posix_backslash_filename_does_not_inherit_directory_scope(self, tmp_project: Path) -> None:
        (tmp_project / "scripts").mkdir()
        escaped = tmp_project / r"scripts\payload"
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["scripts"])

        def write_backslash_name(_state: TaskState) -> None:
            escaped.write_text("outside scripts directory\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_backslash_name
        state = _scoped_delivery_state(orch, ["scripts"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == [r"scripts\payload"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX treats backslashes as filename characters")
    def test_posix_backslash_scope_file_remains_exact_in_audit_policy(self, tmp_project: Path) -> None:
        scoped_file = tmp_project / r"scope\name"
        scoped_file.write_text("allowed exact file\n", encoding="utf-8")
        escaped = tmp_project / "scope" / "name" / "escaped.py"
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=[r"scope\name"])

        def write_descendant_of_rewritten_path(_state: TaskState) -> None:
            escaped.parent.mkdir(parents=True)
            escaped.write_text("must remain outside exact scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_descendant_of_rewritten_path
        state = _scoped_delivery_state(orch, [r"scope\name"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["effective_paths"] == [r"scope\name"]
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == ["scope/name/escaped.py"]

    @pytest.mark.skipif(os.name == "nt", reason="POSIX treats backslashes as filename characters")
    def test_ignored_posix_backslash_filename_is_not_pruned_as_node_output(self, tmp_project: Path) -> None:
        (tmp_project / "src" / "allowed").mkdir(parents=True)
        (tmp_project / ".gitignore").write_text("outside*\n", encoding="utf-8")
        escaped = tmp_project / r"outside\node_modules\payload"
        orch, stubs, build = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        node_tool = object.__new__(NodeTool)
        build.is_ephemeral_build_path = node_tool.is_ephemeral_build_path  # type: ignore[method-assign]

        def write_backslash_name(_state: TaskState) -> None:
            escaped.write_text("ignored outside scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = write_backslash_name
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == [r"outside\node_modules\payload"]

    def test_project_root_symlink_is_allowed_by_repository_wide_scope(self, tmp_project: Path):
        try:
            (tmp_project / "root-link").symlink_to(".", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["."])
        state = _scoped_delivery_state(orch, ["."])

        result = orch._run_agent("implementer", state)

        assert result.success is True
        assert state.validation_cycle_records[-1]["metadata"]["violation_count"] == 0

    def test_scope_audit_snapshot_failure_blocks_provider_execution(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        monkeypatch.setattr(
            delivery_scope_audit_module,
            "snapshot_delivery_scope_files",
            lambda *args, **kwargs: (_ for _ in ()).throw(DeliveryScopeSnapshotError("unavailable")),
        )

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert result.message == "delivery_scope_audit_unavailable"
        assert stubs["implementer"].calls == []
        assert state.delivery_stop_code == "unit_scope_violation"

    def test_scope_audit_post_snapshot_failure_stops_after_provider(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        original_snapshot = delivery_scope_audit_module.snapshot_delivery_scope_files
        calls = 0

        def fail_second_snapshot(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise DeliveryScopeSnapshotError("unavailable")
            return original_snapshot(*args, **kwargs)

        monkeypatch.setattr(delivery_scope_audit_module, "snapshot_delivery_scope_files", fail_second_snapshot)

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert len(stubs["implementer"].calls) == 1
        assert state.delivery_stop_code == "unit_scope_violation"

    @pytest.mark.parametrize("persisted", [None, {"broken": "not-json"}])
    def test_interrupted_scope_audit_fails_closed_without_valid_baseline(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        persisted: dict[str, str] | None,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.delivery_scope_audit_pending = {"schema_version": 1, "agent": "implementer"}
        if persisted is not None:
            orch._store.save_text_snapshot(state.task_id, "delivery_scope_audit_before", persisted)
        state.start_active_operation("agent", agent="implementer", message="interrupted")
        orch._store.save(state)
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert pipeline_calls == []
        assert not any(stub.calls for stub in stubs.values())
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"
        assert result.delivery_scope_audit_pending is None

    def test_resume_fails_closed_on_orphaned_scope_baseline(self, tmp_project: Path, monkeypatch):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert pipeline_calls == []
        assert not any(stub.calls for stub in stubs.values())
        assert result.validation_cycle_records[-1]["metadata"] == {
            "code": "delivery_scope_audit_unavailable",
            "agent": "unknown",
        }

    @pytest.mark.parametrize(
        "pending",
        [
            {"schema_version": True, "agent": "implementer"},
            {
                "schema_version": 4,
                "agent": "implementer",
                "project_prefix": ".",
                "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
                "active_test_write_paths": [],
            },
        ],
    )
    def test_resume_fails_closed_on_malformed_scope_pending(
        self,
        tmp_project: Path,
        monkeypatch,
        pending: dict,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.delivery_scope_audit_pending = pending
        orch._store.save(state)
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert pipeline_calls == []
        assert not any(stub.calls for stub in stubs.values())
        assert result.delivery_scope_audit_pending is None

    @pytest.mark.parametrize(
        "pending",
        [
            {
                "schema_version": 8,
                "agent": "implementer",
                "project_prefix": ".",
                "production_roots": [{"path": "src/allowed", "exact_file": False}],
                "active_test_write_paths": [],
            },
            {
                "schema_version": 8,
                "agent": "implementer",
                "project_prefix": ".",
                "production_roots": [{"path": "src/allowed", "resolved_path": "../outside", "exact_file": False}],
                "active_test_write_paths": [],
            },
            {
                "schema_version": 8,
                "agent": "implementer",
                "project_prefix": ".",
                "production_roots": [{"path": ".", "resolved_path": ".", "exact_file": True}],
                "active_test_write_paths": [],
            },
            {
                "schema_version": 8,
                "agent": "implementer",
                "project_prefix": ".",
                "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
                "active_test_write_paths": ["tests"],
            },
        ],
    )
    def test_resume_fails_closed_on_malformed_resolved_scope_policy(
        self,
        tmp_project: Path,
        pending: dict,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        pending.update(_git_scope_binding_fields(tmp_project))
        pending["snapshot_name"] = "delivery_scope_audit_before"
        state.delivery_scope_audit_pending = pending
        orch._store.save(state)

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    @pytest.mark.parametrize("git_baseline", [None, True, "a" * 39, "g" * 40, "a" * 40])
    def test_resume_fails_closed_on_invalid_git_baseline(
        self,
        tmp_project: Path,
        git_baseline: object,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.delivery_scope_audit_pending = {
            "schema_version": 8,
            "agent": "implementer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "git_baseline": git_baseline,
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._store.save(state)

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    @pytest.mark.parametrize("git_dir", [None, True, "", "relative/path", "/missing/sikula-git-dir"])
    def test_resume_fails_closed_on_invalid_git_directory_binding(
        self,
        tmp_project: Path,
        git_dir: object,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.delivery_scope_audit_pending = {
            "schema_version": 8,
            "agent": "implementer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "git_baseline": _git_head(tmp_project),
            "git_dir": git_dir,
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._store.save(state)

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    @pytest.mark.parametrize("git_common_dir", [None, True, "", "relative/path", "/missing/sikula-common-dir"])
    def test_resume_fails_closed_on_invalid_common_git_directory_binding(
        self,
        tmp_project: Path,
        git_common_dir: object,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.delivery_scope_audit_pending = {
            "schema_version": 8,
            "agent": "implementer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "git_common_dir": git_common_dir,
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._store.save(state)

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    @pytest.mark.parametrize("fingerprint", [None, True, "a" * 63, "g" * 64])
    def test_resume_fails_closed_on_invalid_git_ignore_binding(
        self,
        tmp_project: Path,
        fingerprint: object,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.delivery_scope_audit_pending = {
            "schema_version": 8,
            "agent": "implementer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "git_ignore_fingerprint": fingerprint,
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._store.save(state)

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    @pytest.mark.parametrize("fingerprint", [None, True, "a" * 63, "g" * 64])
    def test_resume_fails_closed_on_invalid_git_ref_binding(
        self,
        tmp_project: Path,
        fingerprint: object,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.delivery_scope_audit_pending = {
            "schema_version": 8,
            "agent": "implementer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "git_ref_fingerprint": fingerprint,
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._store.save(state)

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.validation_cycle_records[-1]["metadata"]["code"] == "delivery_scope_audit_unavailable"

    def test_active_operation_does_not_trigger_scope_recovery_without_control_state(
        self,
        tmp_project: Path,
        monkeypatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=60,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.start_active_operation("agent", agent="implementer", message="visibility only")
        orch._store.save(state)
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert pipeline_calls == [result]
        assert result.active_operation is None
        assert result.delivery_scope_audit_pending is None
        assert not any(record.get("phase") == "delivery_scope_audit" for record in result.validation_cycle_records)

    @pytest.mark.parametrize("agent_name", ["implementer", "fixer"])
    @pytest.mark.parametrize("heartbeat_interval_seconds", [0, 60])
    def test_keyboard_interrupt_preserves_scope_audit_for_resume(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
        agent_name: str,
        heartbeat_interval_seconds: int,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=heartbeat_interval_seconds,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])
        outside = tmp_project / "docs" / "interrupted.md"

        def write_then_interrupt(_state: TaskState) -> None:
            outside.parent.mkdir()
            outside.write_text("partial out-of-scope write\n", encoding="utf-8")
            raise KeyboardInterrupt

        stubs[agent_name].side_effect = write_then_interrupt

        with pytest.raises(KeyboardInterrupt):
            orch._run_agent(agent_name, state)

        interrupted = orch._store.load(state.task_id)
        assert interrupted is not None
        assert interrupted.active_operation is None
        assert interrupted.delivery_scope_audit_pending == {
            "schema_version": 8,
            "agent": agent_name,
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_audit_before") is not None
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.delivery_scope_audit_pending is None
        assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_audit_before") is None
        assert pipeline_calls == []
        assert len(stubs[agent_name].calls) == 1
        audit = result.validation_cycle_records[-1]
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["violation_paths"] == ["docs/interrupted.md"]

    def test_interrupted_scope_audit_uses_immutable_production_roots_after_config_broadens(
        self,
        tmp_project: Path,
    ):
        allowed = tmp_project / "src" / "a"
        allowed.mkdir(parents=True)
        original_orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/a"])
        state = _scoped_delivery_state(original_orch, ["src/a"])
        state.delivery_runtime_write_scope_binding = {
            "schema_version": 1,
            "status": "bound",
            "roots": [{"path": "src/a", "resolved_path": "src/a", "exact_file": False}],
        }
        policy = original_orch._delivery_scope_audit_policy(state, "implementer")
        original_orch._set_delivery_scope_audit_pending(state, "implementer", policy=policy)
        original_orch._delivery_scope_audit_snapshot(state, "implementer", policy=policy)
        escaped = tmp_project / "src" / "b" / "partial.py"
        escaped.parent.mkdir()
        escaped.write_text("partial write\n", encoding="utf-8")

        resumed_orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["."])
        result = resumed_orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        audit = result.validation_cycle_records[-1]
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["violation_paths"] == ["src/b/partial.py"]
        assert audit["metadata"]["effective_paths"] == ["src/a"]

    def test_interrupted_scope_audit_rejects_provider_git_ref_change(self, tmp_project: Path):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        escaped = tmp_project / "docs" / "committed.md"
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["src/allowed"])
        state = _scoped_delivery_state(orch, ["src/allowed"])
        policy = orch._delivery_scope_audit_policy(state, "implementer")
        orch._set_delivery_scope_audit_pending(state, "implementer", policy=policy)
        orch._delivery_scope_audit_snapshot(state, "implementer", policy=policy)
        escaped.parent.mkdir()
        escaped.write_text("committed outside scope\n", encoding="utf-8")
        subprocess.run(["git", "add", "docs/committed.md"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-m", "interrupted provider commit"],
            cwd=tmp_project,
            check=True,
            capture_output=True,
        )
        (allowed / "dirty.py").write_text("dirty in scope\n", encoding="utf-8")

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        audit = result.validation_cycle_records[-1]
        assert audit["metadata"] == {
            "code": "delivery_scope_audit_unavailable",
            "agent": "implementer",
        }

    def test_interrupted_scope_audit_preserves_resolved_symlink_root_authority(self, tmp_project: Path):
        target = tmp_project / "core"
        target.mkdir()
        alias = tmp_project / "core-alias"
        try:
            alias.symlink_to("core", target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation is unavailable: {exc}")
        orch, _, _ = _make_orchestrator(tmp_project, allowed_write_paths=["core-alias"])
        state = _scoped_delivery_state(orch, ["core-alias"])
        policy = orch._delivery_scope_audit_policy(state, "implementer")
        orch._set_delivery_scope_audit_pending(state, "implementer", policy=policy)
        assert state.delivery_scope_audit_pending == {
            "schema_version": 8,
            "agent": "implementer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "core-alias", "resolved_path": "core", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._delivery_scope_audit_snapshot(state, "implementer", policy=policy)
        (alias / "partial.py").write_text("in scope\n", encoding="utf-8")

        result = orch.recover_interrupted_delivery_scope(state.task_id)

        assert result.failed is False
        audit = result.validation_cycle_records[-1]
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["changed_paths"] == ["core/partial.py"]
        assert audit["metadata"]["violation_count"] == 0

    def test_exact_file_scope_does_not_authorize_replacement_directory_descendants(
        self,
        tmp_project: Path,
    ):
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["src/main.py"]},
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/main.py"],
            project_config=project_config,
        )

        def replace_file_with_directory(_state: TaskState) -> None:
            main = tmp_project / "src" / "main.py"
            main.unlink()
            main.mkdir()
            (main / "escaped.py").write_text("outside exact file scope\n", encoding="utf-8")

        stubs["implementer"].side_effect = replace_file_with_directory
        stubs["implementer"].result_data = {"files_written": ["src/main.py"]}
        state = _scoped_delivery_state(orch, ["src/main.py"])

        result = orch._run_agent("implementer", state)

        assert result.success is False
        assert state.failed is True
        audit = state.validation_cycle_records[-1]["metadata"]
        assert audit["violation_paths"] == ["src/main.py/escaped.py"]
        assert (tmp_project / "src" / "main.py" / "escaped.py").exists()

    def test_fixer_test_path_remains_governed_by_test_policy(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )

        def write_test(_state: TaskState) -> None:
            test_file = tmp_project / "tests" / "test_allowed.py"
            test_file.parent.mkdir()
            test_file.write_text("def test_allowed(): pass\n", encoding="utf-8")

        stubs["fixer"].side_effect = write_test
        stubs["fixer"].result_data = {"files_written": ["tests/test_allowed.py"]}
        stubs["fixer"].active_test_write_paths = ["tests"]
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("fixer", state)

        assert result.success is True
        assert state.failed is False
        audit = state.validation_cycle_records[-1]
        assert audit["status"] == "passed"
        assert audit["metadata"]["changed_count"] == 1
        assert audit["metadata"]["production_changed_count"] == 0

    def test_fixer_test_only_edit_to_clean_mixed_cargo_source_uses_retained_baseline(
        self,
        tmp_project: Path,
    ) -> None:
        lib = tmp_project / "src" / "lib.rs"
        before = """pub fn value() -> i32 { 1 }

#[cfg(test)]
mod tests {
    #[test]
    fn value_is_one() {
        assert_eq!(super::value(), 1);
    }
}
"""
        after = before.replace("assert_eq!(super::value(), 1);", 'assert_eq!(super::value(), 1, "value");')
        lib.write_text(before, encoding="utf-8")
        subprocess.run(["git", "add", "src/lib.rs"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "track mixed source"], cwd=tmp_project, check=True, capture_output=True)
        project_config = {
            "project": {"build_tool": "cargo"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["src"],
            },
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )
        orch._tools["build"] = CargoTool(
            Sandbox(tmp_project, allowed_write_paths=["src"], allowed_read_paths=["."]),
            tmp_project,
        )
        stubs["fixer"].active_test_write_paths = ["src"]
        stubs["fixer"].side_effect = lambda _state: lib.write_text(after, encoding="utf-8")
        stubs["fixer"].result_data = {"files_written": ["src/lib.rs"]}
        state = _scoped_delivery_state(orch, ["src/allowed"])

        result = orch._run_agent("fixer", state)

        assert result.success is True
        assert state.failed is False
        audit = state.validation_cycle_records[-1]
        assert audit["status"] == "passed"
        assert audit["metadata"]["changed_paths"] == ["src/lib.rs"]
        assert audit["metadata"]["production_changed_count"] == 0

    def test_fixer_without_active_test_authority_cannot_write_test_paths(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )

        def write_test(_state: TaskState) -> None:
            test_file = tmp_project / "tests" / "test_escaped.py"
            test_file.parent.mkdir()
            test_file.write_text("def test_escaped(): pass\n", encoding="utf-8")

        stubs["fixer"].side_effect = write_test
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.errors = ["compile failed"]

        result = orch._run_agent("fixer", state)

        assert result.success is False
        assert state.delivery_stop_code == "unit_scope_violation"
        assert state.validation_cycle_records[-1]["metadata"]["violation_paths"] == ["tests/test_escaped.py"]

    def test_test_only_fixer_provider_call_cannot_write_unit_production_scope(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )

        def hidden_production_write() -> tuple[list[str], str]:
            (tmp_project / "src" / "allowed" / "hidden.py").write_text("unauthorized\n", encoding="utf-8")
            test_file = tmp_project / "tests" / "test_allowed.py"
            test_file.parent.mkdir()
            test_file.write_text("def test_allowed(): pass\n", encoding="utf-8")
            return ["tests/test_allowed.py"], (
                "TEST FAILURE TRIAGE:\nclassification: stale_test\ncontract_affected: none\nchosen_fix: test_code\n"
            )

        client = ObservedWriteClient(tmp_project, [hidden_production_write])
        fixer = FixerAgent(client, orch._tools, project_config)
        orch._agents["fixer"] = fixer
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.test_errors = ["tests/test_allowed.py: assertion failed"]

        result = orch._run_agent("fixer", state)

        assert result.success is False
        assert result.message == "unit_scope_violation"
        assert state.delivery_stop_code == "unit_scope_violation"
        audit = state.validation_cycle_records[-1]
        assert audit["metadata"]["audit_boundary"] == "provider_attempt"
        assert audit["metadata"]["violation_paths"] == ["src/allowed/hidden.py"]
        assert audit["metadata"]["effective_paths"] == []

    def test_interrupted_test_only_fixer_audit_persists_exact_provider_authority(
        self,
        tmp_project: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
        )
        orch._config_snapshot = {"run_build": True}

        def write_test() -> tuple[list[str], str]:
            (tmp_project / "src" / "allowed" / "hidden.py").write_text("unauthorized\n", encoding="utf-8")
            test_file = tmp_project / "tests" / "test_partial.py"
            test_file.parent.mkdir()
            test_file.write_text("def test_partial(): pass\n", encoding="utf-8")
            return ["tests/test_partial.py"], "partial"

        client = ObservedWriteClient(tmp_project, [write_test])
        fixer = FixerAgent(client, orch._tools, project_config)
        orch._agents["fixer"] = fixer
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.test_errors = ["tests/test_partial.py: assertion failed"]
        original_capture = orch._delivery_scope_audit._capture_delivery_scope_snapshot
        captures = 0

        def interrupt_provider_post_snapshot(*args, **kwargs):
            nonlocal captures
            captures += 1
            if captures == 3:
                raise KeyboardInterrupt
            return original_capture(*args, **kwargs)

        monkeypatch.setattr(
            orch._delivery_scope_audit,
            "_capture_delivery_scope_snapshot",
            interrupt_provider_post_snapshot,
        )

        with pytest.raises(KeyboardInterrupt):
            orch._run_agent("fixer", state)

        interrupted = orch._store.load(state.task_id)
        assert interrupted is not None
        pending = interrupted.delivery_scope_audit_pending
        assert isinstance(pending, dict)
        assert pending["schema_version"] == 8
        assert pending["snapshot_name"] == "delivery_scope_attempt_before"
        assert pending["production_roots"] == []
        assert pending["active_test_write_paths"] == [{"path": "tests", "resolved_path": "tests"}]
        assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_attempt_before") is not None

        recovered = orch.recover_interrupted_delivery_scope(state.task_id)

        assert recovered.failed is True
        assert recovered.delivery_stop_code == "unit_scope_violation"
        assert [record["config_snapshot"] for record in recovered.run_invocation_records] == [{"run_build": True}]
        assert recovered.validation_cycle_records[-1]["metadata"]["violation_paths"] == ["src/allowed/hidden.py"]
        assert orch._store.load_text_snapshot(state.task_id, "delivery_scope_attempt_before") is None

    def test_scope_violation_preempts_no_change_adoption_and_later_phases(self, tmp_project: Path):
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {"allowed_write_paths": ["src/allowed"]},
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            project_config=project_config,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def hidden_write(_state: TaskState) -> None:
            (tmp_project / "outside.py").write_text("provider omitted this path\n", encoding="utf-8")

        stubs["implementer"].side_effect = hidden_write
        stubs["implementer"].result_data = {"files_written": []}
        _scoped_delivery_state(orch, ["src/allowed"])

        result = orch.run(task_id="t1")

        assert result.failed is True
        assert len(stubs["implementer"].calls) == 1
        assert not stubs["reviewer"].calls
        assert not stubs["security_reviewer"].calls
        assert (tmp_project / "outside.py").exists()
        assert not any(entry["action"] == "adopt_worktree_changes" for entry in result.history)
        assert not any("produced no file changes" in entry["result"] for entry in result.history)

    def test_resume_audits_interrupted_implementer_before_pipeline_continues(
        self,
        tmp_project: Path,
        monkeypatch,
    ):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=60,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._set_delivery_scope_audit_pending(state, "implementer")
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.start_active_operation("agent", agent="implementer", message="interrupted")
        orch._store.save(state)
        (allowed / "kept.py").write_text("in scope\n", encoding="utf-8")
        outside = tmp_project / "docs" / "escaped.md"
        outside.parent.mkdir()
        outside.write_text("outside scope\n", encoding="utf-8")
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is True
        assert result.delivery_stop_code == "unit_scope_violation"
        assert result.active_operation is None
        assert pipeline_calls == []
        assert not any(stub.calls for stub in stubs.values())
        assert (allowed / "kept.py").exists()
        assert outside.exists()
        audit = result.validation_cycle_records[-1]
        assert audit["status"] == "failed"
        assert audit["metadata"]["code"] == "unit_scope_violation"
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["violation_paths"] == ["docs/escaped.md"]

    def test_resume_allows_interrupted_implementer_with_only_in_scope_changes(
        self,
        tmp_project: Path,
        monkeypatch,
    ):
        allowed = tmp_project / "src" / "allowed"
        allowed.mkdir()
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=60,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._set_delivery_scope_audit_pending(state, "implementer")
        orch._delivery_scope_audit_snapshot(state, "implementer")
        state.start_active_operation("agent", agent="implementer", message="interrupted")
        orch._store.save(state)
        (allowed / "kept.py").write_text("in scope\n", encoding="utf-8")
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is False
        assert result.active_operation is None
        assert pipeline_calls == [result]
        audit = result.validation_cycle_records[-1]
        assert audit["status"] == "passed"
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["production_changed_count"] == 1

    def test_resume_keeps_interrupted_fixer_test_path_under_test_policy(
        self,
        tmp_project: Path,
        monkeypatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, stubs, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=60,
            project_config=project_config,
        )
        stubs["fixer"].active_test_write_paths = ["tests"]
        state = _scoped_delivery_state(orch, ["src/allowed"])
        orch._set_delivery_scope_audit_pending(state, "fixer")
        assert state.delivery_scope_audit_pending == {
            "schema_version": 8,
            "agent": "fixer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [{"path": "tests", "resolved_path": "tests"}],
        }
        orch._delivery_scope_audit_snapshot(state, "fixer")
        state.start_active_operation("agent", agent="fixer", message="interrupted")
        orch._store.save(state)
        test_file = tmp_project / "tests" / "test_allowed.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_allowed(): pass\n", encoding="utf-8")
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is False
        assert pipeline_calls == [result]
        audit = result.validation_cycle_records[-1]
        assert audit["status"] == "passed"
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["changed_count"] == 1
        assert audit["metadata"]["production_changed_count"] == 0

    def test_resume_rejects_interrupted_production_fixer_test_write(
        self,
        tmp_project: Path,
        monkeypatch,
    ):
        (tmp_project / "src" / "allowed").mkdir()
        project_config = {
            "project": {"build_tool": "python"},
            "sandbox": {
                "allowed_write_paths": ["src/allowed"],
                "allowed_test_write_paths": ["tests"],
            },
        }
        orch, _, _ = _make_orchestrator(
            tmp_project,
            allowed_write_paths=["src/allowed"],
            heartbeat_interval_seconds=60,
            project_config=project_config,
        )
        state = _scoped_delivery_state(orch, ["src/allowed"])
        state.errors = ["compile failed"]
        orch._set_delivery_scope_audit_pending(state, "fixer")
        assert state.delivery_scope_audit_pending == {
            "schema_version": 8,
            "agent": "fixer",
            "project_prefix": ".",
            **_git_scope_binding_fields(tmp_project),
            "snapshot_name": "delivery_scope_audit_before",
            "production_roots": [{"path": "src/allowed", "resolved_path": "src/allowed", "exact_file": False}],
            "active_test_write_paths": [],
        }
        orch._delivery_scope_audit_snapshot(state, "fixer")
        test_file = tmp_project / "tests" / "test_escaped.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_escaped(): pass\n", encoding="utf-8")
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        result = orch.run(task_id=state.task_id)

        assert result.failed is True
        assert pipeline_calls == []
        assert result.delivery_stop_code == "unit_scope_violation"
        audit = result.validation_cycle_records[-1]
        assert audit["metadata"]["resume_recovery"] is True
        assert audit["metadata"]["violation_paths"] == ["tests/test_escaped.py"]


class TestDeliveryTerminalStops:
    @staticmethod
    def _ready_child(orch: Orchestrator, **kwargs) -> TaskState:
        return _save_state(
            orch,
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            **kwargs,
        )

    def test_analyst_dependency_stop_prevents_all_later_phases(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=True,
        )
        stubs["analyst"].side_effect = lambda state: state.set_delivery_stop_disposition(
            "analyst",
            _delivery_disposition(DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP),
        )
        self._ready_child(orch)

        result = orch.run(task_id="t1")

        assert result.delivery_stop_code == "external_dependency_gap"
        assert result.failed is True
        assert result.done is False
        assert len(stubs["analyst"].calls) == 1
        assert not any(stub.calls for name, stub in stubs.items() if name != "analyst")
        assert build.sync_calls == build.compile_calls == build.test_calls == 0
        assert result.result_commit is None
        assert result.delivery_dependency_handoffs == []

    def test_implementer_dependency_stop_prevents_review_and_validation(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=True,
        )
        stubs["implementer"].side_effect = lambda state: state.set_delivery_stop_disposition(
            "implementer",
            _delivery_disposition(DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP),
        )
        self._ready_child(orch, implementation_prompt="p", plan_decided=True)

        result = orch.run(task_id="t1")

        assert result.delivery_stop_code == "external_dependency_gap"
        assert result.failed is True
        assert len(stubs["implementer"].calls) == 1
        assert not stubs["reviewer"].calls
        assert not stubs["security_reviewer"].calls
        assert not stubs["test_writer"].calls
        assert build.sync_calls == build.compile_calls == build.test_calls == 0
        assert result.result_commit is None

    def test_invalid_implementer_disposition_with_partial_writes_is_terminal(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=True,
        )

        def malformed_disposition(state: TaskState) -> None:
            state.files_changed.append("src/partial.py")
            state.record_delivery_disposition_parse_error(
                "implementer",
                "delivery_disposition.keys_invalid",
            )

        stubs["implementer"].side_effect = malformed_disposition
        stubs["implementer"].result_success = False
        self._ready_child(orch, implementation_prompt="p", plan_decided=True)

        result = orch.run(task_id="t1")

        assert result.delivery_stop_code == "implementer_disposition_invalid"
        assert result.failed is True
        assert result.done is False
        assert result.files_changed == ["src/partial.py"]
        assert len(stubs["implementer"].calls) == 1
        assert not stubs["reviewer"].calls
        assert not stubs["security_reviewer"].calls
        assert not stubs["test_writer"].calls
        assert build.sync_calls == build.compile_calls == build.test_calls == 0
        assert result.result_commit is None

        result.failed = False
        orch._store.save(result)
        resumed = orch.run(task_id="t1")

        assert resumed.delivery_stop_code == "implementer_disposition_invalid"
        assert resumed.failed is True
        assert len(stubs["implementer"].calls) == 1
        assert not stubs["reviewer"].calls

    def test_reviewer_scope_amendment_stop_does_not_enter_fix_loop(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=True,
        )
        stubs["reviewer"].side_effect = lambda state: state.set_delivery_stop_disposition(
            "reviewer",
            _delivery_disposition(DELIVERY_DISPOSITION_REQUIRES_SCOPE_AMENDMENT),
        )
        self._ready_child(
            orch,
            implementation_prompt="p",
            plan_decided=True,
            files_changed=["src/main.py"],
        )

        result = orch.run(task_id="t1")

        assert result.delivery_stop_code == "scope_amendment_required"
        assert result.failed is True
        assert len(stubs["reviewer"].calls) == 1
        assert not stubs["implementer"].calls
        assert not stubs["security_reviewer"].calls
        assert not stubs["test_writer"].calls
        assert build.sync_calls == build.compile_calls == build.test_calls == 0
        assert result.result_commit is None

    def test_security_dependency_stop_prevents_test_and_validation(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=True,
        )
        stubs["security_reviewer"].side_effect = lambda state: state.set_delivery_stop_disposition(
            "security_reviewer",
            _delivery_disposition(DELIVERY_DISPOSITION_EXTERNAL_DEPENDENCY_GAP),
        )
        self._ready_child(
            orch,
            implementation_prompt="p",
            plan_decided=True,
            files_changed=["src/main.py"],
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.delivery_stop_code == "external_dependency_gap"
        assert result.failed is True
        assert len(stubs["security_reviewer"].calls) == 1
        assert not stubs["implementer"].calls
        assert not stubs["reviewer"].calls
        assert not stubs["test_writer"].calls
        assert build.sync_calls == build.compile_calls == build.test_calls == 0
        assert result.result_commit is None

    def test_persisted_stop_cannot_be_bypassed_by_reset_or_done_state(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        before_pipeline_calls: list[TaskState] = []
        self._ready_child(
            orch,
            implementation_prompt="p",
            plan_decided=True,
            delivery_stop_code="external_dependency_gap",
        )

        result = orch.run(task_id="t1", before_pipeline=before_pipeline_calls.append)

        assert result.failed is True
        assert result.done is False
        assert result.delivery_stop_code == "external_dependency_gap"
        assert result.final_summary["result"] == "failed"
        assert result.final_summary["delivery_stop_code"] == "external_dependency_gap"
        assert before_pipeline_calls == []
        assert not any(stub.calls for stub in stubs.values())

    def test_persisted_stop_overrides_inconsistent_done_state(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        self._ready_child(
            orch,
            done=True,
            delivery_stop_code="scope_amendment_required",
        )

        result = orch.run(task_id="t1")

        assert result.failed is True
        assert result.done is False
        assert result.delivery_stop_code == "scope_amendment_required"
        assert not any(stub.calls for stub in stubs.values())

    def test_malformed_persisted_disposition_fails_before_provider(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        self._ready_child(
            orch,
            delivery_stop_disposition={"disposition": "external_dependency_gap"},
        )

        result = orch.run(task_id="t1")

        assert result.failed is True
        assert result.done is False
        assert result.delivery_stop_code is None
        assert not any(stub.calls for stub in stubs.values())
        assert any("delivery_stop_state_invalid" in entry["result"] for entry in result.history)

    def test_malformed_persisted_disposition_parse_error_fails_before_provider(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        self._ready_child(
            orch,
            delivery_disposition_parse_error={"error_code": "delivery_disposition.keys_invalid"},
        )

        result = orch.run(task_id="t1")

        assert result.failed is True
        assert result.done is False
        assert result.delivery_stop_code is None
        assert not any(stub.calls for stub in stubs.values())
        assert any("delivery_stop_state_invalid" in entry["result"] for entry in result.history)


# ---------------------------------------------------------------------------
# Tests — loop gates and idempotency
# ---------------------------------------------------------------------------


class TestOrchestratorLoop:
    def test_run_records_each_non_terminal_invocation_config(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = orch._store.create("test task")
        monkeypatch.setattr(orch, "_loop", lambda _state: None)

        orch._config_snapshot = {"run_build": True}
        orch.run(task_id=state.task_id, complete_invocation_history=True)
        orch._config_snapshot = {"run_build": False}
        orch.run(task_id=state.task_id)

        loaded = orch._store.load(state.task_id)
        assert loaded is not None
        assert loaded.run_invocation_schema_version == 1
        assert [record["config_snapshot"] for record in loaded.run_invocation_records] == [
            {"run_build": True},
            {"run_build": False},
        ]

    def test_task_description_marks_first_invocation_history_complete(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        monkeypatch.setattr(orch, "_loop", lambda _state: None)

        state = orch.run(task_description="test task")

        assert state.run_invocation_schema_version == 1
        assert len(state.run_invocation_records) == 1

    def test_run_reuses_invocation_recorded_by_scope_recovery(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = orch._store.create("test task")
        state.record_run_invocation({"run_build": True})
        orch._store.save(state)
        monkeypatch.setattr(orch, "_loop", lambda _state: None)

        orch.run(task_id=state.task_id, invocation_already_recorded=True)

        loaded = orch._store.load(state.task_id)
        assert loaded is not None
        assert [record["config_snapshot"] for record in loaded.run_invocation_records] == [{"run_build": True}]

    def test_before_pipeline_interruption_preserves_invocation_evidence(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = orch._store.create("test task")
        orch._config_snapshot = {"run_build": True}
        pipeline_calls: list[TaskState] = []
        monkeypatch.setattr(orch, "_loop", pipeline_calls.append)

        def interrupt_before_pipeline(_run_state: TaskState) -> None:
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            orch.run(
                task_id=state.task_id,
                complete_invocation_history=True,
                before_pipeline=interrupt_before_pipeline,
            )

        persisted = orch._store.load(state.task_id)
        assert persisted is not None
        assert persisted.config_snapshot == {"run_build": True}
        assert persisted.run_invocation_schema_version == 1
        assert [record["config_snapshot"] for record in persisted.run_invocation_records] == [{"run_build": True}]
        assert pipeline_calls == []

    def test_legacy_resume_records_partial_history_without_claiming_complete_evidence(
        self, tmp_path: Path, monkeypatch
    ):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = _save_state(orch)
        monkeypatch.setattr(orch, "_loop", lambda _state: None)
        orch._config_snapshot = {"run_build": True}

        orch.run(task_id=state.task_id)

        loaded = orch._store.load(state.task_id)
        assert loaded is not None
        assert loaded.run_invocation_schema_version is None
        assert len(loaded.run_invocation_records) == 1

    @pytest.mark.parametrize(("done", "failed"), [(True, False), (False, True)])
    def test_run_does_not_record_terminal_no_op_invocations(self, tmp_path: Path, done: bool, failed: bool):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = _save_state(orch, done=done, failed=failed)
        hook_calls: list[TaskState] = []

        orch.run(task_id=state.task_id, before_pipeline=hook_calls.append)

        loaded = orch._store.load(state.task_id)
        assert loaded is not None
        assert loaded.config_snapshot == {}
        assert loaded.run_invocation_records == []
        assert hook_calls == []

    def test_run_agent_records_active_operation_while_agent_runs(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=60)

        def assert_active_operation(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "agent"
            assert loaded.active_operation["agent"] == "analyst"

        stubs["analyst"].side_effect = assert_active_operation
        state = _save_state(orch)

        result = orch._run_agent("analyst", state)
        loaded = orch._store.load(state.task_id)

        assert result.success
        assert loaded is not None
        assert loaded.active_operation is None

    def test_run_agent_sets_scoped_llm_session_title(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=0)
        llm = StubLLMClient()
        stubs["analyst"].llm = llm
        state = TaskState(
            task_id="5b891efb881f4388a8f0fd0578f98573",
            task_description="Confidential Customer Bug SECRET-123",
        )
        orch._store.save(state)

        def assert_session_title(_state: TaskState) -> None:
            title = getattr(llm, "_session_title")
            assert title == "sikula-analyst-5b891efb"
            assert "Confidential" not in title
            assert "SECRET-123" not in title

        stubs["analyst"].side_effect = assert_session_title

        result = orch._run_agent("analyst", state)

        assert result.success
        assert getattr(llm, "_session_title") is None

    def test_run_agent_skips_active_operation_when_heartbeat_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=0)

        def assert_no_active_operation(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is None

        stubs["analyst"].side_effect = assert_no_active_operation
        state = _save_state(orch)

        result = orch._run_agent("analyst", state)

        assert result.success

    def test_test_writer_audit_prep_records_active_operation_before_agent(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            heartbeat_interval_seconds=60,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def restore_snapshot_effect(state: TaskState, extra_paths=()) -> dict[str, str | None]:
            assert list(extra_paths) == []
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "test_writer audit prep"
            assert loaded.active_operation["message"] == "Preparing test-writer audit baseline"
            return {}

        def test_writer_effect(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is not None
            assert loaded.active_operation["phase"] == "agent"
            assert loaded.active_operation["agent"] == "test_writer"
            state.tests_up_to_date = True

        monkeypatch.setattr(orch, "_test_writer_restore_snapshot", restore_snapshot_effect)
        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)
        loaded = orch._store.load(state.task_id)

        assert changed is False
        assert loaded is not None
        assert loaded.active_operation is None

    def test_run_clears_stale_active_operation_on_resume(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, heartbeat_interval_seconds=0)
        state = _save_state(orch, implementation_prompt="already analyzed")
        state.start_active_operation("agent", agent="analyst", message="stale")
        orch._store.save(state)

        def assert_stale_operation_cleared(state: TaskState) -> None:
            loaded = orch._store.load(state.task_id)
            assert loaded is not None
            assert loaded.active_operation is None
            state.review_approved = True

        stubs["implementer"].side_effect = assert_stale_operation_cleared

        orch.run(task_id=state.task_id)

    def test_agents_receive_effective_pipeline_flags(self, tmp_path: Path):
        config = OrchestratorConfig(
            project_root=tmp_path,
            allowed_write_paths=["."],
            allowed_read_paths=["."],
            run_build=True,
            run_tests=True,
            run_checks=False,
            project_config={
                "project": {"build_tool": "python"},
                "run_checks": True,
                "build": {"checks": [{"name": "ruff", "command": "ruff check ."}]},
            },
        )

        orch = Orchestrator(config=config, llm=StubLLMClient(), state_store=JsonStateStore(tmp_path / "state"))

        effective = orch._agents["reviewer"].project_config["__sikula_effective_pipeline"]
        assert effective == {"run_build": True, "run_tests": True, "run_checks": False}

    def test_uncovered_task_validation_command_fails_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {
                    "test_command": "cargo test",
                    "checks": [{"name": "fmt", "command": "cargo fmt --check"}],
                },
            },
        )

        result = orch.run(
            task_description="Add export support. Acceptance: run `cargo run -p codegen_tool -- fixtures/`."
        )

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(
            entry["action"] == "abort" and "validation coverage gap" in entry["result"] for entry in result.history
        )
        assert any(
            entry["phase"] == "validation_coverage" and entry["status"] == "failed"
            for entry in result.validation_cycle_records
        )

    def test_near_match_task_validation_command_fails_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )

        result = orch.run(task_description="Update config loader. Run `cargo test --workspace --all-features`.")

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(
            entry["phase"] == "validation_coverage"
            and entry["status"] == "failed"
            and "cargo test --workspace --all-features" in entry.get("error_excerpt", "")
            for entry in result.validation_cycle_records
        )

    def test_node_detected_script_defaults_cover_task_validation_commands(self, tmp_path: Path):
        (tmp_path / "pnpm-lock.yaml").write_text("")
        (tmp_path / "package.json").write_text('{"scripts": {"typecheck": "tsc --noEmit", "test": "vitest run"}}')
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "node", "root_path": str(tmp_path)},
                "build": {},
            },
        )
        stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/index.ts")

        result = orch.run(task_description="## Verification\n\npnpm run typecheck\npnpm test\n")

        assert result.done
        assert not result.failed
        assert stubs["analyst"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_validation_heading_with_blank_separator_fails_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )

        result = orch.run(task_description="## Verification\n\ncargo test --workspace --all-features\n")

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(
            entry["phase"] == "validation_coverage"
            and entry["status"] == "failed"
            and "cargo test --workspace --all-features" in entry.get("error_excerpt", "")
            for entry in result.validation_cycle_records
        )

    def test_unknown_review_mode_does_not_skip_validation_coverage_preflight(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )
        state = _save_state(
            orch,
            review_mode="unknown",
        )
        state.task_description = "Update parser. Run `cargo test --workspace --all-features`."
        orch._store.save(state)

        result = orch.run(task_id="t1")

        assert result.failed
        assert not stubs["analyst"].calls
        assert any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_prose_starting_with_tool_name_does_not_fail_validation_coverage(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )
        stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/main.rs")

        result = orch.run(
            task_description=(
                "python parser should reject invalid quoted strings.\n"
                "cargo features should remain optional.\n"
                "- `cargo` features should remain optional."
            )
        )

        assert not result.failed
        assert stubs["analyst"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_python_module_validation_command_is_covered_before_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=True,
            run_checks=True,
            project_config={
                "project": {"build_tool": "python"},
                "build": {"test_command": "python3 -m pytest"},
            },
        )
        stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/main.py")

        result = orch.run(task_description="Update parser. Run `pytest`.")

        assert not result.failed
        assert stubs["analyst"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_package_script_shortcut_validation_command_is_covered_before_agents(self, tmp_path: Path):
        cases = [
            (
                tmp_path / "npm",
                {"name": "npm-tests", "command": "npm run test"},
                "Update package. Run `npm test`.",
            ),
            (
                tmp_path / "yarn",
                {"name": "yarn-tests", "command": "yarn run test"},
                "Update package. Run `yarn test`.",
            ),
        ]

        for root, check, task_description in cases:
            root.mkdir()
            orch, stubs, _ = _make_orchestrator(
                root,
                run_build=True,
                run_tests=False,
                run_checks=True,
                project_config={
                    "project": {"build_tool": "python"},
                    "build": {
                        "compile_command": "ruff check .",
                        "checks": [check],
                    },
                },
            )
            stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/main.py")

            result = orch.run(task_description=task_description)

            assert not result.failed
            assert stubs["analyst"].calls
            assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_wrapper_alias_validation_command_is_covered_before_agents(self, tmp_path: Path):
        cases = [
            (
                tmp_path / "gradle",
                {"project": {"build_tool": "gradle-android"}, "build": {}},
                "Update UI. Run `./gradlew testDebugUnitTest`.",
            ),
            (
                tmp_path / "maven",
                {"project": {"build_tool": "maven"}, "build": {}},
                "Update API. Run `./mvnw test`.",
            ),
        ]

        for root, project_config, task_description in cases:
            root.mkdir()
            orch, stubs, _ = _make_orchestrator(
                root,
                run_build=True,
                run_tests=True,
                run_checks=True,
                project_config=project_config,
            )
            stubs["implementer"].side_effect = lambda state: state.files_changed.append("src/Main")

            result = orch.run(task_description=task_description)

            assert not result.failed
            assert stubs["analyst"].calls
            assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_done_task_skips_all_agents(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, done=True)
        orch.run(task_id="t1")
        assert all(len(s.calls) == 0 for s in stubs.values())

    def test_failed_task_skips_all_agents(self, tmp_path: Path, caplog):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, failed=True)
        caplog.set_level("INFO")
        orch.run(task_id="t1")
        assert all(len(s.calls) == 0 for s in stubs.values())
        assert "--reset-failed" in caplog.text
        assert "nothing to do" not in caplog.text

    def test_contract_gate_failed_task_does_not_log_reset_hint(self, tmp_path: Path, caplog):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, failed=True, contract_gate_blocked=True)
        caplog.set_level("INFO")
        orch.run(task_id="t1")
        assert all(len(s.calls) == 0 for s in stubs.values())
        assert "failed by contract readiness gate" in caplog.text
        assert "--reset-failed" not in caplog.text

    def test_agent_llm_retry_is_recorded_in_history(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = TaskState(task_id="t1", task_description="test")
        orch._agents["test_writer"] = RetryReportingAgent()  # type: ignore[assignment]

        result = orch._run_agent("test_writer", state)

        assert result.success
        retry = state.history[0]
        assert retry["agent"] == "test_writer"
        assert retry["action"] == "llm_retry"
        assert retry["result"] == "temporary provider failure"
        assert retry["provider"] == "codex"
        assert retry["model"] == "gpt-5.3-codex"
        assert retry["operation"] == "run_agent"
        assert retry["attempt"] == 1
        assert retry["max_attempts"] == 4
        assert retry["delay_s"] == 30
        assert retry["error_type"] == "RuntimeError"
        assert state.history[1]["action"] == "test_write"

    def test_agent_llm_usage_is_recorded_in_structured_state(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = TaskState(task_id="t1", task_description="test")
        orch._agents["implementer"] = UsageReportingAgent()  # type: ignore[assignment]

        result = orch._run_agent("implementer", state)

        assert result.success
        assert len(state.llm_usage_records) == 1
        record = state.llm_usage_records[0]
        assert record["agent"] == "implementer"
        assert record["provider"] == "codex"
        assert record["operation"] == "run_agent"
        assert record["reported_tokens"] == {"input_tokens": 50, "output_tokens": 5}
        assert "prompt" not in record
        assert "recorded_at" in record

    def test_analyze_phase_skipped_when_prompt_exists(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        _save_state(orch, implementation_prompt="existing", files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["analyst"].calls) == 0

    def test_analyst_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        stubs["analyst"].side_effect = lambda s: setattr(s, "failed", True)
        _save_state(orch)
        result = orch.run(task_id="t1")
        assert result.failed
        assert len(stubs["implementer"].calls) == 0

    def test_analyst_result_failure_aborts_before_planner(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)

        class FailingAnalyst:
            def __init__(self) -> None:
                self.calls: list[TaskState] = []

            def run(self, state: TaskState) -> AgentResult:
                self.calls.append(state)
                state.record("analyst", "analyze_failed", "invalid implementation prompt")
                return AgentResult(success=False, message="invalid implementation prompt")

        failing_analyst = FailingAnalyst()
        orch._agents["analyst"] = failing_analyst  # type: ignore[assignment]
        _save_state(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(failing_analyst.calls) == 1
        assert len(stubs["planner"].calls) == 0
        assert len(stubs["implementer"].calls) == 0

    def test_planner_skipped_when_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=False, run_build=False)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["planner"].calls) == 0

    def test_planner_skipped_when_already_decided(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)
        _save_state(orch, implementation_prompt="p", plan_decided=True, files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["planner"].calls) == 0

    def test_implementer_no_files_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path)
        _save_state(orch, implementation_prompt="p")
        result = orch.run(task_id="t1")
        assert result.failed
        assert len(stubs["implementer"].calls) == 1

    def test_implementer_no_files_adopts_dirty_worktree_on_resume(self, tmp_path: Path):
        # Simulate a worktree where the previous (interrupted) implementer run modified a
        # tracked file but the process was killed before state.files_changed was populated.
        subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=tmp_path, check=True, capture_output=True)
        (tmp_path / "src").mkdir()
        original = tmp_path / "src" / "main.py"
        original.write_text("# original")
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_path, check=True, capture_output=True)
        original.write_text("# modified by previous interrupted implementer run")

        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        _save_state(
            orch,
            implementation_prompt="p",
            worktree_path=str(tmp_path),
            worktree_base=str(tmp_path),
        )
        result = orch.run(task_id="t1")

        assert not result.failed
        assert "src/main.py" in result.files_changed
        assert any(h["action"] == "adopt_worktree_changes" for h in result.history)

    def test_implementer_skipped_when_files_already_changed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])
        orch.run(task_id="t1")
        assert len(stubs["implementer"].calls) == 0


# ---------------------------------------------------------------------------
# Tests — build/fix loop
# ---------------------------------------------------------------------------


class TestOrchestratorBuildLoop:
    def _build_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

    def test_max_build_iterations_sets_failed(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=2)
        build.compile_success = False
        self._build_ready(orch)
        result = orch.run(task_id="t1")
        assert result.failed
        assert result.build_iterations == 2

    def test_active_build_loop_budget_survives_resume(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=2)
        build.compile_success = False
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=0,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert result.build_iterations == 2
        assert build.compile_calls == 1

    def test_last_fixer_change_gets_final_validation_chance(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=1,
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        stubs["fixer"].side_effect = fixer_effect
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        assert result.done
        assert result.build_iterations == 2
        assert build.compile_calls == 2
        assert len(stubs["fixer"].calls) == 1
        assert any(record["action"] == "final_validation_after_fix" for record in result.history)

    def test_final_validation_after_last_fix_does_not_run_fixer_again(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=1,
        )
        build.compile_results = [False, False]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        stubs["fixer"].side_effect = fixer_effect
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert result.build_iterations == 2
        assert build.compile_calls == 2
        assert len(stubs["fixer"].calls) == 1

    def test_resume_after_last_fix_gets_final_validation_chance(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=1,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "src/fix.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=0,
            fixer_changed_code=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.build_iterations == 2
        assert build.compile_calls == 1
        assert len(stubs["fixer"].calls) == 0

    def test_sync_skipped_when_already_synced(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True)
        self._build_ready(orch)
        orch.run(task_id="t1")
        assert build.sync_calls == 0

    def test_sync_runs_when_not_synced(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        orch.run(task_id="t1")
        assert build.sync_calls == 1

    def test_tests_skipped_when_disabled(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=False)
        self._build_ready(orch)
        result = orch.run(task_id="t1")
        assert build.test_calls == 0
        assert result.test_status == "skipped"

    def test_done_set_when_build_passes(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path, run_build=True, run_tests=False, run_checks=False)
        self._build_ready(orch)
        result = orch.run(task_id="t1")
        assert result.done
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_test_and_check_statuses_set_when_enabled_phases_pass(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=True, run_checks=True)
        self._build_ready(orch)
        result = orch.run(task_id="t1")

        assert result.done
        assert build.test_calls == 1
        assert result.test_status == "success"
        assert result.check_status == "skipped"

    def test_validation_records_capture_successful_build_test_and_skipped_checks(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path, run_build=True, run_tests=True, run_checks=True)
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        phases = [(r["phase"], r["status"]) for r in result.validation_cycle_records]
        assert ("build", "success") in phases
        assert ("test", "success") in phases
        assert ("check", "skipped") in phases
        assert all(r["build_iteration"] == 1 for r in result.validation_cycle_records)

    def test_validation_records_capture_failed_build_error_excerpt(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=1)
        build.compile_success = False
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        build_records = [r for r in result.validation_cycle_records if r["phase"] == "build"]
        assert build_records[-1]["status"] == "failed"
        assert build_records[-1]["error_excerpt"] == "compile failed"

    def test_validation_records_capture_configured_check_name(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=True,
        )
        orch._config.project_config["build"] = {"checks": [{"name": "ruff", "command": "ruff check ."}]}
        self._build_ready(orch)

        result = orch.run(task_id="t1")

        check_records = [r for r in result.validation_cycle_records if r["phase"] == "check"]
        assert len(check_records) == 1
        assert check_records[0]["status"] == "success"
        assert check_records[0]["build_iteration"] == 1
        assert check_records[0]["step"] == 0
        assert check_records[0]["check_name"] == "ruff"
        assert "timestamp" in check_records[0]


# ---------------------------------------------------------------------------
# Tests — review loop
# ---------------------------------------------------------------------------


class TestOrchestratorReviewLoop:
    def _review_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

    def test_review_skipped_when_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=False, run_build=False)
        self._review_ready(orch)
        result = orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) == 0
        assert result.done
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_review_and_security_skipped_when_both_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=False,
            run_security_review=False,
            run_build=False,
        )
        self._review_ready(orch)
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 0
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_review_skipped_when_already_approved(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False)
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_approved=True)
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) == 0

    def test_max_review_iterations_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=2)
        self._review_ready(orch)

        def always_issues(state: TaskState) -> None:
            state.review_issues = ["dead field X"]

        stubs["reviewer"].side_effect = always_issues
        result = orch.run(task_id="t1")
        assert result.failed
        # max_review_iterations=2 fix attempts → 3 reviews (initial + 2 post-fix), 2 implements
        assert len(stubs["reviewer"].calls) == 3
        assert len(stubs["implementer"].calls) == 2

    def test_every_implement_fix_gets_reviewed(self, tmp_path: Path):
        """Every fix attempt must be followed by a review — implementer never runs unreviewd."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=3)
        self._review_ready(orch)

        def always_issues(state: TaskState) -> None:
            state.review_issues = ["dead field X"]

        stubs["reviewer"].side_effect = always_issues
        orch.run(task_id="t1")
        # max=3 fix attempts → 4 reviews, 3 implements; reviews == implements + 1
        assert len(stubs["reviewer"].calls) == 4
        assert len(stubs["implementer"].calls) == 3
        assert len(stubs["reviewer"].calls) == len(stubs["implementer"].calls) + 1

    def test_review_issues_trigger_implementer_fix(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=3)
        call_count = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                state.review_issues = ["missing null check"]
            else:
                state.review_approved = True
                state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        self._review_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 1
        assert orch._store.load("t1").delivery_stop_code is None

    def test_noop_review_fix_does_not_mark_code_or_tests_stale(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=False,
            max_review_iterations=1,
        )
        call_count = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            call_count["n"] += 1
            if call_count["n"] == 1:
                state.review_approved = False
                state.review_issues = ["false positive already addressed"]
            else:
                state.review_approved = True
                state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_data = {"files_written": []}
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            tests_up_to_date=True,
            build_synced=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.tests_up_to_date is True
        assert result.build_synced is True
        assert len(stubs["reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 1

    def test_review_fix_implementer_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False, max_review_iterations=3)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = False
            state.review_issues = ["missing null check"]

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_success = False
        stubs["implementer"].result_message = "quota exhausted"
        self._review_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 1
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "implementer failed: quota exhausted"
            for entry in result.history
        )

    def test_reviewer_agent_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False)
        stubs["reviewer"].side_effect = None
        stubs["reviewer"].result_success = False
        stubs["reviewer"].result_message = "usage limit reached"
        self._review_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "reviewer failed: usage limit reached"
            for entry in result.history
        )

    @pytest.mark.parametrize(
        ("agent_name", "approval_field"),
        [("reviewer", "review_approved"), ("security_reviewer", "security_approved")],
    )
    def test_delivery_review_protocol_error_retries_once_without_fix(
        self,
        tmp_path: Path,
        agent_name: str,
        approval_field: str,
    ):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            delivery_plan_id="plan",
            delivery_unit_id="unit",
        )
        review_agent = stubs[agent_name]

        def malformed_then_approved(current: TaskState) -> None:
            if len(review_agent.calls) == 1:
                setattr(current, approval_field, False)
                current.review_issues = ["malformed output"]
                review_agent.result_success = False
                review_agent.result_message = "invalid delivery disposition"
                review_agent.result_data = {"disposition_parse_error": "delivery_disposition.position_invalid"}
            else:
                setattr(current, approval_field, True)
                current.review_issues.clear()
                review_agent.result_success = True
                review_agent.result_message = "approved"
                review_agent.result_data = {}

        review_agent.side_effect = malformed_then_approved

        result = orch._run_delivery_review_agent(agent_name, state)

        assert result.success is True
        assert len(review_agent.calls) == 2
        assert len(stubs["implementer"].calls) == 0
        assert state.review_iterations == 0
        assert any(entry["action"] == "review_protocol_retry" for entry in state.history)

    @pytest.mark.parametrize("agent_name", ["reviewer", "security_reviewer"])
    def test_delivery_review_protocol_error_aborts_after_one_retry(self, tmp_path: Path, agent_name: str):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_review=True, run_build=False)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            delivery_plan_id="plan",
            delivery_unit_id="unit",
        )
        review_agent = stubs[agent_name]
        review_agent.result_success = False
        review_agent.result_message = "invalid delivery disposition"
        review_agent.result_data = {"disposition_parse_error": "delivery_disposition.position_invalid"}

        result = orch._run_delivery_review_agent(agent_name, state)

        assert result.success is False
        assert len(review_agent.calls) == 2
        assert len(stubs["implementer"].calls) == 0
        assert sum(entry["action"] == "review_protocol_retry" for entry in state.history) == 1


# ---------------------------------------------------------------------------
# Tests — security review loop
# ---------------------------------------------------------------------------


class TestOrchestratorSecurityLoop:
    def _security_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_approved=True)

    def test_security_review_skipped_when_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_security_review=False, run_build=False)
        self._security_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["security_reviewer"].calls) == 0

    def test_max_security_iterations_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path, run_security_review=True, run_review=False, run_build=False, max_security_review_iterations=2
        )
        self._security_ready(orch)

        def always_blocking(state: TaskState) -> None:
            state.review_issues = ["hardcoded secret"]

        stubs["security_reviewer"].side_effect = always_blocking
        result = orch.run(task_id="t1")
        assert result.failed
        # max=2 fix attempts → 3 security reviews, 2 implements
        assert len(stubs["security_reviewer"].calls) == 3
        assert len(stubs["implementer"].calls) == 2

    def test_security_blocking_issue_triggers_re_review(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
            max_security_review_iterations=2,
            max_review_iterations=3,
        )
        sec_calls = {"n": 0}

        def security_effect(state: TaskState) -> None:
            sec_calls["n"] += 1
            if sec_calls["n"] == 1:
                state.review_issues = ["hardcoded key"]
            else:
                state.security_approved = True
                state.review_issues.clear()

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["security_reviewer"].side_effect = security_effect
        stubs["reviewer"].side_effect = reviewer_effect
        self._security_ready(orch)
        result = orch.run(task_id="t1")
        assert not result.failed
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) >= 1

    def test_noop_security_fix_does_not_mark_code_or_tests_stale(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
            max_security_review_iterations=1,
            max_review_iterations=1,
        )
        security_calls = {"n": 0}

        def security_effect(state: TaskState) -> None:
            security_calls["n"] += 1
            if security_calls["n"] == 1:
                state.security_approved = False
                state.review_issues = ["false positive already addressed"]
            else:
                state.security_approved = True
                state.review_issues.clear()

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["security_reviewer"].side_effect = security_effect
        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_data = {"files_written": []}
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            tests_up_to_date=True,
            build_synced=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.tests_up_to_date is True
        assert result.build_synced is True
        assert len(stubs["security_reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 1

    def test_security_fix_implementer_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
            max_security_review_iterations=2,
            max_review_iterations=3,
        )

        def security_effect(state: TaskState) -> None:
            state.security_approved = False
            state.review_issues = ["hardcoded key"]

        stubs["security_reviewer"].side_effect = security_effect
        stubs["implementer"].result_success = False
        stubs["implementer"].result_message = "not authenticated"
        self._security_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "implementer failed: not authenticated"
            for entry in result.history
        )

    def test_security_reviewer_agent_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=False,
            run_build=False,
        )
        stubs["security_reviewer"].side_effect = None
        stubs["security_reviewer"].result_success = False
        stubs["security_reviewer"].result_message = "not authenticated"
        self._security_ready(orch)

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["implementer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "security_reviewer failed: not authenticated"
            for entry in result.history
        )

    def test_security_warnings_only_do_not_trigger_implementer_fix(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_security_review=True,
            run_review=True,
            run_build=False,
        )

        def security_warning_effect(state: TaskState) -> None:
            state.security_review_cycle_records.append(
                {
                    "reviewer_output": "## Warnings\n\n### Missing audit log\nConcern: low-risk observability gap",
                    "approved": True,
                    "has_warnings": True,
                }
            )
            state.security_approved = True

        stubs["security_reviewer"].side_effect = security_warning_effect
        self._security_ready(orch)
        result = orch.run(task_id="t1")

        assert result.done
        assert not result.failed
        assert len(stubs["implementer"].calls) == 0
        assert result.security_review_cycle_records[-1]["has_warnings"] is True


# ---------------------------------------------------------------------------
# Tests — fix phase flag resets
# ---------------------------------------------------------------------------


class TestOrchestratorFixPhase:
    def test_fix_resets_build_synced_on_build_config_change(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=2)
        build.compile_success = False
        build.build_config_files = {"requirements.txt"}

        def fixer_effect(state: TaskState) -> None:
            if "requirements.txt" not in state.files_changed:
                state.files_changed.append("requirements.txt")

        stubs["fixer"].side_effect = fixer_effect
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        orch.run(task_id="t1")
        assert build.sync_calls > 0

    def test_fix_new_files_defer_re_review_until_validation_passes(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_success = False

        def fixer_effect(state: TaskState) -> None:
            if "src/new.py" not in state.files_changed:
                state.files_changed.append("src/new.py")

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["reviewer"].calls) == 0

    def test_fix_new_files_trigger_re_review_after_validation_passes(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/new.py" not in state.files_changed:
                state.files_changed.append("src/new.py")

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 2
        assert len(stubs["reviewer"].calls) == 1

    def test_fix_re_edit_of_existing_file_triggers_re_review(self, tmp_path: Path):
        """Re-editing an already-tracked file must still trigger review after validation passes."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_results = [False, True]

        # Fixer always reports editing src/main.py — which is already in files_changed.
        # Before the fix, new_files would be empty (set-diff misses re-edits), so review
        # would not be marked stale. With the fix, fixer_result.data["files_written"] is used.
        stubs["fixer"].result_data = {"files_written": ["src/main.py"]}

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1

    def test_fix_triggers_security_re_review(self, tmp_path: Path):
        """After fixer writes files, both reviewer and security reviewer run after validation passes."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            max_iterations=2,
            max_review_iterations=2,
            max_security_review_iterations=2,
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def security_effect(state: TaskState) -> None:
            state.security_approved = True
            state.review_issues.clear()

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        stubs["security_reviewer"].side_effect = security_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1

    def test_test_writer_changes_after_post_fix_review_trigger_another_validation(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=False,
            run_security_review=False,
            run_test_writing=True,
            max_iterations=3,
        )
        build.compile_results = [False, True, True]

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        def test_writer_effect(state: TaskState) -> None:
            if "tests/test_fix.py" not in state.files_changed:
                state.files_changed.append("tests/test_fix.py")
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 3
        assert len(stubs["test_writer"].calls) == 1

    def test_resume_after_fixer_validates_before_re_review(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=2,
        )
        events: list[str] = []
        original_compile_check = build.compile_check

        def compile_check() -> ToolResult:
            events.append("build")
            return original_compile_check()

        def reviewer_effect(state: TaskState) -> None:
            events.append("review")
            state.review_approved = True
            state.review_issues.clear()

        build.compile_check = compile_check  # type: ignore[method-assign]
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            fixer_changed_code=True,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert events == ["build", "review"]

    def test_resume_active_build_loop_validates_before_re_review_when_fixer_flag_cleared(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
        )
        events: list[str] = []
        original_compile_check = build.compile_check

        def compile_check() -> ToolResult:
            events.append("build")
            return original_compile_check()

        def reviewer_effect(state: TaskState) -> None:
            events.append("review")
            state.review_approved = True
            state.review_issues.clear()

        build.compile_check = compile_check  # type: ignore[method-assign]
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=1,
            fixer_changed_code=False,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert events == ["build", "review"]

    def test_resume_active_final_build_loop_keeps_final_scope_before_re_review(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
        )
        review_scopes: list[str | None] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope)
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            plan=["Step 1", "Step 2"],
            plan_decided=True,
            plan_completed=True,
            current_step=1,
            step_implemented=True,
            build_synced=True,
            build_iterations=2,
            build_loop_key="final_full_task",
            build_loop_start_iteration=2,
            fixer_changed_code=False,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 1
        assert review_scopes == ["final_full_task"]

    def test_review_fix_build_config_change_resyncs_before_next_validation(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
            max_review_iterations=2,
        )
        build.compile_results = [False, True, True]
        build.build_config_files = {"pyproject.toml"}
        review_calls = {"n": 0}

        def fixer_effect(state: TaskState) -> None:
            if "src/fix.py" not in state.files_changed:
                state.files_changed.append("src/fix.py")

        def reviewer_effect(state: TaskState) -> None:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                state.review_approved = False
                state.review_issues = ["needs config fix"]
                return
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            if "pyproject.toml" not in state.files_changed:
                state.files_changed.append("pyproject.toml")

        stubs["fixer"].side_effect = fixer_effect
        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls == 3
        assert build.sync_calls == 1

    def test_resume_after_interrupt_between_fixer_and_reviewer_runs_review(self, tmp_path: Path):
        """Simulate interrupt after fixer ran and flags were reset+saved, but reviewer had not yet run.
        On resume, review_approved=False must cause reviewer to run."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False, run_review=True, max_review_iterations=2)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        # State reflects: fixer ran, flags reset and saved, reviewer not yet run
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=False,
        )
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) >= 1

    def test_fixer_no_files_skips_review(self, tmp_path: Path):
        """If fixer writes no files, review must not be re-triggered."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            max_iterations=2,
            max_review_iterations=2,
        )
        build.compile_success = False

        # Fixer reports no files written
        stubs["fixer"].result_data = {"files_written": []}

        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
        )
        orch.run(task_id="t1")

        assert len(stubs["reviewer"].calls) == 0

    def test_test_only_fix_preserves_review_and_test_writer_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_results = [False, True]

        def run_tests() -> ToolResult:
            build.test_calls += 1
            success = test_results.pop(0)
            return ToolResult(success=success, output="", error="" if success else "tests failed")

        def fixer_effect(state: TaskState) -> None:
            if "tests/test_main.py" not in state.files_changed:
                state.files_changed.append("tests/test_main.py")

        build.run_tests = run_tests  # type: ignore[method-assign]
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.review_approved
        assert result.security_approved
        assert result.tests_up_to_date
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 0
        assert any(record["action"] == "test_only_fix" for record in result.history)

    def test_test_only_fix_execution_gate_audit_triggers_followup_fix(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=4,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        test_file.write_text("def test_existing():\n    assert True\n")
        test_results = [False, True]

        def run_tests() -> ToolResult:
            build.test_calls += 1
            success = test_results.pop(0)
            return ToolResult(success=success, output="", error="" if success else "tests failed")

        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            state.errors.clear()
            state.test_errors.clear()
            state.check_errors.clear()
            if attempts["count"] == 1:
                test_file.write_text(
                    'if (typeof document === "undefined") {\n'
                    '  test("client main tests require DOM", () => {});\n'
                    "} else {\n"
                    '  test("opens detail", () => {});\n'
                    "}\n"
                )
            else:
                test_file.write_text("def test_existing_seam():\n    assert True\n")
            if "tests/test_main.py" not in state.files_changed:
                state.files_changed.append("tests/test_main.py")

        build.run_tests = run_tests  # type: ignore[method-assign]
        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/test_main.py"]}
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert attempts["count"] == 2
        assert build.test_calls == 2
        assert len(result.test_execution_gate_records) == 1
        assert result.test_execution_gate_records[0]["source"] == "fixer"
        assert result.test_execution_gate_records[0]["status"] == "resolved"
        assert not any(str(error).startswith("TEST EXECUTION GATE AUDIT:") for error in result.test_errors)
        assert any(record["action"] == "test_execution_gate_audit" for record in result.history)

    def test_test_writer_execution_gate_audit_records_pending_test_error(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text('test.skip("changed behavior", () => {});\n')
            state.tests_up_to_date = True
            state.files_changed.append("tests/test_main.py")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert len(state.test_execution_gate_records) == 1
        assert state.test_execution_gate_records[0]["source"] == "test_writer"
        assert "excerpt" not in state.test_execution_gate_records[0]["findings"][0]
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")
        assert 'test.skip("changed behavior"' not in state.test_errors[0]
        assert "skipped JavaScript/TypeScript test" in state.test_errors[0]

    def test_test_writer_audit_pending_counts_only_saved_snapshot(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        monkeypatch.setattr(
            orch,
            "_iter_configured_test_files",
            lambda: (_ for _ in ()).throw(AssertionError("unexpected full test-tree scan")),
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        orch._begin_test_writer_audit_pending(
            state,
            {"tests/target.test.ts": 'test.skip("preexisting target gate", () => {});\n'},
        )

        assert set(state.test_writer_audit_gate_counts) == {"tests/target.test.ts"}
        assert len(state.test_writer_audit_gate_counts["tests/target.test.ts"]) == 1

    def test_test_writer_audit_uses_head_baseline_for_newly_reported_existing_file(self, tmp_path: Path):
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("preexisting project gate", () => {});\n', encoding="utf-8")
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add preexisting skip"], cwd=tmp_path, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def test_writer_effect(state: TaskState) -> None:
            assert "tests/client_main.test.ts" not in state.test_writer_audit_gate_counts
            test_file.write_text(
                test_file.read_text(encoding="utf-8") + "test('new behavior', () => {});\n",
                encoding="utf-8",
            )
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert state.test_execution_gate_records == []
        assert state.test_errors == []

    def test_resume_missing_snapshot_uses_reported_file_gate_counts(self, tmp_path: Path):
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("preexisting project gate", () => {});\n', encoding="utf-8")
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "test@example.com"], cwd=tmp_path, check=True, capture_output=True
        )
        subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add preexisting skip"], cwd=tmp_path, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=True,
            test_writer_audit_files_written=["tests/client_main.test.ts"],
        )
        orch._persist_test_writer_audit_restore_baselines(state, ["tests/client_main.test.ts"])
        orch._store.delete_text_snapshot(state.task_id, orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT)
        test_file.write_text(
            test_file.read_text(encoding="utf-8") + "test('new behavior', () => {});\n",
            encoding="utf-8",
        )
        orch._store.save(state)

        changed = orch._run_test_write_phase(state)

        assert changed
        assert stubs["test_writer"].calls == []
        assert state.test_execution_gate_records == []
        assert state.test_errors == []

    def test_test_writer_execution_gate_audit_includes_inline_source_tests(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "sandbox": {"allowed_test_write_paths": ["src/"]},
            },
        )
        source_file = tmp_path / "src" / "lib.rs"
        source_file.parent.mkdir()
        source_file.write_text(
            """\
pub fn answer() -> i32 {
    42
}

#[cfg(test)]
mod tests {
    #[test]
    fn existing_contract() {
        assert_eq!(super::answer(), 42);
    }
}
""",
            encoding="utf-8",
        )

        def test_writer_effect(state: TaskState) -> None:
            source_file.write_text(
                """\
pub fn answer() -> i32 {
    42
}

#[cfg(test)]
mod tests {
    #[test]
    fn existing_contract() {
        assert_eq!(super::answer(), 42);
    }

    #[ignore]
    #[test]
    fn generated_contract() {
        assert_eq!(super::answer(), 42);
    }
}
""",
                encoding="utf-8",
            )
            state.tests_up_to_date = True
            state.files_changed.append("src/lib.rs")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.rs"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert len(state.test_execution_gate_records) == 1
        finding = state.test_execution_gate_records[0]["findings"][0]
        assert finding["path"] == "src/lib.rs"
        assert finding["reason"] == "Rust ignored test"
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")

    def test_test_writer_synthetic_harness_audit_includes_inline_source_tests(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "cargo"},
                "sandbox": {"allowed_test_write_paths": ["src/"]},
            },
        )
        source_file = tmp_path / "src" / "lib.rs"
        source_file.parent.mkdir()
        baseline = "pub fn answer() -> i32 {\n    42\n}\n"
        source_file.write_text(baseline, encoding="utf-8")
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                source_file.write_text(
                    baseline
                    + """\

#[cfg(test)]
mod tests {
    struct FakeElement;
    struct FakeEventTarget;
    struct FakeHistory;
    struct FakeServer;
}
""",
                    encoding="utf-8",
                )
            else:
                saved = orch._store.load("t1")
                assert saved is not None
                assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
                assert source_file.read_text(encoding="utf-8") == baseline
                source_file.write_text(
                    baseline
                    + """\

#[cfg(test)]
mod tests {
    #[test]
    fn generated_contract() {
        assert_eq!(super::answer(), 42);
    }
}
""",
                    encoding="utf-8",
                )
            state.tests_up_to_date = True
            state.files_changed.append("src/lib.rs")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["src/lib.rs"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.rs"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 2
        assert len(state.synthetic_test_harness_records) == 1
        record = state.synthetic_test_harness_records[0]
        assert record["findings"][0]["path"] == "src/lib.rs"
        assert record["status"] == "resolved"
        assert "FakeElement" not in source_file.read_text(encoding="utf-8")

    def test_test_writer_execution_gate_audit_fails_when_build_loop_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text('test.skip("changed behavior", () => {});\n')
            state.tests_up_to_date = True
            state.files_changed.append("tests/test_main.py")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert state.failed
        assert state.history[-1]["action"] == "abort"

    def test_no_build_resume_with_active_execution_gate_audit_fails_even_when_tests_up_to_date(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("changed behavior", () => {});\n', encoding="utf-8")
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            tests_up_to_date=True,
        )
        state.record_test_execution_gate_audit(
            "test_writer",
            detect_new_test_execution_gates(
                path="tests/test_main.py",
                before=None,
                after=test_file.read_text(encoding="utf-8"),
            ),
        )
        orch._store.save(state)

        orch._run_single_pass(state)

        assert state.failed
        assert state.done is False
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")
        assert state.history[-1]["action"] == "abort"

    def test_execution_gate_audit_treats_dot_test_write_root_as_project_root(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            project_config={
                "project": {"build_tool": "rust"},
                "sandbox": {"allowed_test_write_paths": ["."]},
            },
        )
        source_file = tmp_path / "src" / "lib.rs"
        source_file.parent.mkdir()
        source_file.write_text(
            """\
#[cfg(test)]
mod tests {
    #[ignore]
    #[test]
    fn generated_contract_test() {}
}
""",
            encoding="utf-8",
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/lib.rs"])

        findings = orch._audit_test_execution_gates_after_agent(
            state,
            source="test_writer",
            files_written=["src/lib.rs"],
            before_snapshot={"src/lib.rs": None},
        )

        assert len(findings) == 1
        assert findings[0]["path"] == "src/lib.rs"
        assert findings[0]["reason"] == "Rust ignored test"
        assert len(state.test_execution_gate_records) == 1
        assert state.test_errors[0].startswith("TEST EXECUTION GATE AUDIT:")

    def test_test_writer_dot_root_snapshot_omits_unrelated_source_but_keeps_gate_counts(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "node"},
                "sandbox": {"allowed_test_write_paths": ["."]},
            },
        )
        unrelated = tmp_path / "src" / "unrelated.ts"
        test_file = tmp_path / "tests" / "existing.test.ts"
        unrelated.parent.mkdir()
        test_file.parent.mkdir()
        unrelated.write_text("export const unrelated = 'do not snapshot';\n", encoding="utf-8")
        test_file.write_text('test.skip("external service contract", () => {});\n', encoding="utf-8")
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(
            ["git", "-c", "user.email=test@example.com", "-c", "user.name=Test User", "commit", "-m", "baseline"],
            cwd=tmp_path,
            check=True,
            capture_output=True,
        )
        snapshots: list[dict[str, str | None]] = []

        def test_writer_effect(state: TaskState) -> None:
            snapshot = orch._store.load_text_snapshot("t1", orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT)
            assert snapshot is not None
            snapshots.append(snapshot)
            test_file.write_text(
                "\n".join(
                    [
                        'test.skip("external service contract", () => {});',
                        'test.skip("external service contract", () => {});',
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            state.tests_up_to_date = True
            state.files_changed.append("tests/existing.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/existing.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.ts"])

        assert orch._run_test_write_phase(state)

        assert snapshots == [{"src/main.ts": None}]
        findings = state.test_execution_gate_records[0]["findings"]
        assert len(findings) == 1
        assert findings[0]["path"] == "tests/existing.test.ts"
        assert findings[0]["baseline_count"] == 1
        assert findings[0]["occurrence"] == 2

    def test_resume_recovers_pending_synthetic_harness_and_retries_test_writer(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        pre_agent_test = "test('previous valid seam', () => expect(true).toBe(true));\n"
        test_file.write_text(
            pre_agent_test
            + "\n".join(
                [
                    "test.skip('generated harness', () => {});",
                    "class FakeEventTarget { addEventListener() {}; dispatchEvent() {} }",
                    "class FakeHistory { pushState() {} }",
                    "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                ]
            ),
            encoding="utf-8",
        )
        orch._store.save_text_snapshot(
            "t1",
            orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT,
            {"tests/client_main.test.ts": pre_agent_test},
        )
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            saved = orch._store.load("t1")
            assert saved is not None
            assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
            assert "tests/client_main.test.ts" in saved.files_changed
            assert test_file.read_text(encoding="utf-8") == pre_agent_test
            test_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
            state.tests_up_to_date = True
            state.files_changed.append("tests/client_main.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/client_main.test.ts"],
            test_files_written=["tests/client_main.test.ts"],
            tests_up_to_date=True,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=True,
            test_writer_audit_gate_counts={"tests/client_main.test.ts": {}},
        )

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 1
        assert state.test_writer_audit_pending is False
        assert state.test_writer_audit_files_written == []
        assert state.test_writer_audit_gate_counts == {}
        assert orch._store.load_text_snapshot("t1", orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT) is None
        assert len(state.test_execution_gate_records) == 1
        assert state.test_execution_gate_records[0]["findings"][0]["path"] == "tests/client_main.test.ts"
        assert len(state.synthetic_test_harness_records) == 1
        assert state.synthetic_test_harness_records[0]["findings"][0]["path"] == "tests/client_main.test.ts"
        assert state.synthetic_test_harness_records[0]["status"] == "resolved"
        assert test_file.read_text(encoding="utf-8") == "test('view model seam', () => expect(true).toBe(true));\n"

    def test_test_writer_synthetic_harness_recovery_retries_with_narrow_test(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeEventTarget { addEventListener() {}; dispatchEvent() {} }",
                            "class FakeElement extends FakeEventTarget { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                saved = orch._store.load("t1")
                assert saved is not None
                assert saved.tests_up_to_date is False
                assert saved.test_writer_audit_agent_completed is False
                assert "tests/client_main.test.ts" not in saved.files_changed
                assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
                test_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
            state.tests_up_to_date = True
            state.files_changed.append("tests/client_main.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 2
        assert state.failed is False
        assert state.test_errors == []
        assert len(state.synthetic_test_harness_records) == 1
        assert state.synthetic_test_harness_records[0]["source"] == "test_writer"
        assert state.synthetic_test_harness_records[0]["status"] == "resolved"
        assert test_file.read_text(encoding="utf-8") == "test('view model seam', () => expect(true).toBe(true));\n"
        assert any(record["action"] == "synthetic_test_harness_audit" for record in state.history)
        assert any(record["action"] == "synthetic_test_harness_recovered" for record in state.history)

    def test_test_writer_repeated_synthetic_harness_is_removed_and_gap_recorded(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text(
                "\n".join(
                    [
                        "class FakeElement { appendChild() {}; querySelector() {} }",
                        "class FakeHistory { pushState() {} }",
                        "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                    ]
                ),
                encoding="utf-8",
            )
            state.tests_up_to_date = True
            state.files_changed.append("tests/client_main.test.ts")

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            plan=["Add production behavior", "Harden tests"],
            plan_decided=True,
            step_file_tracking_enabled=True,
            step_files_changed=["src/main.py"],
            files_changed=["src/main.py"],
        )

        changed = orch._run_test_write_phase(state)

        assert not changed
        assert state.failed is False
        assert not test_file.exists()
        assert len(state.synthetic_test_harness_records) == 1
        assert all(record["status"] == "resolved" for record in state.synthetic_test_harness_records)
        assert len(state.testability_gaps) == 1
        assert state.testability_gaps[0]["target"] == "synthetic runtime harness in tests/client_main.test.ts"
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/client_main.test.ts" not in state.step_files_changed
        assert "tests/client_main.test.ts" not in state.test_files_written

    def test_test_writer_synthetic_harness_recovery_preserves_other_written_tests(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        synthetic_file = tmp_path / "tests" / "client_main.test.ts"
        narrow_file = tmp_path / "tests" / "view_model.test.ts"
        synthetic_file.parent.mkdir()
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                synthetic_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                narrow_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
                state.files_changed.extend(["tests/client_main.test.ts", "tests/view_model.test.ts"])
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert changed
        assert attempts["count"] == 2
        assert not synthetic_file.exists()
        assert narrow_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/view_model.test.ts" in state.files_changed

    def test_test_writer_synthetic_harness_recovery_drops_clean_retry_reported_file(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def test_writer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                assert not test_file.exists()
                state.record_testability_gap(
                    "test_writer",
                    "TESTABILITY GAP:\ntarget: browser route harness\nreason: no stable DOM seam",
                    target="browser route harness",
                    reason="no stable DOM seam",
                )
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])

        changed = orch._run_test_write_phase(state)

        assert not changed
        assert attempts["count"] == 2
        assert not test_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/client_main.test.ts" not in state.test_files_written
        assert len(state.testability_gaps) == 1

    def test_test_writer_synthetic_harness_audit_uses_pre_agent_dirty_baseline(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "project"
        test_file = project / "tests" / "client_main.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test('existing narrow test', () => {});\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add narrow test"], cwd=repo, check=True, capture_output=True)
        dirty_harness = "\n".join(
            [
                "class FakeElement { appendChild() {}; querySelector() {} }",
                "class FakeHistory { pushState() {} }",
                "class FakeMouseEvent { preventDefault() {} }",
            ]
        )
        test_file.write_text(dirty_harness, encoding="utf-8")
        orch, stubs, _ = _make_orchestrator(
            project,
            run_build=False,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def test_writer_effect(state: TaskState) -> None:
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\ntest('uses helper', () => {});\n")
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])

        assert orch._run_test_write_phase(state)

        assert state.synthetic_test_harness_records == []
        assert "test('uses helper'" in test_file.read_text(encoding="utf-8")

    def test_synthetic_harness_audit_catches_cumulative_test_harness(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text(
            "\n".join(
                [
                    "class FakeElement { appendChild() {}; querySelector() {} }",
                    "class FakeHistory { pushState() {} }",
                ]
            ),
            encoding="utf-8",
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])
        attempts = {"count": 0}

        def fixer_effect(_state: TaskState) -> None:
            attempts["count"] += 1
            test_file.write_text(
                "\n".join(
                    [
                        "class FakeElement { appendChild() {}; querySelector() {} }",
                        "class FakeHistory { pushState() {} }",
                        "class FakeMouseEvent { preventDefault() {} }",
                    ]
                ),
                encoding="utf-8",
            )

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.failed is False
        assert state.test_errors == []
        assert attempts["count"] == 2
        assert len(state.synthetic_test_harness_records) == 1
        finding = state.synthetic_test_harness_records[0]["findings"][0]
        assert set(finding["subsystems"]) == {"event_dispatch", "navigation_history", "render_tree"}
        assert all(record["status"] == "resolved" for record in state.synthetic_test_harness_records)
        assert "FakeMouseEvent" not in test_file.read_text(encoding="utf-8")
        assert len(state.testability_gaps) == 1

        stubs["fixer"].side_effect = lambda _state: test_file.write_text(
            test_file.read_text(encoding="utf-8") + "\ntest('assertion tweak', () => {});\n",
            encoding="utf-8",
        )

        assert orch._run_fix_phase(state, "1/1")

        assert len(state.synthetic_test_harness_records) == 1

    def test_fixer_synthetic_harness_recovery_restores_validation_errors_for_retry(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.test_errors.clear()
                state.test_status = "success"
            else:
                saved = orch._store.load("t1")
                assert saved is not None
                assert saved.test_errors == ["tests failed"]
                assert saved.test_status == "failed"
                assert any(record["action"] == "synthetic_test_harness_recovered" for record in saved.history)
                assert state.test_errors == ["tests failed"]
                assert state.test_status == "failed"
                test_file.write_text("test('view model seam', () => expect(true).toBe(true));\n", encoding="utf-8")
            state.files_changed.append("tests/client_main.test.ts")

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            test_errors=["tests failed"],
            test_status="failed",
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert test_file.read_text(encoding="utf-8") == "test('view model seam', () => expect(true).toBe(true));\n"
        assert state.synthetic_test_harness_records[0]["status"] == "resolved"

    def test_fixer_synthetic_harness_recovery_aborts_failed_retry(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.files_changed.append("tests/client_main.test.ts")
            else:
                stubs["fixer"].result_success = False
                stubs["fixer"].result_message = "provider failure"

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
        )

        assert not orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert state.failed is True
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "fixer failed: provider failure"
            for entry in state.history
        )

    def test_fixer_synthetic_harness_recovery_preserves_other_changed_files(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        src_file = tmp_path / "src" / "main.py"
        test_file = tmp_path / "tests" / "client_main.test.ts"
        src_file.parent.mkdir()
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                src_file.write_text("def value():\n    return 1\n", encoding="utf-8")
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.test_errors.clear()
                state.files_changed.extend(["src/main.py", "tests/client_main.test.ts"])

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert src_file.exists()
        assert not test_file.exists()
        assert "src/main.py" in state.files_changed
        assert "tests/client_main.test.ts" not in state.files_changed
        assert state.review_approved is False
        assert state.security_approved is False
        assert state.tests_up_to_date is False

    def test_fixer_synthetic_harness_recovery_accepts_noop_retry_with_retained_fix(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        src_file = tmp_path / "src" / "main.py"
        test_file = tmp_path / "tests" / "client_main.test.ts"
        src_file.parent.mkdir()
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                stubs["fixer"].result_success = True
                stubs["fixer"].result_message = None
                src_file.write_text("def value():\n    return 1\n", encoding="utf-8")
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.files_changed.extend(["src/main.py", "tests/client_main.test.ts"])
            else:
                stubs["fixer"].result_success = False
                stubs["fixer"].result_message = "Agent made no file changes"

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert state.failed is False
        assert src_file.exists()
        assert not test_file.exists()
        assert "src/main.py" in state.files_changed
        assert "tests/client_main.test.ts" not in state.files_changed
        assert state.review_approved is False
        assert state.security_approved is False
        assert state.tests_up_to_date is False
        assert any(record["action"] == "synthetic_test_harness_recovery_noop_retry" for record in state.history)
        assert not any(
            record["action"] == "abort" and "Agent made no file changes" in record["result"] for record in state.history
        )

    def test_fixer_synthetic_harness_recovery_accepts_noop_retry_with_restored_tree_only(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                stubs["fixer"].result_success = True
                stubs["fixer"].result_message = None
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
                state.files_changed.append("tests/client_main.test.ts")
            else:
                state.record_testability_gap(
                    "fixer",
                    "TESTABILITY GAP:\ntarget: browser route harness\nreason: no stable DOM seam",
                    target="browser route harness",
                    reason="no stable DOM seam",
                )
                stubs["fixer"].result_success = False
                stubs["fixer"].result_message = "Agent made no file changes"

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_errors=["tests failed"],
            test_status="failed",
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert state.failed is False
        assert not test_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert any(record["action"] == "synthetic_test_harness_recovery_noop_retry" for record in state.history)
        assert not any(
            record["action"] == "abort" and "Agent made no file changes" in record["result"] for record in state.history
        )

    def test_fixer_synthetic_harness_recovery_drops_clean_retry_reported_file(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        attempts = {"count": 0}

        def fixer_effect(state: TaskState) -> None:
            attempts["count"] += 1
            if attempts["count"] == 1:
                test_file.write_text(
                    "\n".join(
                        [
                            "class FakeElement { appendChild() {}; querySelector() {} }",
                            "class FakeHistory { pushState() {} }",
                            "async function fakeFetch(input: Request): Promise<Response> { return new Response('{}'); }",
                        ]
                    ),
                    encoding="utf-8",
                )
            else:
                assert not test_file.exists()
                state.record_testability_gap(
                    "fixer",
                    "TESTABILITY GAP:\ntarget: browser route harness\nreason: no stable DOM seam",
                    target="browser route harness",
                    reason="no stable DOM seam",
                )

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=[],
            test_files_written=["tests/client_main.test.ts"],
            test_errors=["tests failed"],
            test_status="failed",
        )

        assert orch._run_fix_phase(state, "1/1")

        assert attempts["count"] == 2
        assert not test_file.exists()
        assert "tests/client_main.test.ts" not in state.files_changed
        assert "tests/client_main.test.ts" not in state.test_files_written
        assert len(state.testability_gaps) == 1

    def test_synthetic_harness_audit_uses_git_head_project_baseline(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "project"
        test_file = project / "tests" / "client_main.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.write_text(
            "\n".join(
                [
                    "class FakeElement { appendChild() {}; querySelector() {} }",
                    "class FakeHistory { pushState() {} }",
                    "class FakeMouseEvent { preventDefault() {} }",
                ]
            ),
            encoding="utf-8",
        )
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add test harness"], cwd=repo, check=True, capture_output=True)
        orch, stubs, _ = _make_orchestrator(
            project,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])

        def fixer_effect(_state: TaskState) -> None:
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\ntest('uses helper', () => {});\n")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.synthetic_test_harness_records == []

    def test_synthetic_harness_audit_uses_pre_agent_dirty_baseline(self, tmp_path: Path):
        repo = tmp_path / "repo"
        project = repo / "project"
        test_file = project / "tests" / "client_main.test.ts"
        test_file.parent.mkdir(parents=True)
        test_file.write_text("test('existing narrow test', () => {});\n", encoding="utf-8")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add narrow test"], cwd=repo, check=True, capture_output=True)
        dirty_harness = "\n".join(
            [
                "class FakeElement { appendChild() {}; querySelector() {} }",
                "class FakeHistory { pushState() {} }",
                "class FakeMouseEvent { preventDefault() {} }",
            ]
        )
        test_file.write_text(dirty_harness, encoding="utf-8")
        orch, stubs, _ = _make_orchestrator(
            project,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["tests/client_main.test.ts"])

        def fixer_effect(_state: TaskState) -> None:
            test_file.write_text(test_file.read_text(encoding="utf-8") + "\ntest('uses helper', () => {});\n")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.synthetic_test_harness_records == []
        assert "test('uses helper'" in test_file.read_text(encoding="utf-8")

    def test_synthetic_harness_audit_resolves_when_test_is_narrowed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        test_file = tmp_path / "tests" / "client_main.test.ts"
        test_file.parent.mkdir()
        test_file.write_text("class FakeHistory {}\n", encoding="utf-8")
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"])
        state.record_synthetic_test_harness_audit(
            "test_writer",
            [
                {
                    "path": "tests/client_main.test.ts",
                    "subsystems": ["navigation_history", "event_dispatch", "network_server"],
                    "evidence": [
                        {
                            "category": "navigation_history",
                            "lines": [{"line": 1, "excerpt": "class FakeHistory {}"}],
                        }
                    ],
                }
            ],
        )

        def fixer_effect(_state: TaskState) -> None:
            test_file.write_text("def test_view_model_contract():\n    assert True\n", encoding="utf-8")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/client_main.test.ts"]}

        assert orch._run_fix_phase(state, "1/1")

        assert state.synthetic_test_harness_records[0]["status"] == "resolved"
        assert "resolved_at" in state.synthetic_test_harness_records[0]

    def test_final_scope_test_only_fix_resets_final_gate_and_security(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )

        def fixer_effect(state: TaskState) -> None:
            state.files_changed.append("tests/test_main.py")

        stubs["fixer"].side_effect = fixer_effect
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            active_scope="final_full_task",
            final_full_task_review_done=True,
            review_approved=True,
            security_approved=True,
            security_review_iterations=2,
            tests_up_to_date=True,
        )

        assert orch._run_fix_phase(state, "1/3")

        assert state.review_approved
        assert state.tests_up_to_date
        assert not state.security_approved
        assert state.security_review_iterations == 0
        assert not state.final_full_task_review_done
        assert any(record["action"] == "test_only_fix" for record in state.history)

    def test_fix_phase_fails_task_when_fixer_result_is_unsuccessful(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=True)
        stubs["fixer"].result_success = False
        stubs["fixer"].result_message = "Agent made no file changes"
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            errors=["compile failed"],
        )

        assert not orch._run_fix_phase(state, "1/3")

        assert state.failed
        assert any(
            record["agent"] == "orchestrator"
            and record["action"] == "abort"
            and "Agent made no file changes" in record["result"]
            for record in state.history
        )

    def test_test_only_fix_with_broad_android_root_requires_test_artifact_path(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["feature/"]},
            },
        )
        test_results = [False, True]

        def run_tests() -> ToolResult:
            build.test_calls += 1
            success = test_results.pop(0)
            return ToolResult(success=success, output="", error="" if success else "tests failed")

        def fixer_effect(state: TaskState) -> None:
            path = "feature/countries/src/test/kotlin/com/example/countries/CountriesModuleTest.kt"
            if path not in state.files_changed:
                state.files_changed.append(path)

        build.run_tests = run_tests  # type: ignore[method-assign]
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["feature/countries/src/main/kotlin/com/example/countries/CountriesRoutes.kt"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.review_approved
        assert result.security_approved
        assert result.tests_up_to_date
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 0
        assert any(record["action"] == "test_only_fix" for record in result.history)

    def test_test_writer_agent_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=True,
        )
        stubs["test_writer"].result_success = False
        stubs["test_writer"].result_message = "quota exhausted"
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            security_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert not result.done
        assert len(stubs["test_writer"].calls) == 1
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "test_writer failed: quota exhausted"
            for entry in result.history
        )

    def test_production_fix_under_broad_test_root_stales_semantic_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["feature/"]},
            },
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            path = "feature/countries/src/main/kotlin/com/example/countries/CountriesRoutes.kt"
            if path not in state.files_changed:
                state.files_changed.append(path)

        def test_writer_effect(state: TaskState) -> None:
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["feature/countries/src/main/kotlin/com/example/countries/CountriesScreen.kt"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        assert not any(record["action"] == "test_only_fix" for record in result.history)

    def test_production_fix_under_directory_ending_test_does_not_count_as_test_only(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["feature/"]},
            },
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            path = "feature/latest/src/main/App.ts"
            if path not in state.files_changed:
                state.files_changed.append(path)

        def test_writer_effect(state: TaskState) -> None:
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["feature/latest/src/main/Existing.ts"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        assert not any(record["action"] == "test_only_fix" for record in result.history)

    def test_mixed_test_and_production_fix_stales_semantic_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            max_iterations=3,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        build.compile_results = [False, True]

        def fixer_effect(state: TaskState) -> None:
            for path in ["src/fix.py", "tests/test_main.py"]:
                if path not in state.files_changed:
                    state.files_changed.append(path)

        def test_writer_effect(state: TaskState) -> None:
            state.tests_up_to_date = True

        stubs["fixer"].side_effect = fixer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        assert not any(record["action"] == "test_only_fix" for record in result.history)


# ---------------------------------------------------------------------------
# Tests — interrupt / resume scenarios
# ---------------------------------------------------------------------------


class TestOrchestratorInterruptResume:
    def test_resume_after_interrupt_between_fixer_and_reviewer_runs_review(self, tmp_path: Path):
        """Interrupt after fixer ran and flags were reset+saved, reviewer had not yet run.
        On resume, review_approved=False must cause reviewer to run."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False, run_review=True, max_review_iterations=2)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_approved=False)
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) >= 1

    def test_resume_after_interrupt_between_security_fix_and_review_runs_review(self, tmp_path: Path):
        """Interrupt after security-fix implementer ran and review_approved was reset+saved,
        but review loop had not yet run. On resume, reviewer must run."""
        orch, stubs, _ = _make_orchestrator(
            tmp_path, run_build=False, run_review=True, run_security_review=True, max_review_iterations=2
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        # Simulates state saved after security-fix implementer reset review_approved
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=False,
            security_approved=False,
        )
        orch.run(task_id="t1")
        assert len(stubs["reviewer"].calls) >= 1

    def test_resume_with_tests_up_to_date_false_reruns_test_writer(self, tmp_path: Path):
        """Interrupt after implementer fix reset tests_up_to_date but before test writer ran.
        On resume, test writer must run."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False, run_review=True, run_test_writing=True)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=False,
            tests_up_to_date=False,
        )
        orch.run(task_id="t1")
        assert len(stubs["test_writer"].calls) >= 1

    def test_resume_pending_test_writer_audit_before_agent_completion_reruns_test_writer(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        partial_test = tmp_path / "tests" / "test_main.py"
        partial_test.parent.mkdir()
        orch._store.save_text_snapshot(
            "t1",
            orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT,
            {},
        )
        partial_test.write_text('test.skip("partial generated placeholder", () => {});\n', encoding="utf-8")

        def test_writer_effect(state: TaskState) -> None:
            assert not partial_test.exists()
            partial_test.write_text("def test_generated_behavior():\n    assert True\n", encoding="utf-8")
            state.tests_up_to_date = True
            state.files_changed.append("tests/test_main.py")

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": ["tests/test_main.py"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            tests_up_to_date=False,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=False,
            test_writer_audit_gate_counts={"tests/test_main.py": {}},
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["test_writer"].calls) == 1
        assert result.tests_up_to_date is True
        assert result.test_writer_audit_pending is False
        assert result.test_writer_audit_agent_completed is False
        assert not result.test_execution_gate_records
        assert partial_test.read_text(encoding="utf-8") == "def test_generated_behavior():\n    assert True\n"
        assert orch._store.load_text_snapshot(state.task_id, orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT) is None

    def test_resume_pending_test_writer_audit_restores_unaudited_state_paths_before_rerun(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_test_writing=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        unaudited_test = tmp_path / "tests" / "test_main.py"
        unaudited_test.parent.mkdir()
        orch._store.save_text_snapshot(
            "t1",
            orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT,
            {},
        )
        unaudited_test.write_text('test.skip("partial generated placeholder", () => {});\n', encoding="utf-8")

        def test_writer_effect(state: TaskState) -> None:
            assert not unaudited_test.exists()
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        stubs["test_writer"].result_data = {"files_written": []}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            test_files_written=["tests/test_main.py"],
            review_approved=True,
            tests_up_to_date=False,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=False,
            test_writer_audit_gate_counts={"tests/test_main.py": {}},
        )

        changed = orch._run_test_write_phase(state)

        assert changed is False
        assert len(stubs["test_writer"].calls) == 1
        assert not unaudited_test.exists()
        assert state.tests_up_to_date is True
        assert state.test_writer_audit_pending is False
        assert "tests/test_main.py" not in state.files_changed
        assert "tests/test_main.py" not in state.test_files_written
        assert state.test_execution_gate_records == []
        assert any(record["action"] == "test_writer_interrupted_output_restored" for record in state.history)
        assert orch._store.load_text_snapshot(state.task_id, orchestrator_module._TEST_WRITER_AUDIT_SNAPSHOT) is None

    def test_resume_pending_test_writer_audit_before_agent_completion_fails_without_snapshot(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_test_writing=True,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_approved=True,
            tests_up_to_date=False,
            test_writer_audit_pending=True,
            test_writer_audit_agent_completed=False,
        )

        result = orch.run(task_id="t1")

        assert result.failed
        assert len(stubs["test_writer"].calls) == 0
        assert result.history[-1]["action"] == "abort"
        assert "snapshot missing" in result.history[-1]["result"]

    def test_restore_test_writer_snapshot_rejects_symlink_parent(self, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        linked_tests = project / "tests"
        try:
            linked_tests.symlink_to(outside, target_is_directory=True)
        except OSError as exc:
            pytest.skip(f"symlink creation failed: {exc}")
        orch, _, _ = _make_orchestrator(project)
        state = _save_state(orch, implementation_prompt="p")

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/test_main.py"],
            before_snapshot={"tests/test_main.py": "def test_generated():\n    assert True\n"},
        )

        assert restored == []
        assert errors
        assert "symlink component" in errors[0]
        assert not (outside / "test_main.py").exists()

    def test_restore_test_writer_snapshot_rejects_symlink_target(self, tmp_path: Path):
        project = tmp_path / "project"
        project.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        outside_file = outside / "test_main.py"
        outside_file.write_text("outside\n", encoding="utf-8")
        tests_dir = project / "tests"
        tests_dir.mkdir()
        linked_test = tests_dir / "test_main.py"
        try:
            linked_test.symlink_to(outside_file)
        except OSError as exc:
            pytest.skip(f"symlink creation failed: {exc}")
        orch, _, _ = _make_orchestrator(project)
        state = _save_state(orch, implementation_prompt="p")

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/test_main.py"],
            before_snapshot={"tests/test_main.py": "def test_generated():\n    assert True\n"},
        )

        assert restored == []
        assert errors
        assert "symlink component" in errors[0]
        assert outside_file.read_text(encoding="utf-8") == "outside\n"

    def test_review_fix_restore_preserves_branch_diff_test_file(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        branch_text = "def test_existing_branch_change():\n    assert True\n"
        test_file.write_text("class FakeElement:\n    pass\n", encoding="utf-8")
        monkeypatch.setattr(orch, "_path_has_pending_changes", lambda _path: False)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            test_files_written=["tests/test_main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
        )

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/test_main.py"],
            before_snapshot={"tests/test_main.py": branch_text},
        )

        assert restored == ["tests/test_main.py"]
        assert errors == []
        assert test_file.read_text(encoding="utf-8") == branch_text
        assert "tests/test_main.py" in state.files_changed
        assert "tests/test_main.py" not in state.test_files_written

    def test_review_fix_restore_prunes_new_generated_test_file(self, tmp_path: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(tmp_path)
        test_file = tmp_path / "tests" / "generated_test.py"
        test_file.parent.mkdir()
        test_file.write_text("class FakeElement:\n    pass\n", encoding="utf-8")
        monkeypatch.setattr(orch, "_path_has_pending_changes", lambda _path: False)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/generated_test.py"],
            test_files_written=["tests/generated_test.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
        )

        restored, errors = orch._restore_test_file_paths_from_snapshot(
            state,
            paths=["tests/generated_test.py"],
            before_snapshot={"tests/generated_test.py": None},
        )

        assert restored == ["tests/generated_test.py"]
        assert errors == []
        assert not test_file.exists()
        assert "tests/generated_test.py" not in state.files_changed
        assert "tests/generated_test.py" not in state.test_files_written

    def test_step_loop_runs_implementer_per_step(self, tmp_path: Path):
        """Step loop: implementer must run once per step."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{len(stubs['implementer'].calls)}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 2

    def test_step_file_tracking_resets_between_steps(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=True,
        )
        test_writer_contexts: list[tuple[str | None, list[str]]] = []

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        def test_writer_effect(state: TaskState) -> None:
            test_writer_contexts.append((state.active_scope, list(state.step_files_changed)))
            state.tests_up_to_date = True

        stubs["implementer"].side_effect = implementer_effect
        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            step_file_tracking_enabled=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert test_writer_contexts == [
            (None, ["src/step0.py"]),
            (None, ["src/step1.py"]),
            ("final_full_task", ["src/step1.py"]),
        ]

    def test_step_file_tracking_ignores_malformed_persisted_evidence(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path)
        state = TaskState(
            task_id="t1",
            task_description="test task",
            plan=["Step 1", "Step 2"],
            step_file_tracking_enabled=True,
            step_files_changed=True,  # type: ignore[arg-type]
        )

        orch._record_step_files_changed(state, ["src/current.py"])

        assert state.step_files_changed is True

    def test_step_loop_implementer_failure_aborts_before_review(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)
        stubs["implementer"].result_success = False
        stubs["implementer"].result_message = "usage limit reached"
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.failed is True
        assert saved is not None
        assert saved.failed is True
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 0
        assert any(
            entry["agent"] == "orchestrator"
            and entry["action"] == "abort"
            and entry["result"] == "implementer failed: usage limit reached"
            for entry in saved.history
        )

    def test_step_loop_max_review_iterations_persists_failed_state(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            max_review_iterations=1,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = False
            state.review_issues = ["future step is missing"]

        def implementer_effect(state: TaskState) -> None:
            if not state.files_changed:
                state.files_changed.append("src/step0.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.failed is True
        assert saved.failed is True
        assert saved.done is False
        assert any(h["action"] == "abort" for h in saved.history)

    def test_step_loop_resume_at_review_limit_allows_approval_before_abort(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            max_review_iterations=3,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=1,
            step_implemented=True,
            files_changed=["src/step0.py"],
            review_approved=False,
            review_iterations=3,
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.done is True
        assert saved.done is True
        assert saved.failed is False
        assert len(stubs["reviewer"].calls) == 2
        assert len(stubs["implementer"].calls) == 0

    def test_step_loop_resume_reruns_current_step(self, tmp_path: Path):
        """Interrupt during step 1, resume: step 1 implementer must run again."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        # Simulate interrupt during step 0: step_implemented=False (implementer not yet done)
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=0,
            step_implemented=False,
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 2

    def test_step_loop_resume_preserves_current_step_file_tracking(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=True,
        )
        test_writer_contexts: list[tuple[str | None, list[str]]] = []

        def test_writer_effect(state: TaskState) -> None:
            test_writer_contexts.append((state.active_scope, list(state.step_files_changed)))
            state.tests_up_to_date = True

        stubs["test_writer"].side_effect = test_writer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=1,
            step_implemented=True,
            step_file_tracking_enabled=True,
            step_files_changed=["src/current.py"],
            files_changed=["src/previous.py", "src/current.py"],
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["implementer"].calls) == 0
        assert test_writer_contexts == [
            (None, ["src/current.py"]),
            ("final_full_task", ["src/current.py"]),
        ]

    def test_delivery_single_pass_already_satisfied_runs_remaining_gates(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=False,
        )

        def already_satisfied(state: TaskState) -> None:
            state.delivery_no_change_outcome = "already_satisfied"

        stubs["implementer"].side_effect = already_satisfied
        stubs["implementer"].result_data = {
            "files_written": [],
            "implementation_outcome": "already_satisfied",
        }
        _save_state(
            orch,
            implementation_prompt="p",
            plan_decided=True,
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
        )

        result = orch.run(task_id="t1")

        assert result.done is True
        assert result.failed is False
        assert result.files_changed == []
        assert len(stubs["implementer"].calls) == 1
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert build.compile_calls == 1
        assert build.test_calls == 1
        assert any(record["action"] == "implementation_already_satisfied" for record in result.history)

    def test_delivery_single_pass_rejects_unclassified_no_change_result(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_review=True,
            run_security_review=True,
            run_test_writing=False,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            plan_decided=True,
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
        )

        result = orch.run(task_id="t1")

        assert result.done is False
        assert result.failed is True
        assert not stubs["reviewer"].calls
        assert not stubs["security_reviewer"].calls
        assert build.compile_calls == 0

    def test_delivery_single_pass_resumes_after_persisted_already_satisfied_outcome(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_build=False,
            run_review=True,
            run_security_review=True,
            run_test_writing=False,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            plan_decided=True,
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_no_change_outcome="already_satisfied",
        )

        result = orch.run(task_id="t1")

        assert result.done is True
        assert result.failed is False
        assert not stubs["implementer"].calls
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1

    def test_delivery_step_plan_accepts_only_explicit_already_satisfied_noops(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def already_satisfied(state: TaskState) -> None:
            state.delivery_no_change_outcome = "already_satisfied"

        stubs["implementer"].side_effect = already_satisfied
        stubs["implementer"].result_data = {
            "files_written": [],
            "implementation_outcome": "already_satisfied",
        }
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: verify registration", "Step 2: verify wiring"],
            plan_decided=True,
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 2},
        )

        result = orch.run(task_id="t1")

        assert result.done is True
        assert result.failed is False
        assert result.files_changed == []
        assert len(stubs["implementer"].calls) == 2
        assert [record["action"] for record in result.history].count("step_already_satisfied") == 2
        assert any(record["action"] == "plan_already_satisfied" for record in result.history)

    def test_step_loop_no_changes_advances_to_next_step(self, tmp_path: Path):
        """Step with no file changes must not abort — treat as already implemented and advance."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        call_count = [0]

        def implementer_effect(state: TaskState) -> None:
            call_count[0] += 1
            if call_count[0] == 1:
                pass  # step 0: no changes — already implemented
            else:
                state.files_changed.append("src/step1.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: already done", "Step 2: add feature B"],
            plan_decided=True,
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 2
        history_actions = [h["action"] for h in result.history]
        assert "step_skipped" in history_actions

    def test_step_loop_skips_completed_step(self, tmp_path: Path):
        """Interrupt after step 0 completed: on resume, step 0 implementer must not run again."""
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        # Simulate interrupt after step 0 completed: current_step=1, step_implemented=False
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add feature A", "Step 2: add feature B"],
            plan_decided=True,
            current_step=1,
            step_implemented=False,
            files_changed=["src/step0.py"],
        )
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["implementer"].calls) == 1  # only step 1, not step 0 again

    def test_multi_step_run_gets_final_full_task_review_before_final_build(self, tmp_path: Path, caplog):
        import logging

        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=False,
            run_security_review=False,
            run_test_writing=False,
        )

        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        with caplog.at_level(logging.INFO, logger="core.orchestrator"):
            result = orch.run(task_id="t1")

        assert result.done
        assert result.plan_completed is True
        assert result.final_full_task_review_done is True
        assert review_scopes == ["step", "step", "final_full_task"]
        assert build.compile_calls == 1
        messages = [record.message for record in caplog.records]
        assert "--- Phase: final full-task gate ---" in messages
        assert "--- Phase: final full-task review (initial) ---" in messages

    def test_deferred_final_build_fixer_reruns_review_in_final_scope(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=False,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=3,
        )
        build.compile_results = [False, True]
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        def fixer_effect(state: TaskState) -> None:
            state.files_changed.append("src/final_build_fix.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.final_full_task_review_done is True
        assert review_scopes == ["step", "step", "final_full_task", "final_full_task"]
        assert build.compile_calls == 2

    def test_build_per_step_fixer_review_stays_step_scoped_before_final_gate(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=6,
        )
        build.compile_results = [False, True, True, True]
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        def fixer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}_build_fix.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["fixer"].side_effect = fixer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.final_full_task_review_done is True
        assert review_scopes == ["step", "step", "step", "final_full_task"]
        assert build.compile_calls == 4
        build_records = [r for r in result.validation_cycle_records if r["phase"] == "build"]
        assert build_records[-1]["scope"] == "final_full_task"

    def test_build_per_step_budget_does_not_starve_final_build(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=True,
            run_build_per_step=True,
            run_security_review=False,
            run_test_writing=False,
            max_iterations=2,
        )

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert not result.failed
        assert result.build_iterations == 3
        assert build.compile_calls == 3
        build_records = [r for r in result.validation_cycle_records if r["phase"] == "build"]
        assert [r.get("scope") for r in build_records] == [None, None, "final_full_task"]

    def test_final_gate_completes_when_optional_agents_are_disabled(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{state.current_step}.py")

        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.plan_completed is True
        assert result.final_full_task_review_done is True
        assert len(stubs["reviewer"].calls) == 0
        assert len(stubs["security_reviewer"].calls) == 0
        assert len(stubs["test_writer"].calls) == 0

    def test_plan_completed_resume_skips_last_step_and_runs_final_gate(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_planner=True,
            run_build=False,
            run_security_review=False,
            run_test_writing=False,
        )
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or "step")
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            plan=["Step 1: add parser", "Step 2: add evaluator"],
            plan_decided=True,
            plan_completed=True,
            current_step=1,
            step_implemented=True,
            files_changed=["src/step0.py", "src/step1.py"],
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["implementer"].calls) == 0
        assert review_scopes == ["final_full_task"]
        assert result.final_full_task_review_done is True


# ---------------------------------------------------------------------------
# Tests — preexisting-changes fast-path (review --fix with no fixer needed)
# ---------------------------------------------------------------------------


_REVIEW_DIFF = "@@ -1 +1 @@\n+x"  # minimal non-empty diff marking a task as a review task


def _git_diff_refresh_subprocess(cmd, **kwargs):
    if cmd[0:2] == ["git", "merge-base"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="base123\n", stderr="")
    if cmd[0:2] == ["git", "diff"]:
        return subprocess.CompletedProcess(cmd, 0, stdout="@@ fresh diff\n+current\n", stderr="")
    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")


class TestOrchestratorPreexistingChangesFastPath:
    def test_review_fix_without_plan_stays_task_scoped(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=False,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        review_scopes: list[str] = []

        def reviewer_effect(state: TaskState) -> None:
            review_scopes.append(state.active_scope or ("step" if state.plan else "task"))
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            plan_decided=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert review_scopes == ["task"]
        assert result.plan_completed is False
        assert result.final_full_task_review_done is False

    def test_review_fix_fast_path_runs_build_loop_for_active_execution_gate(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=True,
            project_config={
                "project": {"build_tool": "python"},
                "sandbox": {"allowed_test_write_paths": ["tests/"]},
            },
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        test_file = tmp_path / "tests" / "test_main.py"
        test_file.parent.mkdir()
        test_file.write_text('test.skip("changed behavior", () => {});\n', encoding="utf-8")

        def fixer_effect(state: TaskState) -> None:
            test_file.unlink()
            state.files_changed.append("tests/test_main.py")

        stubs["fixer"].side_effect = fixer_effect
        stubs["fixer"].result_data = {"files_written": ["tests/test_main.py"]}
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py", "tests/test_main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            plan_decided=True,
            tests_up_to_date=True,
        )
        state.record_test_execution_gate_audit(
            "test_writer",
            detect_new_test_execution_gates(
                path="tests/test_main.py",
                before=None,
                after='test.skip("changed behavior", () => {});\n',
            ),
        )
        orch._store.save(state)

        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["fixer"].calls) == 1
        assert any(entry["action"] == "review_fast_path_blocked" for entry in result.history)
        assert result.test_execution_gate_records[0]["status"] == "resolved"
        assert not test_file.exists()

    def test_review_fix_skips_validation_coverage_preflight_abort(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=False,
            project_config={
                "project": {"build_tool": "cargo"},
                "build": {"test_command": "cargo test"},
            },
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.rs"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            plan_decided=True,
        )
        state.task_description = "Review branch.\n\n## Verification\n\ncargo test --workspace --all-features\n"
        orch._store.save(state)

        result = orch.run(task_id="t1")

        assert result.done
        assert stubs["reviewer"].calls
        assert not any(entry["phase"] == "validation_coverage" for entry in result.validation_cycle_records)

    def test_review_fix_refreshes_review_diff_before_review(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=False,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)

        def reviewer_effect(state: TaskState) -> None:
            assert state.review_diff == "@@ fresh diff\n+current\n"
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff="@@ stale diff\n+old",
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            build_synced=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert result.review_diff == "@@ fresh diff\n+current\n"

    def test_review_fix_reviews_test_writer_changes(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=False,
            max_review_iterations=3,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)

        review_calls = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                state.review_approved = False
                state.review_issues = ["needs branch fix"]
                return
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append("src/fix.py")

        def test_writer_effect(state: TaskState) -> None:
            if "tests/test_fix.py" not in state.files_changed:
                state.files_changed.append("tests/test_fix.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["test_writer"].side_effect = test_writer_effect

        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            build_synced=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert review_calls["n"] == 3
        assert "tests/test_fix.py" in result.files_changed

    def test_review_fix_rejects_test_writer_changes_without_rerunning_test_writer(self, tmp_path: Path, monkeypatch):
        orch, stubs, _ = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
            run_build=False,
            max_review_iterations=3,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)

        review_calls = {"n": 0}
        test_writer_calls = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            review_calls["n"] += 1
            if review_calls["n"] == 1:
                state.review_approved = False
                state.review_issues = ["needs branch fix"]
                return
            if review_calls["n"] == 3:
                state.review_approved = False
                state.review_issues = ["test writer changes are not acceptable"]
                return
            state.review_approved = True
            state.review_issues.clear()

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/fix_{review_calls['n']}.py")

        def test_writer_effect(state: TaskState) -> None:
            test_writer_calls["n"] += 1
            state.files_changed.append(f"tests/test_fix_{test_writer_calls['n']}.py")

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].side_effect = implementer_effect
        stubs["test_writer"].side_effect = test_writer_effect

        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            review_diff=_REVIEW_DIFF,
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            build_synced=True,
        )
        result = orch.run(task_id="t1")

        assert result.failed
        assert not result.done
        assert review_calls["n"] == 3
        assert test_writer_calls["n"] == 1
        assert "tests/test_fix_1.py" in result.files_changed
        assert "tests/test_fix_2.py" not in result.files_changed

    def test_build_and_test_writer_skipped_when_reviewer_approves_without_fixes(self, tmp_path: Path):
        """review --fix fast-path: files pre-exist, reviewer approves, nothing written → skip build."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_test_writing=True,
            run_build=True,
            run_tests=True,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], review_diff=_REVIEW_DIFF)
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["test_writer"].calls) == 0
        assert build.compile_calls == 0
        assert build.test_calls == 0
        assert result.test_status == "skipped"
        assert result.check_status == "skipped"

    def test_build_runs_when_review_issues_required_implementer_fix(self, tmp_path: Path):
        """When reviewer finds issues and implementer runs to fix them, build must still run."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
            run_tests=True,
            max_review_iterations=3,
        )

        review_call_count = {"n": 0}

        def reviewer_effect(state: TaskState) -> None:
            review_call_count["n"] += 1
            if review_call_count["n"] >= 2:
                state.review_approved = True
                state.review_issues.clear()
            else:
                state.review_issues = ["needs fix"]

        stubs["reviewer"].side_effect = reviewer_effect
        stubs["implementer"].result_data = {"files_written": ["src/main.py"]}
        _save_state(
            orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True, review_diff=_REVIEW_DIFF
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls >= 1

    def test_fast_path_not_triggered_when_review_was_already_approved_before_session(self, tmp_path: Path):
        """If review_approved=True at session start (previous session), reviewer is skipped →
        fast-path must NOT trigger — build should still run."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
        )
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_approved=True,
            review_diff=_REVIEW_DIFF,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert len(stubs["reviewer"].calls) == 0  # reviewer skipped (already approved)
        assert build.compile_calls >= 1

    def test_fast_path_not_triggered_when_fixer_changed_code(self, tmp_path: Path):
        """review --fix resume edge case: fixer changed code in a previous session
        (fixer_changed_code=True), reviewer approves this session — build must still run."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
            run_tests=True,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            review_diff=_REVIEW_DIFF,
            fixer_changed_code=True,
        )
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls >= 1

    def test_review_fix_resume_active_build_loop_validates_before_re_review(self, tmp_path: Path, monkeypatch):
        """review --fix resume with an active build loop must validate before refreshing review."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
            run_build=True,
            run_tests=True,
        )
        monkeypatch.setattr(subprocess, "run", _git_diff_refresh_subprocess)
        events: list[str] = []
        original_compile_check = build.compile_check

        def compile_check() -> ToolResult:
            events.append("build")
            return original_compile_check()

        def reviewer_effect(state: TaskState) -> None:
            events.append("review")
            assert state.review_diff == "@@ fresh diff\n+current\n"
            state.review_approved = True
            state.review_issues.clear()

        build.compile_check = compile_check  # type: ignore[method-assign]
        stubs["reviewer"].side_effect = reviewer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            build_iterations=1,
            build_loop_key="task",
            build_loop_start_iteration=1,
            review_diff="@@ stale diff\n+old",
            review_mode="review_fix",
            review_base_branch="main",
            worktree_base=str(tmp_path),
            fixer_changed_code=False,
            review_approved=False,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert events == ["build", "review"]

    def test_fast_path_not_triggered_in_run_mode_resume(self, tmp_path: Path):
        """Run-mode resume: implementer ran in previous session (files pre-exist), reviewer
        approves in this session — build must still run (code not yet CI-validated)."""
        orch, stubs, build = _make_orchestrator(
            tmp_path,
            run_review=True,
            run_build=True,
            run_tests=True,
        )

        def reviewer_effect(state: TaskState) -> None:
            state.review_approved = True
            state.review_issues.clear()

        stubs["reviewer"].side_effect = reviewer_effect
        # No review_diff — this is a run-mode task, not a review task
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        result = orch.run(task_id="t1")

        assert result.done
        assert build.compile_calls >= 1


# ---------------------------------------------------------------------------
# Tests — build tool factory
# ---------------------------------------------------------------------------


class TestBuildToolFactory:
    def test_python_platform_creates_python_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "python"}, "build": {}})
        assert isinstance(tool, PythonTool)

    def test_default_platform_creates_gradle_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {}, "build": {}})
        assert isinstance(tool, AndroidGradleTool)

    def test_python_tool_uses_configured_commands(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "python"},
            "build": {"compile_command": "mypy .", "test_command": "pytest -v", "timeout": 120},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, PythonTool)
        assert tool._compile_command == "mypy ."
        assert tool._test_command == "pytest -v"
        assert tool._timeout == 120

    def test_cargo_platform_creates_cargo_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        (tmp_path / "Cargo.lock").write_text("# lock\n")
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "cargo"}, "build": {}})
        assert isinstance(tool, CargoTool)
        assert tool._sync_command == "cargo fetch --locked"

    def test_cargo_platform_without_lockfile_uses_unlocked_fetch(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "cargo"}, "build": {}})
        assert isinstance(tool, CargoTool)
        assert tool._sync_command == "cargo fetch"

    def test_cargo_tool_uses_configured_commands(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "cargo"},
            "build": {
                "sync_command": "cargo generate-lockfile",
                "compile_command": "cargo check --workspace",
                "test_command": "cargo test --workspace",
                "timeout": 300,
            },
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, CargoTool)
        assert tool._sync_command == "cargo generate-lockfile"
        assert tool._compile_command == "cargo check --workspace"
        assert tool._test_command == "cargo test --workspace"
        assert tool._timeout == 300

    def test_node_platform_creates_node_tool(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "node"}, "build": {}})
        assert isinstance(tool, NodeTool)

    def test_node_tool_uses_configured_commands(self, tmp_path: Path):
        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "node"},
            "build": {
                "package_manager": "pnpm",
                "sync_command": "pnpm install --offline",
                "compile_command": "pnpm typecheck",
                "test_command": "pnpm test",
                "timeout": 240,
            },
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, NodeTool)
        assert tool._package_manager == "pnpm"
        assert tool._sync_command == "pnpm install --offline"
        assert tool._compile_command == "pnpm typecheck"
        assert tool._test_command == "pnpm test"
        assert tool._compile_timeout == 240

    def test_gradle_jvm_creates_jvm_tool(self, tmp_path: Path):
        from tools.gradle_jvm_tool import JvmGradleTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "gradle-jvm"}, "build": {}})
        assert isinstance(tool, JvmGradleTool)

    def test_gradle_jvm_uses_configured_tasks(self, tmp_path: Path):
        from tools.gradle_jvm_tool import JvmGradleTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "gradle-jvm"},
            "build": {"compile_task": "compileKotlin", "test_task": "test", "compile_timeout": 300},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, JvmGradleTool)
        assert tool._compile_task == "compileKotlin"
        assert tool._compile_timeout == 300

    def test_maven_creates_maven_tool(self, tmp_path: Path):
        from tools.maven_tool import MavenTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "maven"}, "build": {}})
        assert isinstance(tool, MavenTool)

    def test_maven_uses_configured_commands(self, tmp_path: Path):
        from tools.maven_tool import MavenTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "maven"},
            "build": {"compile_command": "mvn compile -q", "test_command": "mvn test -q"},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, MavenTool)
        assert tool._compile_command == "mvn compile -q"
        assert tool._test_command == "mvn test -q"


# ---------------------------------------------------------------------------
# Tests — fix_command in check config
# ---------------------------------------------------------------------------

_CHECK_WITH_FIX = [
    {"name": "ruff-format", "command": "ruff format --check .", "fix_command": "ruff format .", "timeout": 60}
]
_CHECK_WITH_FIX_GRADLE = [
    {
        "name": "ktlint-format",
        "command": "./gradlew ktlintCheck",
        "fix_command": "./gradlew ktlintFormat",
        "timeout": 600,
    }
]
_CHECK_WITHOUT_FIX = [{"name": "ruff-check", "command": "ruff check .", "timeout": 60}]
_CHECK_WITH_FIX_NO_TIMEOUT = [
    {"name": "ruff-format", "command": "ruff format --check .", "fix_command": "ruff format ."}
]


def _make_orch_with_checks(
    tmp_path: Path, checks: list, **config_kwargs
) -> tuple[Orchestrator, dict[str, StubAgent], StubBuildTool]:
    project_config = {"project": {"build_tool": "python"}, "build": {"checks": checks}}
    config = OrchestratorConfig(
        project_root=tmp_path,
        allowed_write_paths=["."],
        allowed_read_paths=["."],
        project_config=project_config,
        run_build=True,
        run_tests=False,
        run_checks=True,
        **config_kwargs,
    )
    store = JsonStateStore(tmp_path / "state")
    orch = Orchestrator(config=config, llm=StubLLMClient(), state_store=store)
    stubs = {
        name: StubAgent(name=name)
        for name in ["analyst", "planner", "implementer", "reviewer", "security_reviewer", "test_writer", "fixer"]
    }

    def _default_reviewer(state: TaskState) -> None:
        state.review_approved = True
        state.review_issues = []

    def _default_security_reviewer(state: TaskState) -> None:
        state.security_approved = True
        state.review_issues = []

    stubs["reviewer"].side_effect = _default_reviewer
    stubs["security_reviewer"].side_effect = _default_security_reviewer

    orch._agents = stubs  # type: ignore[assignment]
    build = StubBuildTool()
    orch._tools["build"] = build
    return orch, stubs, build


class TestOrchestratorFixCommand:
    def _checks_ready(self, orch: Orchestrator) -> None:
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

    def test_autofix_called_when_check_fails(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert "ruff-format_autofix" in build.check_calls

    def test_check_rerun_after_autofix_success(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert build.check_calls.count("ruff-format") == 2

    def test_task_done_without_fixer_when_autofix_passes(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        result = orch.run(task_id="t1")
        assert result.done
        assert len(stubs["fixer"].calls) == 0

    def test_validation_records_include_autofix_attempt(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        check_records = [(r["phase"], r["status"], r.get("check_name")) for r in result.validation_cycle_records]
        assert ("check", "failed", "ruff-format") in check_records
        assert ("check_autofix", "success", "ruff-format") in check_records
        assert ("check", "success", "ruff-format") in check_records

    def test_autofix_cleans_new_untracked_artifacts_before_check_rerun(self, tmp_project: Path):
        orch, _, build = _make_orch_with_checks(tmp_project, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}

        def write_autofix_artifact() -> None:
            artifact = tmp_project / "reports" / "ruff-format.cache"
            artifact.parent.mkdir()
            artifact.write_text("generated by autofix\n")

        build.check_side_effects["ruff-format_autofix"] = write_autofix_artifact
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "reports" / "ruff-format.cache").exists()
        assert result.validation_artifact_records[0]["phase"] == "check_autofix"
        assert result.validation_artifact_records[0]["status"] == "cleaned"
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "reports/ruff-format.cache",
                "before_status": "clean",
                "after_status": "untracked",
            }
        ]

    def test_autofix_preserves_existing_source_changes(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        orch, _, build = _make_orch_with_checks(tmp_project, _CHECK_WITH_FIX)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}

        def format_source() -> None:
            source.write_text("# formatted by autofix\n")

        build.check_side_effects["ruff-format_autofix"] = format_source
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# formatted by autofix\n"
        assert result.validation_artifact_records == []

    def test_autofix_failure_falls_through_to_fixer(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX, max_iterations=2)
        build.check_results = {"ruff-format": [False], "ruff-format_autofix": [False]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["fixer"].calls) >= 1

    def test_validation_records_do_not_duplicate_check_failure_when_autofix_fails(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX, max_iterations=1)
        build.check_results = {"ruff-format": [False], "ruff-format_autofix": [False]}
        self._checks_ready(orch)

        result = orch.run(task_id="t1")

        check_failures = [
            r
            for r in result.validation_cycle_records
            if r["phase"] == "check" and r["status"] == "failed" and r.get("check_name") == "ruff-format"
        ]
        autofix_failures = [
            r
            for r in result.validation_cycle_records
            if r["phase"] == "check_autofix" and r["status"] == "failed" and r.get("check_name") == "ruff-format"
        ]
        assert len(check_failures) == 1
        assert len(autofix_failures) == 1

    def test_no_fix_command_calls_fixer_on_failure(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITHOUT_FIX, max_iterations=2)
        build.check_success = False
        self._checks_ready(orch)
        orch.run(task_id="t1")
        assert len(stubs["fixer"].calls) >= 1
        assert "ruff-check_autofix" not in build.check_calls

    def test_fix_command_timeout_not_forwarded_when_absent(self, tmp_path: Path):
        orch, _, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX_NO_TIMEOUT)
        build.check_results = {"ruff-format": [False, True], "ruff-format_autofix": [True]}
        self._checks_ready(orch)
        orch.run(task_id="t1")
        autofix_cfg = next(cfg for name, cfg in build.check_configs if name == "ruff-format_autofix")
        assert "timeout" not in autofix_cfg

    def test_fix_command_works_for_gradle_check(self, tmp_path: Path):
        orch, stubs, build = _make_orch_with_checks(tmp_path, _CHECK_WITH_FIX_GRADLE)
        build.check_results = {"ktlint-format": [False, True], "ktlint-format_autofix": [True]}
        self._checks_ready(orch)
        result = orch.run(task_id="t1")
        assert "ktlint-format_autofix" in build.check_calls
        assert result.done
        assert len(stubs["fixer"].calls) == 0


# ---------------------------------------------------------------------------
# Tests — OrchestratorConfig defaults (__post_init__ None guards)
# ---------------------------------------------------------------------------


class TestOrchestratorConfigDefaults:
    def test_none_write_paths_defaults_to_empty_list(self, tmp_path: Path):
        cfg = OrchestratorConfig(project_root=tmp_path, allowed_write_paths=None)
        assert cfg.allowed_write_paths == []

    def test_none_read_paths_defaults_to_dot(self, tmp_path: Path):
        cfg = OrchestratorConfig(project_root=tmp_path, allowed_read_paths=None)
        assert cfg.allowed_read_paths == ["."]

    def test_none_project_config_defaults_to_empty_dict(self, tmp_path: Path):
        cfg = OrchestratorConfig(project_root=tmp_path, project_config=None)
        assert cfg.project_config == {}


# ---------------------------------------------------------------------------
# Tests — run() error paths
# ---------------------------------------------------------------------------


class TestOrchestratorRunErrors:
    def test_task_not_found_raises_value_error(self, tmp_path: Path):
        import pytest

        orch, _, _ = _make_orchestrator(tmp_path)
        with pytest.raises(ValueError, match="Task not found"):
            orch.run(task_id="nonexistent")

    def test_no_args_raises_value_error(self, tmp_path: Path):
        import pytest

        orch, _, _ = _make_orchestrator(tmp_path)
        with pytest.raises(ValueError):
            orch.run()


# ---------------------------------------------------------------------------
# Tests — presync phase
# ---------------------------------------------------------------------------


class TestOrchestratorPresync:
    def test_presync_success_sets_presync_done(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        _save_state(orch)
        orch.run(task_id="t1")
        assert orch._store.load("t1").presync_done is True

    def test_presync_failure_is_non_fatal_analyst_still_runs(self, tmp_path: Path):
        orch, stubs, build = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        build.presync_success = False
        _save_state(orch)
        orch.run(task_id="t1")
        assert len(stubs["analyst"].calls) >= 1

    def test_presync_failure_still_sets_presync_done(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        build.presync_success = False
        _save_state(orch)
        orch.run(task_id="t1")
        assert orch._store.load("t1").presync_done is True

    def test_presync_skipped_on_resume(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_presync=True, run_build=False)
        _save_state(orch, presync_done=True)
        orch.run(task_id="t1")
        assert build.presync_calls == 0


# ---------------------------------------------------------------------------
# Tests — planner failure abort
# ---------------------------------------------------------------------------


class TestOrchestratorPlannerAbort:
    def test_delivery_child_stops_before_implementation_when_planner_budget_is_exceeded(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=False, run_build=False)

        def planner_effect(state: TaskState) -> None:
            state.plan = ["Step 1", "Step 2", "Step 3"]
            state.plan_decided = True
            state.record("planner", "plan", "3 steps")

        stubs["planner"].side_effect = planner_effect
        _save_state(
            orch,
            implementation_prompt="p",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 1},
        )

        result = orch.run(task_id="t1")
        saved = orch._store.load("t1")

        assert result.failed is True
        assert result.plan == ["Step 1", "Step 2", "Step 3"]
        assert result.delivery_budget_stop is not None
        assert result.delivery_budget_stop["code"] == "unit_budget_exceeded"
        assert result.delivery_budget_stop["name"] == "max_planner_steps"
        assert result.delivery_budget_stop["limit"] == 1
        assert result.delivery_budget_stop["actual"] == 3
        assert result.delivery_budget_stop["phase"] == "planner"
        assert result.delivery_stop_code is None
        assert len(stubs["planner"].calls) == 1
        assert len(stubs["implementer"].calls) == 0
        assert saved is not None
        assert saved.delivery_budget_stop == result.delivery_budget_stop
        assert any(entry["action"] == "delivery_budget_exceeded" for entry in saved.history)

    def test_delivery_child_allows_explicit_two_step_budget(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=False, run_build=False)

        def planner_effect(state: TaskState) -> None:
            state.plan = ["Step 1", "Step 2"]
            state.plan_decided = True

        def implementer_effect(state: TaskState) -> None:
            state.files_changed.append(f"src/step{len(stubs['implementer'].calls)}.py")

        stubs["planner"].side_effect = planner_effect
        stubs["implementer"].side_effect = implementer_effect
        _save_state(
            orch,
            implementation_prompt="p",
            delivery_plan_id="plan-1",
            delivery_unit_id="unit-1",
            delivery_unit_budget={"max_planner_steps": 2},
        )

        result = orch.run(task_id="t1")

        assert result.done is True
        assert result.delivery_budget_stop is None
        assert len(stubs["planner"].calls) == 1
        assert len(stubs["implementer"].calls) == 2
        assert result.step_file_tracking_enabled is True
        assert result.step_files_changed == ["src/step2.py"]

    def test_planner_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)
        stubs["planner"].side_effect = lambda s: setattr(s, "failed", True)
        _save_state(orch, implementation_prompt="p")
        result = orch.run(task_id="t1")
        assert result.failed
        assert len(stubs["implementer"].calls) == 0

    def test_planner_result_failure_aborts_task(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_planner=True, run_build=False)

        class FailingPlanner:
            def __init__(self) -> None:
                self.calls: list[TaskState] = []

            def run(self, state: TaskState) -> AgentResult:
                self.calls.append(state)
                state.record("planner", "plan_failed", "temporary provider failure")
                return AgentResult(success=False, message="temporary provider failure")

        failing_planner = FailingPlanner()
        orch._agents["planner"] = failing_planner  # type: ignore[assignment]
        _save_state(orch, implementation_prompt="p")

        result = orch.run(task_id="t1")

        assert result.failed
        assert result.plan_decided is False
        assert result.plan == []
        assert len(failing_planner.calls) == 1
        assert len(stubs["implementer"].calls) == 0


# ---------------------------------------------------------------------------
# Tests — _run_agent exception handling
# ---------------------------------------------------------------------------


class TestOrchestratorAgentException:
    def test_agent_exception_sets_failed(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        stubs["analyst"].raise_exception = RuntimeError("unexpected crash")
        _save_state(orch)
        result = orch.run(task_id="t1")
        assert result.failed

    def test_agent_exception_recorded_in_history(self, tmp_path: Path):
        orch, stubs, _ = _make_orchestrator(tmp_path, run_build=False)
        stubs["analyst"].raise_exception = RuntimeError("crash")
        _save_state(orch)
        result = orch.run(task_id="t1")
        assert any(h.get("action") == "error" for h in result.history)


# ---------------------------------------------------------------------------
# Tests — _run_tests and _sync failure paths
# ---------------------------------------------------------------------------


class TestOrchestratorTestsAndSyncFailure:
    def test_test_failure_appends_to_test_errors(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=True, max_iterations=1)
        build.test_success = False
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        result = orch.run(task_id="t1")
        assert result.test_errors
        assert result.test_status == "failed"

    def test_test_failure_preserves_middle_failure_diagnostics(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, run_tests=True, max_iterations=1)
        failure_output = (
            "Compiling workspace\n"
            + "".join(f"build line {i}\n" for i in range(160))
            + "thread 'test_rejects_wrong_result_type' panicked at assertion failed\n"
            + "left: Ok(ParsedConfig)\n"
            + "right: Err(ValidationError)\n"
            + "failures:\n"
            + "    test_rejects_wrong_result_type\n"
            + "".join(f"Running unrelated test binary {i}\n" for i in range(260))
            + "error: test failed, to rerun pass `-p example_crate --test validation_tests`\n"
        )

        def failing_tests() -> ToolResult:
            build.test_calls += 1
            return ToolResult(success=False, output=failure_output, error=failure_output)

        build.run_tests = failing_tests
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert "test_rejects_wrong_result_type" in result.test_errors[-1]
        test_records = [r for r in result.validation_cycle_records if r["phase"] == "test"]
        assert "test_rejects_wrong_result_type" in test_records[-1]["error_excerpt"]

    def test_sync_failure_appends_to_errors(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=1)
        build.sync_success = False
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        result = orch.run(task_id="t1")
        assert result.errors

    def test_sync_records_tool_metadata_for_audit(self, tmp_path: Path):
        orch, _, build = _make_orchestrator(tmp_path, run_build=True, max_iterations=1)
        build.sync_metadata = {
            "sync_retry": {
                "reason": "cargo_lockfile_needs_update",
                "initial_command": "cargo fetch --locked",
                "retry_command": "cargo fetch",
                "retry_status": "success",
            }
        }
        _save_state(orch, implementation_prompt="p", files_changed=["Cargo.toml"], build_synced=False)

        result = orch.run(task_id="t1")

        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["status"] == "success"
        assert sync_records[-1]["metadata"]["sync_retry"]["reason"] == "cargo_lockfile_needs_update"
        assert sync_records[-1]["metadata"]["sync_retry"]["initial_command"] == "cargo fetch --locked"
        assert sync_records[-1]["metadata"]["sync_retry"]["retry_command"] == "cargo fetch"

    def test_sync_adopts_known_outputs_and_reruns_semantic_gates(self, tmp_project: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
        )
        output = tmp_project / "generated.lock"
        output.write_text("before sync\n")
        subprocess.run(["git", "add", "generated.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add generated lock"], cwd=tmp_project, check=True, capture_output=True)
        build.sync_adoptable_files = {"generated.lock"}
        build.sync_side_effect = lambda: output.write_text("resolved during sync\n")
        stubs["test_writer"].side_effect = lambda state: setattr(state, "tests_up_to_date", True)
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=False,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert result.files_changed == ["src/main.py", "generated.lock"]
        assert result.review_approved
        assert result.security_approved
        assert result.tests_up_to_date
        assert len(stubs["reviewer"].calls) == 1
        assert len(stubs["security_reviewer"].calls) == 1
        assert len(stubs["test_writer"].calls) == 1
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"] == [
            {"path": "generated.lock", "before_status": "clean", "after_status": "tracked"}
        ]

    def test_sync_cleans_brand_new_builtin_lockfile_without_config(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "Cargo.lock"
        build.sync_adoptable_files = {"Cargo.lock"}
        build.sync_side_effect = lambda: output.write_text("brand-new lockfile from default sync\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert not output.exists()
        assert "Cargo.lock" not in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["cleaned"] == [
            {"path": "Cargo.lock", "before_status": "clean", "after_status": "untracked"}
        ]

    def test_sync_adopts_brand_new_builtin_lockfile_when_configured(self, tmp_project: Path):
        config = {
            "project": {"build_tool": "python"},
            "build": {"sync_adopt_paths": ["Cargo.lock"]},
        }
        orch, _, build = _make_orchestrator(
            tmp_project,
            project_config=config,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "Cargo.lock"
        build.sync_adoptable_files = {"Cargo.lock"}
        build.sync_side_effect = lambda: output.write_text("brand-new lockfile explicitly opted in\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert output.exists()
        assert "Cargo.lock" in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"] == [
            {"path": "Cargo.lock", "before_status": "clean", "after_status": "untracked"}
        ]

    def test_sync_adopts_outputs_from_configured_patterns(self, tmp_project: Path):
        config = {
            "project": {"build_tool": "python"},
            "build": {"sync_adopt_paths": ["generated/api/"]},
        }
        orch, stubs, build = _make_orchestrator(
            tmp_project,
            project_config=config,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=True,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "generated" / "api" / "schema.json"
        build.sync_side_effect = lambda: (output.parent.mkdir(parents=True, exist_ok=True), output.write_text("{}\n"))
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=False,
            review_approved=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert "generated/api/schema.json" in result.files_changed
        assert len(stubs["reviewer"].calls) == 1
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"][0]["path"] == "generated/api/schema.json"

    def test_sync_adopts_outputs_from_configured_string_glob(self, tmp_project: Path):
        config = {
            "project": {"build_tool": "python"},
            "build": {"sync_adopt_paths": "generated/**/*.json"},
        }
        orch, _, build = _make_orchestrator(
            tmp_project,
            project_config=config,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        output = tmp_project / "generated" / "api" / "schema.json"
        build.sync_side_effect = lambda: (output.parent.mkdir(parents=True, exist_ok=True), output.write_text("{}\n"))
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert "generated/api/schema.json" in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"][0]["path"] == "generated/api/schema.json"

    def test_sync_adopts_project_relative_paths_when_git_root_differs(self, tmp_project: Path):
        project = tmp_project / "app"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("# app\n")
        output = project / "Cargo.lock"
        output.write_text("before sync\n")
        subprocess.run(
            ["git", "add", "app/src/main.py", "app/Cargo.lock"], cwd=tmp_project, check=True, capture_output=True
        )
        subprocess.run(["git", "commit", "-m", "add app"], cwd=tmp_project, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"Cargo.lock"}
        build.sync_side_effect = lambda: output.write_text("resolved during sync\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert "Cargo.lock" in result.files_changed
        assert "app/Cargo.lock" not in result.files_changed
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["adopted"][0]["path"] == "Cargo.lock"

    def test_sync_blocks_adoptable_outputs_outside_project_root(self, tmp_project: Path):
        project = tmp_project / "app"
        project.mkdir()
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"Cargo.lock"}
        output = tmp_project / "Cargo.lock"
        output.write_text("before sync\n")
        subprocess.run(["git", "add", "Cargo.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add root lock"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        before = orch._validation_artifact_snapshot(state)
        output.write_text("resolved outside project root\n")

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert output.read_text() == "before sync\n"
        assert "outside project root" in (error or "")
        assert "Cargo.lock" not in state.files_changed
        assert metadata["outside_project"][0]["path"] == "Cargo.lock"
        assert metadata["cleaned"][0]["path"] == "Cargo.lock"
        assert "cleanup_failed" not in metadata
        assert state.validation_artifact_records[0]["status"] == "blocked"

    def test_sync_blocks_outside_project_outputs_after_adopting_inside_outputs(self, tmp_project: Path):
        project = tmp_project / "app"
        project.mkdir()
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"Cargo.lock", "generated.lock"}
        inside_output = project / "generated.lock"
        inside_output.write_text("before sync\n")
        output = tmp_project / "Cargo.lock"
        output.write_text("before sync\n")
        subprocess.run(
            ["git", "add", "app/generated.lock", "Cargo.lock"], cwd=tmp_project, check=True, capture_output=True
        )
        subprocess.run(["git", "commit", "-m", "add sync outputs"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        before = orch._validation_artifact_snapshot(state)
        inside_output.write_text("resolved inside project root\n")
        output.write_text("resolved outside project root\n")

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert "outside project root" in (error or "")
        assert output.read_text() == "before sync\n"
        assert "generated.lock" in state.files_changed
        assert metadata["adopted"][0]["path"] == "generated.lock"
        assert metadata["outside_project"][0]["path"] == "Cargo.lock"
        assert metadata["cleaned"][0]["path"] == "Cargo.lock"

    def test_sync_cleans_unexpected_artifacts(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        artifact = tmp_project / "tmp-sync.log"
        build.sync_side_effect = lambda: artifact.write_text("cache\n")
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)

        result = orch.run(task_id="t1")

        assert result.done
        assert not artifact.exists()
        assert "tmp-sync.log" not in result.files_changed
        assert result.validation_artifact_records[0]["phase"] == "sync"
        assert result.validation_artifact_records[0]["status"] == "cleaned"
        sync_records = [record for record in result.validation_cycle_records if record["phase"] == "sync"]
        assert sync_records[-1]["metadata"]["sync_outputs"]["cleaned"][0]["path"] == "tmp-sync.log"

    def test_sync_adopts_known_outputs_before_unexpected_cleanup_failure(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=True,
            run_security_review=True,
            run_test_writing=True,
        )
        build.sync_adoptable_files = {"generated.lock"}
        tracked_output = tmp_project / "generated.lock"
        tracked_output.write_text("before sync\n")
        subprocess.run(["git", "add", "generated.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add generated lock"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=False,
            review_approved=True,
            security_approved=True,
            tests_up_to_date=True,
        )
        before = orch._validation_artifact_snapshot(state)
        tracked_output.write_text("resolved during sync\n")
        (tmp_project / "tmp-sync.log").write_text("cache\n")
        monkeypatch.setattr(
            orchestrator_module,
            "restore_validation_artifacts",
            lambda *_args: ["tmp-sync.log: permission denied"],
        )

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert "permission denied" in (error or "")
        assert "generated.lock" in state.files_changed
        assert not state.review_approved
        assert not state.security_approved
        assert not state.tests_up_to_date
        assert metadata["adopted"] == [{"path": "generated.lock", "before_status": "clean", "after_status": "tracked"}]
        assert metadata["cleanup_failed"] == [
            {"path": "tmp-sync.log", "before_status": "clean", "after_status": "untracked"}
        ]
        assert "adoptable" not in metadata
        assert state.validation_artifact_records[0]["status"] == "cleanup_failed"

    def test_sync_adopts_known_outputs_before_cleanup_pass_limit(
        self, tmp_project: Path, monkeypatch: pytest.MonkeyPatch
    ):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        build.sync_adoptable_files = {"generated.lock"}
        tracked_output = tmp_project / "generated.lock"
        tracked_output.write_text("before sync\n")
        subprocess.run(["git", "add", "generated.lock"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add generated lock"], cwd=tmp_project, check=True, capture_output=True)
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=False)
        before = orch._validation_artifact_snapshot(state)
        tracked_output.write_text("resolved during sync\n")
        (tmp_project / "tmp-sync.log").write_text("cache\n")
        monkeypatch.setattr(orchestrator_module, "_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES", 0)

        ok, metadata, error = orch._record_sync_artifact_changes(
            state,
            before=before,
            adopt_known_outputs=True,
        )

        assert not ok
        assert "after 0 cleanup passes" in (error or "")
        assert "generated.lock" in state.files_changed
        assert metadata["adopted"][0]["path"] == "generated.lock"
        assert metadata["cleanup_failed"][0]["path"] == "tmp-sync.log"
        assert "adoptable" not in metadata
        assert state.validation_artifact_records[0]["status"] == "cleanup_failed"


class TestValidationArtifacts:
    @staticmethod
    def _non_executable_mode(path: Path) -> int:
        path.chmod(0o644)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & stat.S_IXUSR:
            mode &= ~stat.S_IXUSR
            path.chmod(mode)
            mode = stat.S_IMODE(path.stat().st_mode)
        return mode

    @staticmethod
    def _executable_mode(path: Path, before_mode: int) -> int:
        mode = before_mode | stat.S_IXUSR
        if mode == before_mode:
            pytest.skip("filesystem does not preserve executable bit changes")
        path.chmod(mode)
        changed_mode = stat.S_IMODE(path.stat().st_mode)
        if changed_mode != mode:
            pytest.skip("filesystem does not preserve executable bit changes")
        return changed_mode

    def test_successful_build_cleans_untracked_generated_artifacts(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_build_artifact() -> None:
            artifact = tmp_project / "generated" / "compile-artifact.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("generated by compile validation\n")

        build.compile_side_effect = write_build_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "generated" / "compile-artifact.txt").exists()
        assert result.validation_artifact_records[0]["phase"] == "build"
        assert result.validation_artifact_records[0]["status"] == "cleaned"

    def test_successful_tests_clean_untracked_generated_artifacts(self, tmp_project: Path):
        orch, stubs, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_runtime_artifact() -> None:
            runtime = tmp_project / "tests" / "client-main.runtime-1.ts"
            runtime.parent.mkdir(parents=True, exist_ok=True)
            runtime.write_text('import "file:///Users/example/project/src/client/main.ts";\n')

        build.test_side_effect = write_runtime_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "tests" / "client-main.runtime-1.ts").exists()
        assert len(stubs["fixer"].calls) == 0
        assert result.validation_artifact_records == [
            {
                "phase": "test",
                "status": "cleaned",
                "build_iteration": 1,
                "step": 0,
                "timestamp": result.validation_artifact_records[0]["timestamp"],
                "artifacts": [
                    {
                        "path": "tests/client-main.runtime-1.ts",
                        "before_status": "clean",
                        "after_status": "untracked",
                    }
                ],
            }
        ]
        assert any(
            record["phase"] == "validation_artifact" and record["status"] == "cleaned"
            for record in result.validation_cycle_records
        )

    def test_successful_tests_restore_existing_dirty_file_contents(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.write_text("# task change\n")
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def mutate_task_file() -> None:
            source.write_text("# task change\n# generated during test\n")

        build.test_side_effect = mutate_task_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# task change\n"
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_restore_existing_dirty_file_mode_only_change(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.write_text("# task change\n")
        before_mode = self._non_executable_mode(source)
        after_mode = self._executable_mode(source, before_mode)
        source.chmod(before_mode)
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def chmod_task_file() -> None:
            source.chmod(after_mode)

        build.test_side_effect = chmod_task_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# task change\n"
        assert stat.S_IMODE(source.stat().st_mode) == before_mode
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_restore_existing_dirty_file_content_and_mode(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.write_text("# task change\n")
        before_mode = self._non_executable_mode(source)
        after_mode = self._executable_mode(source, before_mode)
        source.chmod(before_mode)
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def mutate_task_file() -> None:
            source.write_text("# task change\n# generated during test\n")
            source.chmod(after_mode)

        build.test_side_effect = mutate_task_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# task change\n"
        assert stat.S_IMODE(source.stat().st_mode) == before_mode
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_restore_clean_tracked_file_from_head(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        assert source.read_text() == "# placeholder\n"
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def mutate_clean_tracked_file() -> None:
            source.write_text("# generated during test\n")

        build.test_side_effect = mutate_clean_tracked_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/feature.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert source.read_text() == "# placeholder\n"
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "clean",
                "after_status": "tracked",
            }
        ]

    def test_successful_tests_preserve_task_deleted_tracked_file(self, tmp_project: Path):
        source = tmp_project / "src" / "main.py"
        source.unlink()
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def recreate_deleted_file() -> None:
            source.write_text("# generated during test\n")

        build.test_side_effect = recreate_deleted_file
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not source.exists()
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "src/main.py",
                "before_status": "tracked",
                "after_status": "tracked",
            }
        ]

    def test_successful_checks_clean_untracked_generated_artifacts(self, tmp_project: Path):
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=False,
            run_checks=True,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
            project_config={
                "project": {"build_tool": "python"},
                "build": {"checks": [{"name": "typecheck", "command": "python -m py_compile src/main.py"}]},
            },
        )

        def write_check_artifact() -> None:
            artifact = tmp_project / "reports" / "typecheck.runtime.txt"
            artifact.parent.mkdir(parents=True, exist_ok=True)
            artifact.write_text("generated by check validation\n")

        build.check_side_effects["typecheck"] = write_check_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert not (tmp_project / "reports" / "typecheck.runtime.txt").exists()
        assert result.validation_artifact_records[0]["phase"] == "check"
        assert result.validation_artifact_records[0]["check_name"] == "typecheck"
        assert result.validation_artifact_records[0]["status"] == "cleaned"

    def test_gitignored_validation_outputs_are_not_artifacts(self, tmp_project: Path):
        (tmp_project / ".gitignore").write_text("coverage/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ignore coverage"], cwd=tmp_project, check=True, capture_output=True)

        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_ignored_coverage() -> None:
            coverage = tmp_project / "coverage" / "index.html"
            coverage.parent.mkdir(parents=True, exist_ok=True)
            coverage.write_text("<html></html>\n")

        build.test_side_effect = write_ignored_coverage
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert (tmp_project / "coverage" / "index.html").exists()
        assert result.validation_artifact_records == []

    def test_isolated_subproject_validation_cleans_repo_root_artifacts(self, tmp_path: Path):
        repo = tmp_path
        project = repo / "apps" / "demo"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("# placeholder\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_repo_root_artifact() -> None:
            (repo / "coverage.json").write_text("{}\n")

        build.test_side_effect = write_repo_root_artifact
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
            worktree_path=str(project),
            worktree_base=str(repo),
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert not (repo / "coverage.json").exists()
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "coverage.json",
                "before_status": "clean",
                "after_status": "untracked",
            }
        ]

    def test_no_isolate_subproject_validation_cleans_repo_root_artifacts(self, tmp_path: Path):
        repo = tmp_path
        project = repo / "apps" / "demo"
        (project / "src").mkdir(parents=True)
        (project / "src" / "main.py").write_text("# placeholder\n")
        subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_repo_root_artifact() -> None:
            (repo / "coverage.json").write_text("{}\n")

        build.test_side_effect = write_repo_root_artifact
        _save_state(
            orch,
            implementation_prompt="p",
            files_changed=["src/main.py"],
            build_synced=True,
        )

        result = orch.run(task_id="t1")

        assert result.done
        assert not (repo / "coverage.json").exists()
        assert result.validation_artifact_records[0]["artifacts"] == [
            {
                "path": "coverage.json",
                "before_status": "clean",
                "after_status": "untracked",
            }
        ]

    def test_validation_artifact_root_falls_back_to_project_root_outside_git(self, tmp_path: Path):
        orch, _, _ = _make_orchestrator(
            tmp_path,
            run_build=True,
            run_tests=False,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        state = TaskState(task_id="t1", task_description="test task")

        assert orch._validation_artifact_root(state) == tmp_path.resolve()

    def test_validation_artifact_cleanup_rescans_after_restoring_gitignore(self, tmp_project: Path):
        (tmp_project / ".gitignore").write_text("coverage/\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=tmp_project, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "ignore coverage"], cwd=tmp_project, check=True, capture_output=True)
        orch, _, build = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )

        def write_hidden_artifact() -> None:
            (tmp_project / ".gitignore").write_text("coverage/\ngenerated/\n")
            artifact = tmp_project / "generated" / "test-runtime.cache"
            artifact.parent.mkdir()
            artifact.write_text("generated while hidden by mutated ignore rules\n")

        build.test_side_effect = write_hidden_artifact
        _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)

        result = orch.run(task_id="t1")

        assert result.done
        assert (tmp_project / ".gitignore").read_text() == "coverage/\n"
        assert not (tmp_project / "generated" / "test-runtime.cache").exists()
        assert [record["artifacts"] for record in result.validation_artifact_records] == [
            [
                {
                    "path": ".gitignore",
                    "before_status": "clean",
                    "after_status": "tracked",
                }
            ],
            [
                {
                    "path": "generated/test-runtime.cache",
                    "before_status": "clean",
                    "after_status": "untracked",
                }
            ],
        ]

    def test_validation_artifact_cleanup_fails_after_repeated_rescan_artifacts(self, tmp_project: Path, monkeypatch):
        orch, _, _ = _make_orchestrator(
            tmp_project,
            run_build=True,
            run_tests=True,
            run_checks=False,
            run_review=False,
            run_security_review=False,
            run_test_writing=False,
        )
        state = _save_state(orch, implementation_prompt="p", files_changed=["src/main.py"], build_synced=True)
        before = orch._validation_artifact_snapshot(state)
        artifact = tmp_project / "reports" / "stuck.cache"
        artifact.parent.mkdir(parents=True)
        artifact.write_text("keeps reappearing\n")
        monkeypatch.setattr(orchestrator_module, "_VALIDATION_ARTIFACT_CLEANUP_MAX_PASSES", 2)
        monkeypatch.setattr(orchestrator_module, "restore_validation_artifacts", lambda *_args: [])

        ok = orch._record_validation_artifacts(
            state,
            phase="check",
            before=before,
            check_name="typecheck",
        )

        assert not ok
        assert state.check_status == "failed"
        assert (
            "check/typecheck command produced additional repository artifact(s) after 2 cleanup passes"
            in (state.check_errors[-1])
        )
        assert [record["status"] for record in state.validation_artifact_records] == ["cleaned", "cleaned"]
        assert [record["check_name"] for record in state.validation_artifact_records] == ["typecheck", "typecheck"]


# ---------------------------------------------------------------------------
# Tests — _build_tool xcodebuild factory
# ---------------------------------------------------------------------------


class TestBuildToolFactoryXcode:
    def test_xcodebuild_creates_xcode_tool(self, tmp_path: Path):
        from tools.xcode_tool import XcodeTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        tool = _build_tool(sandbox, tmp_path, {"project": {"build_tool": "xcodebuild"}, "build": {}})
        assert isinstance(tool, XcodeTool)

    def test_xcodebuild_uses_configured_scheme(self, tmp_path: Path):
        from tools.xcode_tool import XcodeTool

        sandbox = Sandbox(project_root=tmp_path, allowed_write_paths=["."], allowed_read_paths=["."])
        config = {
            "project": {"build_tool": "xcodebuild"},
            "build": {"scheme": "MyApp", "destination": "generic/platform=iOS"},
        }
        tool = _build_tool(sandbox, tmp_path, config)
        assert isinstance(tool, XcodeTool)
        assert tool._scheme == "MyApp"


# ---------------------------------------------------------------------------
# Tests — _fmt_elapsed
# ---------------------------------------------------------------------------


class TestFmtElapsed:
    def test_seconds_under_60(self):
        assert _fmt_elapsed(45.7) == "46s"

    def test_zero_seconds(self):
        assert _fmt_elapsed(0.0) == "0s"

    def test_exactly_60_seconds(self):
        assert _fmt_elapsed(60.0) == "1m 00s"

    def test_minutes_format(self):
        assert _fmt_elapsed(125.0) == "2m 05s"
