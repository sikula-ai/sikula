from __future__ import annotations

import argparse
from collections.abc import Callable
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import stat
import subprocess
from types import SimpleNamespace

import yaml

import pytest
import sikula as sikula_module

import core.delivery_verify as delivery_verify_module
from agents.delivery_integration_review_agent import (
    DeliveryIntegrationReviewAgent,
    DeliveryIntegrationReviewAgentError,
)
from core.llm_client import LLMConfigurationError
from core.delivery_progress import (
    DeliveryProgress,
    DeliveryProgressEvent,
    delivery_events_path,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    mark_delivery_assembly,
    mark_delivery_finalized,
    mark_delivery_verification,
    read_delivery_progress,
    upsert_delivery_unit_progress,
    write_delivery_progress,
)
from core.delivery_verification_model import (
    DeliveryVerificationRecord,
    parse_delivery_verification_record,
)
from core.delivery_verification import (
    MAX_DELIVERY_VERIFICATION_PACKET_BYTES,
    MAX_DELIVERY_VERIFICATION_SOURCE_BYTES,
    _git_object,
    build_delivery_verification_identity,
    check_delivery_verification_readiness,
    delivery_verification_config_fingerprint,
    with_delivery_verification_readiness,
)
from core.delivery_verification_review import (
    DeliveryIntegrationReviewParseError,
    parse_delivery_integration_review,
)
from core.delivery_verification_validation import (
    DeliveryVerificationValidationResult,
    reusable_delivery_validation,
    run_delivery_verification_validation,
)
from core.delivery_verify import (
    DeliveryVerifyResult,
    _append_audit,
    _candidate_config_matches_capture,
    _copy_environment_files,
    _remove_copied_environment_files,
    _safe_append_audit,
    verify_delivery_plan,
)
from core.delivery_finalize import finalize_delivery_plan, preview_delivery_finalize
from core.state import JsonStateStore
from core.worktree import delivery_verification_git_env, detached_delivery_verification_worktree
from sikula import _run_delivery_verification
from tools.base_tool import ToolResult
from sikula_cli.delivery import cmd_delivery_check, cmd_delivery_status, cmd_delivery_verify, register_parser


_COMMIT = "a" * 40
_TREE = "b" * 40
_SHA = "sha256:" + "c" * 64


class _ReadonlyLLM:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.calls: list[tuple[str, Path]] = []

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.calls.append((prompt, cwd))
        return self.outputs.pop(0)


class _SideEffectReadonlyLLM:
    def __init__(self, output: str, side_effect: Callable[[], None]) -> None:
        self.output = output
        self.side_effect = side_effect
        self.calls: list[tuple[str, Path]] = []

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.calls.append((prompt, cwd))
        self.side_effect()
        return self.output


class _FailingReadonlyLLM:
    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        raise RuntimeError("provider failed")


class _WorkspaceSetupFailingReadonlyLLM(_ReadonlyLLM):
    def prepare_readonly_agent_workspace(self, cwd: Path) -> None:
        raise LLMConfigurationError("tracked provider settings conflict")


class _BuildTool:
    def __init__(self, *, mutate: Path | None = None) -> None:
        self.calls: list[str] = []
        self.mutate = mutate

    def generate_sources(self) -> ToolResult:
        self.calls.append("presync")
        return ToolResult(True, "")

    def sync(self) -> ToolResult:
        self.calls.append("sync")
        return ToolResult(True, "")

    def compile_check(self) -> ToolResult:
        self.calls.append("build")
        if self.mutate:
            self.mutate.write_text("changed\n", encoding="utf-8")
        return ToolResult(True, "")

    def run_tests(self) -> ToolResult:
        self.calls.append("test")
        return ToolResult(True, "")

    def run_check(self, name: str, config: dict) -> ToolResult:
        self.calls.append(f"check:{name}")
        return ToolResult(True, "")


def _git_init(root: Path) -> str:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
    (root / "README.md").write_text("# Demo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _git_commit_all(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=root, check=True, capture_output=True)
    return subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _write_plan(root: Path, *, schema_version: int = 2, risk_tags: list[str] | None = None) -> Path:
    unit_path = root / ".sikula" / "delivery" / "demo" / "units" / "unit.md"
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text("# Unit\n", encoding="utf-8")
    source_text = "# Source\n"
    source_path = root / ".sikula" / "tasks" / "source.md"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(source_text, encoding="utf-8")
    data = {
        "schema_version": schema_version,
        "plan_id": "demo",
        "title": "Demo",
        "final_branch": "sikula/delivery/demo",
        "repositories": [{"id": "main", "root": "."}],
        "units": [
            {
                "id": "unit",
                "task_path": unit_path.relative_to(root).as_posix(),
                "depends_on": [],
                "risk_tags": risk_tags or [],
            }
        ],
    }
    if schema_version == 2:
        data["source_task"] = {
            "path": source_path.relative_to(root).as_posix(),
            "sha256": "sha256:" + sha256(source_text.encode("utf-8")).hexdigest(),
        }
        data["verification"] = {"mode": "final_gate"}
    plan_path = root / ".sikula" / "delivery" / "demo" / "plan.yaml"
    plan_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return plan_path


def _config(root: Path) -> dict:
    return {
        "project": {"root_path": str(root), "build_tool": "python"},
        "llm": {"provider": "codex", "model": "test"},
        "build": {"compile_command": "python -m compileall .", "test_command": "pytest"},
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
    }


def _record(**overrides) -> DeliveryVerificationRecord:
    values = {
        "schema_version": 1,
        "gate_id": _SHA,
        "candidate_commit": _COMMIT,
        "candidate_tree": _TREE,
        "source_fingerprint": _SHA,
        "plan_fingerprint": _SHA,
        "completed_scope_fingerprint": _SHA,
        "config_fingerprint": _SHA,
        "policy_fingerprint": _SHA,
        "status": "running",
        "attempt": 1,
    }
    values.update(overrides)
    return DeliveryVerificationRecord(**values)


def _assembled_progress() -> DeliveryProgress:
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="demo",
        units=[make_delivery_unit_progress("unit", "done", commit="d" * 40)],
    )
    return mark_delivery_assembly(
        progress,
        base_commit="e" * 40,
        assembled_commit=_COMMIT,
        status="ready",
    )


def test_delivery_verification_git_env_removes_repository_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redirected = (
        "GIT_ALTERNATE_OBJECT_DIRECTORIES",
        "GIT_COMMON_DIR",
        "GIT_DIR",
        "GIT_INDEX_FILE",
        "GIT_OBJECT_DIRECTORY",
        "GIT_WORK_TREE",
    )
    for key in redirected:
        monkeypatch.setenv(key, f"redirected-{key.lower()}")
    monkeypatch.setenv("SIKULA_ENV_SENTINEL", "preserved")

    env = delivery_verification_git_env()

    assert all(key not in env for key in redirected)
    assert env["GIT_NO_REPLACE_OBJECTS"] == "1"
    assert env["SIKULA_ENV_SENTINEL"] == "preserved"


def test_delivery_verification_record_round_trip() -> None:
    record = _record(
        status="passed",
        semantic_status="approved",
        security_required=True,
        security_status="approved",
        validation_reused=True,
        finding_count=2,
        evidence_path=".sikula/state/delivery/demo/verification/audit.jsonl",
        completed_at="2026-09-02T12:00:00+00:00",
    )

    assert parse_delivery_verification_record(record.to_dict()) == record
    assert record.passed is True


@pytest.mark.parametrize(
    "mutation",
    [
        {"schema_version": 2},
        {"schema_version": True},
        {"schema_version": 1.0},
        {"gate_id": "not-a-hash"},
        {"candidate_commit": "abc"},
        {"status": "unknown"},
        {"semantic_status": []},
        {"security_status": {}},
        {"attempt": 0},
        {"security_required": "yes"},
        {"evidence_path": "../private/audit.jsonl"},
        {"unexpected": True},
    ],
)
def test_delivery_verification_record_rejects_malformed_state(mutation: dict) -> None:
    payload = _record().to_dict()
    payload.update(mutation)

    with pytest.raises(ValueError):
        parse_delivery_verification_record(payload)


def test_mark_delivery_verification_requires_current_candidate() -> None:
    progress = _assembled_progress()

    with pytest.raises(ValueError, match="current assembled candidate"):
        mark_delivery_verification(progress, replace(_record(), candidate_commit="f" * 40))


def test_unit_progress_change_invalidates_verification_and_finalization() -> None:
    progress = mark_delivery_verification(
        _assembled_progress(),
        _record(status="passed", semantic_status="approved"),
    )
    progress = mark_delivery_finalized(progress, final_branch="sikula/delivery/demo", final_commit=_COMMIT)

    updated = upsert_delivery_unit_progress(
        progress,
        make_delivery_unit_progress("unit", "failed", failure_code="implementation_failed"),
    )

    assert updated.verification is None
    assert updated.final_commit is None
    assert updated.final_branch is None


def test_assembly_change_invalidates_verification() -> None:
    progress = mark_delivery_verification(_assembled_progress(), _record())

    updated = mark_delivery_assembly(
        progress,
        base_commit="e" * 40,
        assembled_commit="f" * 40,
        status="ready",
    )

    assert updated.verification is None


def test_passed_security_sensitive_record_requires_security_approval() -> None:
    progress = _assembled_progress()
    record = _record(
        status="passed",
        semantic_status="approved",
        security_required=True,
        security_status="not_run",
    )

    with pytest.raises(ValueError, match="security approval"):
        mark_delivery_verification(progress, record)


def test_verification_readiness_preserves_legacy_plan_behavior(tmp_path: Path) -> None:
    _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path, schema_version=1))

    readiness = check_delivery_verification_readiness(status, _config(tmp_path))

    assert readiness.required is False
    assert readiness.ready is True


def test_legacy_delivery_verify_does_not_require_provider_or_progress(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, schema_version=1)
    config = {**_config(tmp_path), "llm": {"provider": "unsupported"}}

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=None,
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is True
    assert result.status == "not_required"
    assert result.next_action == "finalize_delivery"


def test_verification_readiness_accepts_bounded_schema_v2_plan(tmp_path: Path) -> None:
    _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path))
    config = {
        **_config(tmp_path),
        "agents": {"security_reviewer": {"llm": {"provider": "unsupported"}}},
    }

    readiness = check_delivery_verification_readiness(status, config)

    assert readiness.required is True
    assert readiness.ready is True
    assert readiness.security_required is False
    assert readiness.active_unit_count == 1


def test_verification_readiness_uses_runtime_defaults_when_config_is_absent(tmp_path: Path) -> None:
    _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path))

    readiness = check_delivery_verification_readiness(status, {})

    assert readiness.ready is True


def test_delivery_check_ignores_corrupt_runtime_progress(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    progress_path = delivery_progress_path(tmp_path, "demo")
    progress_path.parent.mkdir(parents=True, exist_ok=True)
    progress_path.write_text("not json\n", encoding="utf-8")

    cmd_delivery_check(argparse.Namespace(plan_file=str(plan_path), json=True), _config(tmp_path))

    result = json.loads(capsys.readouterr().out)
    assert result["valid"] is True
    assert result["errors"] == []


def test_verification_readiness_requires_security_for_sensitive_plan(tmp_path: Path) -> None:
    _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path, risk_tags=["privacy"]))

    readiness = check_delivery_verification_readiness(status, _config(tmp_path))

    assert readiness.ready is True
    assert readiness.security_required is True


def test_verification_readiness_preserves_security_requirement_after_amendment(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, risk_tags=["privacy"])
    replacement_path = plan_path.parent / "units" / "replacement.md"
    replacement_path.write_text("# Replacement\n", encoding="utf-8")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["units"][0]["superseded_by"] = ["replacement"]
    plan["units"].append(
        {
            "id": "replacement",
            "task_path": replacement_path.relative_to(tmp_path).as_posix(),
            "depends_on": [],
            "risk_tags": [],
            "supersedes": "unit",
        }
    )
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    status = get_delivery_status(plan_path)

    readiness = check_delivery_verification_readiness(status, _config(tmp_path))

    assert status.valid is True
    assert readiness.ready is True
    assert readiness.active_unit_count == 1
    assert readiness.security_required is True


@pytest.mark.parametrize(
    "final_checks",
    [
        [{"name": "missing"}],
        None,
        "check contracts",
    ],
)
def test_verification_readiness_rejects_invalid_final_check_policy(
    tmp_path: Path,
    final_checks: object,
) -> None:
    _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path))
    config = {**_config(tmp_path), "delivery": {"verification": {"final_checks": final_checks}}}

    readiness = check_delivery_verification_readiness(status, config)

    assert readiness.ready is False
    assert any(issue.code == "delivery_verification.config_invalid" for issue in readiness.errors)


@pytest.mark.parametrize("delivery_config", [None, []])
def test_verification_readiness_rejects_non_mapping_delivery_config(
    tmp_path: Path,
    delivery_config: object,
) -> None:
    _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path))
    config = {**_config(tmp_path), "delivery": delivery_config}

    readiness = check_delivery_verification_readiness(status, config)

    assert readiness.ready is False
    assert any(issue.code == "delivery_verification.config_invalid" for issue in readiness.errors)


def test_schema_v2_finalize_requires_exact_passing_gate(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="demo",
        units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
        assembly_base_commit=base,
    )
    write_delivery_progress(delivery_progress_path(tmp_path, "demo"), progress)
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}

    preview = preview_delivery_finalize(plan_path, project_root=tmp_path, project_config=config)

    assert preview.ready is False
    assert any(issue.code == "delivery_verification.required" for issue in preview.errors)

    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    llm = _ReadonlyLLM([approval])
    reviewer = DeliveryIntegrationReviewAgent(llm, config)
    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=reviewer,
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is True
    assert result.status == "passed"
    assert result.semantic_status == "approved"
    assert result.security_status == "not_run"
    assert result.candidate_commit
    assert result.candidate_tree

    current_status = with_delivery_verification_readiness(get_delivery_status(plan_path), config)
    changed_config = {**config, "run_build": True, "run_tests": True}
    stale_status = with_delivery_verification_readiness(get_delivery_status(plan_path), changed_config)

    assert current_status.verification_status == "passed"
    assert stale_status.verification_status == "stale"
    assert stale_status.next_action == "verify the assembled delivery with delivery verify"

    finalized = finalize_delivery_plan(plan_path, project_root=tmp_path, project_config=config)

    assert finalized.finalized is True
    assert finalized.final_commit == result.candidate_commit

    finalized_status = get_delivery_status(plan_path)
    repeated = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=reviewer,
        security_reviewer=None,
        project_root=tmp_path,
    )
    repeated_status = get_delivery_status(plan_path)

    assert repeated.succeeded is True
    assert len(llm.calls) == 1
    assert repeated_status.final_branch == finalized_status.final_branch
    assert repeated_status.final_commit == finalized_status.final_commit
    assert repeated_status.finalized_at == finalized_status.finalized_at

    stale = finalize_delivery_plan(plan_path, project_root=tmp_path, project_config=changed_config)

    assert stale.finalized is False
    assert any(issue.code == "delivery_verification.stale" for issue in stale.errors)


def test_delivery_verification_blocks_candidate_config_drift_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    config_path = tmp_path / ".sikula" / "config.yaml"
    captured_source = b"project:\n  root_path: .\n  build_tool: python\n"
    config_path.write_bytes(captured_source)
    config = {
        **_config(tmp_path),
        "_config_path": str(config_path),
        "_config_source_fingerprint": f"sha256:{sha256(captured_source).hexdigest()}",
        "run_build": False,
        "run_tests": False,
        "run_checks": False,
    }
    config_path.write_text(
        "project:\n  root_path: .\n  build_tool: python\nrun_tests: true\n",
        encoding="utf-8",
    )
    unit_commit = _git_commit_all(tmp_path, "delivery unit changes config")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    monkeypatch.setattr(
        "core.delivery_verify.run_delivery_verification_validation",
        lambda *args, **kwargs: pytest.fail("validation must not run with stale configuration"),
    )
    llm = _ReadonlyLLM([])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.config_changed"
    assert result.next_action == "restart_with_candidate_config"
    assert result.validation_reused is False
    assert result.validation_executed is False
    assert llm.calls == []


def test_delivery_verification_accepts_external_runtime_config(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    _git_init(project_root)
    config_path = tmp_path / "external-config.yaml"
    source = b"project:\n  root_path: project\n  build_tool: python\n"
    config_path.write_bytes(source)
    config = {
        "_config_path": str(config_path),
        "_config_source_fingerprint": f"sha256:{sha256(source).hexdigest()}",
    }

    assert _candidate_config_matches_capture(project_root, project_root, config) is True


def test_delivery_verification_reviews_configured_nested_project(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    project_root = tmp_path / "apps" / "service"
    project_root.mkdir(parents=True)
    plan_path = _write_plan(project_root)
    rules_path = project_root / ".sikula" / "reviewer-rules.md"
    rules_path.write_text("Verify the nested project marker.\n", encoding="utf-8")
    (project_root / "project-marker.txt").write_text("nested project\n", encoding="utf-8")
    (tmp_path / "repository-marker.txt").write_text("repository root\n", encoding="utf-8")
    unit_commit = _git_commit_all(tmp_path, "nested delivery unit")
    progress = DeliveryProgress(
        schema_version=1,
        plan_id="demo",
        units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
        assembly_base_commit=base,
    )
    write_delivery_progress(delivery_progress_path(project_root, "demo"), progress)
    config = {
        **_config(project_root),
        "reviewer": {"extra_rules": ".sikula/reviewer-rules.md"},
        "run_build": False,
        "run_tests": False,
        "run_checks": False,
    }
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    llm = _ReadonlyLLM([approval])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(project_root / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=project_root,
    )

    assert result.succeeded is True
    prompt, reviewer_root = llm.calls[0]
    assert reviewer_root.parts[-2:] == ("apps", "service")
    assert "Verify the nested project marker." in prompt


def test_delivery_check_and_status_use_configured_nested_project_root(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _git_init(tmp_path)
    project_root = tmp_path / "apps" / "service"
    project_root.mkdir(parents=True)
    plan_path = _write_plan(project_root)
    config = _config(project_root)

    cmd_delivery_check(argparse.Namespace(plan_file=str(plan_path), json=True), config)
    check_result = json.loads(capsys.readouterr().out)
    cmd_delivery_status(argparse.Namespace(plan_file=str(plan_path), json=True), config)
    status_result = json.loads(capsys.readouterr().out)

    assert check_result["valid"] is True
    assert check_result["errors"] == []
    assert status_result["valid"] is True
    assert status_result["errors"] == []


def test_schema_v2_finalize_rechecks_branch_before_persisting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    verified = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(_ReadonlyLLM([approval]), config),
        security_reviewer=None,
        project_root=tmp_path,
    )
    assert verified.succeeded is True
    moved = False

    def read_and_move_branch(path: Path, *, plan_id: str) -> tuple[object, object]:
        nonlocal moved
        loaded = read_delivery_progress(path, plan_id=plan_id)
        if not moved:
            subprocess.run(
                ["git", "update-ref", "refs/heads/sikula/delivery/demo", base],
                cwd=tmp_path,
                check=True,
            )
            moved = True
        return loaded

    monkeypatch.setattr("core.delivery_finalize.read_delivery_progress", read_and_move_branch)

    finalized = finalize_delivery_plan(plan_path, project_root=tmp_path, project_config=config)
    progress, errors = read_delivery_progress(progress_path, plan_id="demo")

    assert finalized.finalized is False
    assert [issue.code for issue in finalized.errors] == ["delivery_verification.ref_changed"]
    assert progress is not None and not errors
    assert progress.final_commit is None
    assert progress.finalized_at is None


def test_delivery_verification_reuses_current_exact_pass(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    llm = _ReadonlyLLM([approval])
    reviewer = DeliveryIntegrationReviewAgent(llm, config)

    first = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=reviewer,
        security_reviewer=None,
        project_root=tmp_path,
    )
    second = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=reviewer,
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert first.succeeded is True
    assert second.succeeded is True
    assert second.gate_id == first.gate_id
    assert second.attempt == first.attempt
    assert len(llm.calls) == 1

    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "# changed after approval\n", encoding="utf-8")

    assert get_delivery_status(plan_path).verification_status == "stale"


def test_failed_delivery_verification_becomes_stale_when_config_changes(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    verified = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(_ReadonlyLLM([approval]), config),
        security_reviewer=None,
        project_root=tmp_path,
    )
    assert verified.succeeded is True
    progress, errors = read_delivery_progress(progress_path, plan_id="demo")
    assert progress is not None and not errors and progress.verification is not None
    failed = replace(
        progress.verification,
        status="failed",
        stop_code="delivery_verification.scope_amendment_required",
    )
    write_delivery_progress(progress_path, mark_delivery_verification(progress, failed))

    current = with_delivery_verification_readiness(get_delivery_status(plan_path), config)
    changed = with_delivery_verification_readiness(
        get_delivery_status(plan_path),
        {**config, "run_build": True},
    )

    assert current.verification_status == "failed"
    assert changed.verification_status == "stale"
    assert changed.next_action == "verify the assembled delivery with delivery verify"

    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "# changed after rejection\n", encoding="utf-8")

    assert get_delivery_status(plan_path).verification_status == "stale"


def test_delivery_verification_repairs_missing_terminal_event_on_reuse(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    llm = _ReadonlyLLM([approval])
    reviewer = DeliveryIntegrationReviewAgent(llm, config)
    append_event = delivery_verify_module.append_delivery_progress_event

    def fail_terminal_event(path: Path, event: DeliveryProgressEvent) -> None:
        if event.event_type == "verification.passed":
            raise OSError("event storage unavailable")
        append_event(path, event)

    monkeypatch.setattr(delivery_verify_module, "append_delivery_progress_event", fail_terminal_event)
    with pytest.raises(OSError, match="event storage unavailable"):
        verify_delivery_plan(
            plan_path,
            config,
            state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
            semantic_reviewer=reviewer,
            security_reviewer=None,
            project_root=tmp_path,
        )

    progress, errors = read_delivery_progress(delivery_progress_path(tmp_path, "demo"), plan_id="demo")
    assert progress is not None and not errors and progress.verification is not None
    assert progress.verification.status == "passed"
    monkeypatch.setattr(delivery_verify_module, "append_delivery_progress_event", append_event)

    repaired = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=reviewer,
        security_reviewer=None,
        project_root=tmp_path,
    )

    events = [
        json.loads(line) for line in delivery_events_path(tmp_path, "demo").read_text(encoding="utf-8").splitlines()
    ]
    assert repaired.succeeded is True
    assert len(llm.calls) == 1
    assert [event["event_type"] for event in events if event["event_type"].startswith("verification.")] == [
        "verification.running",
        "verification.passed",
    ]


def test_delivery_verification_records_blocked_event_when_audit_setup_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    monkeypatch.setattr("core.delivery_verify._safe_append_audit", lambda *args, **kwargs: False)
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(_ReadonlyLLM([]), config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    progress, errors = read_delivery_progress(progress_path, plan_id="demo")
    events = [
        json.loads(line) for line in delivery_events_path(tmp_path, "demo").read_text(encoding="utf-8").splitlines()
    ]
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.audit_unavailable"
    assert progress is not None and not errors and progress.verification is not None
    assert progress.verification.status == "blocked"
    assert [event["event_type"] for event in events[-2:]] == ["verification.running", "verification.blocked"]


def test_delivery_verification_records_stale_when_plan_changes_during_review(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'

    def mutate_plan() -> None:
        plan_path.write_text(plan_path.read_text(encoding="utf-8") + "# concurrent update\n", encoding="utf-8")

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(_SideEffectReadonlyLLM(approval, mutate_plan), config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "stale"
    assert result.stop_code == "delivery_verification.candidate_changed"
    assert get_delivery_status(plan_path, project_root=tmp_path).verification_status == "stale"


def test_delivery_verification_reviews_source_captured_before_validation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    source_path = tmp_path / ".sikula" / "tasks" / "source.md"
    original_source = source_path.read_text(encoding="utf-8")

    def mutate_source_during_validation(*args, **kwargs) -> DeliveryVerificationValidationResult:
        source_path.write_text("# Transient authority\n", encoding="utf-8")
        return DeliveryVerificationValidationResult(passed=True, reused=False, executed=True)

    monkeypatch.setattr(
        "core.delivery_verify.run_delivery_verification_validation",
        mutate_source_during_validation,
    )
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    llm = _SideEffectReadonlyLLM(
        approval,
        lambda: source_path.write_text(original_source, encoding="utf-8"),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is True
    assert original_source in llm.calls[0][0]
    assert "Transient authority" not in llm.calls[0][0]


@pytest.mark.parametrize("private_kind", ["configured_root", "platform_env"])
def test_delivery_verification_rejects_configured_private_source_before_provider(
    tmp_path: Path,
    private_kind: str,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    relative_source = ".private-state/source.md" if private_kind == "configured_root" else "local.properties"
    private_source = tmp_path / relative_source
    private_source.parent.mkdir(parents=True, exist_ok=True)
    private_text = "PRIVATE=value\n"
    private_source.write_text(private_text, encoding="utf-8")
    plan = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    plan["source_task"] = {
        "path": private_source.relative_to(tmp_path).as_posix(),
        "sha256": "sha256:" + sha256(private_text.encode("utf-8")).hexdigest(),
    }
    plan_path.write_text(yaml.safe_dump(plan, sort_keys=False), encoding="utf-8")
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {
        **_config(tmp_path),
        "run_build": False,
        "run_tests": False,
        "run_checks": False,
    }
    if private_kind == "configured_root":
        config["tasks"] = {"state_dir": ".private-state"}
    else:
        config["project"] = {**config["project"], "build_tool": "gradle-android"}
    llm = _ReadonlyLLM([])
    readiness = check_delivery_verification_readiness(get_delivery_status(plan_path), config)

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    progress, errors = read_delivery_progress(progress_path, plan_id="demo")
    assert readiness.ready is False
    assert {issue.code for issue in readiness.errors} == {"delivery_verification.source_private"}
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.not_ready"
    assert {issue.code for issue in result.errors} == {"delivery_verification.source_private"}
    assert llm.calls == []
    assert progress is not None and not errors
    assert progress.assembly_status is None


def test_delivery_verification_does_not_overwrite_a_newer_running_attempt(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )

    def supersede_attempt() -> None:
        progress, errors = read_delivery_progress(progress_path, plan_id="demo")
        assert progress is not None and not errors and progress.verification is not None
        newer = replace(progress.verification, attempt=progress.verification.attempt + 1)
        write_delivery_progress(progress_path, mark_delivery_verification(progress, newer))

    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(
            _SideEffectReadonlyLLM(approval, supersede_attempt),
            config,
        ),
        security_reviewer=None,
        project_root=tmp_path,
    )

    persisted, errors = read_delivery_progress(progress_path, plan_id="demo")
    assert result.status == "stale"
    assert persisted is not None and not errors and persisted.verification is not None
    assert persisted.verification.status == "running"
    assert persisted.verification.attempt == 2


def test_security_sensitive_gate_requires_independent_security_approval(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, risk_tags=["privacy"])
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    semantic_llm = _ReadonlyLLM([approval])
    security_llm = _ReadonlyLLM([approval])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(semantic_llm, config),
        security_reviewer=DeliveryIntegrationReviewAgent(security_llm, config),
        project_root=tmp_path,
    )

    assert result.succeeded is True
    assert result.semantic_status == "approved"
    assert result.security_required is True
    assert result.security_status == "approved"
    assert len(semantic_llm.calls) == 1
    assert len(security_llm.calls) == 1

    changed_config = {**config, "security": {"context": "Apply the strengthened privacy boundary."}}
    stale = preview_delivery_finalize(plan_path, project_root=tmp_path, project_config=changed_config)

    assert stale.ready is False
    assert any(issue.code == "delivery_verification.stale" for issue in stale.errors)


@pytest.mark.parametrize("failed_review", ["semantic", "security"])
def test_reviewer_failure_preserves_completed_gate_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failed_review: str,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, risk_tags=["privacy"] if failed_review == "security" else None)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    validation = DeliveryVerificationValidationResult(passed=True, reused=False, executed=True)
    monkeypatch.setattr(
        "core.delivery_verify.run_delivery_verification_validation",
        lambda *args, **kwargs: validation,
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    semantic_llm = _FailingReadonlyLLM() if failed_review == "semantic" else _ReadonlyLLM([approval])
    security_reviewer = (
        DeliveryIntegrationReviewAgent(_FailingReadonlyLLM(), config) if failed_review == "security" else None
    )

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(semantic_llm, config),
        security_reviewer=security_reviewer,
        project_root=tmp_path,
    )

    progress, errors = read_delivery_progress(progress_path, plan_id="demo")
    assert result.status == "blocked"
    assert result.validation_executed is True
    assert result.semantic_status == ("blocked" if failed_review == "semantic" else "approved")
    assert result.security_status == ("blocked" if failed_review == "security" else "not_run")
    assert progress is not None and not errors and progress.verification is not None
    assert progress.verification.validation_executed is True
    assert progress.verification.semantic_status == result.semantic_status
    assert progress.verification.security_status == result.security_status


def test_terminal_audit_failure_preserves_completed_gate_phases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, risk_tags=["privacy"])
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    validation = DeliveryVerificationValidationResult(passed=True, reused=True, executed=True)
    monkeypatch.setattr(
        "core.delivery_verify.run_delivery_verification_validation",
        lambda *args, **kwargs: validation,
    )

    def fail_terminal_append(path: Path, value: dict[str, object], *, project_root: Path) -> bool:
        if value.get("event") == "failed":
            return False
        return _safe_append_audit(path, value, project_root=project_root)

    monkeypatch.setattr("core.delivery_verify._safe_append_audit", fail_terminal_append)
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    rejection = (
        '{"schema_version":1,"disposition":"repair_required","summary":"Repair required.",'
        '"findings":[{"code":"integration_gap","summary":"Repair the integration.","unit_ids":["unit"]}]}'
    )

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(_ReadonlyLLM([approval]), config),
        security_reviewer=DeliveryIntegrationReviewAgent(_ReadonlyLLM([rejection]), config),
        project_root=tmp_path,
    )

    progress, errors = read_delivery_progress(progress_path, plan_id="demo")
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.audit_unavailable"
    assert result.validation_reused is True
    assert result.validation_executed is True
    assert result.semantic_status == "approved"
    assert result.security_status == "rejected"
    assert result.finding_count == 1
    assert progress is not None and not errors and progress.verification is not None
    assert progress.verification.validation_reused is True
    assert progress.verification.validation_executed is True
    assert progress.verification.semantic_status == "approved"
    assert progress.verification.security_status == "rejected"
    assert progress.verification.finding_count == 1


def test_review_audit_failure_preserves_evidence_in_terminal_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    progress_path = delivery_progress_path(tmp_path, "demo")
    write_delivery_progress(
        progress_path,
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    usage = {"agent": "reviewer", "reported_tokens": 42}
    reviewer = DeliveryIntegrationReviewAgent(
        _ReadonlyLLM([approval]),
        config,
        usage_records=[usage],
    )
    original_safe_append = _safe_append_audit
    failed = False

    def fail_review_append_once(path: Path, value: dict[str, object], *, project_root: Path) -> bool:
        nonlocal failed
        if value.get("event") == "semantic_review" and not failed:
            failed = True
            return False
        return original_safe_append(path, value, project_root=project_root)

    monkeypatch.setattr("core.delivery_verify._safe_append_audit", fail_review_append_once)

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=reviewer,
        security_reviewer=None,
        project_root=tmp_path,
    )

    progress, errors = read_delivery_progress(progress_path, plan_id="demo")
    audit_records = [
        json.loads(line)
        for line in (progress_path.parent / "verification.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    fallback = next(record for record in audit_records if "review_evidence" in record)
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.audit_unavailable"
    assert result.semantic_status == "approved"
    assert progress is not None and not errors and progress.verification is not None
    assert progress.verification.semantic_status == "approved"
    assert fallback["event"] == "blocked"
    assert fallback["review_evidence"]["event"] == "semantic_review"
    assert fallback["review_evidence"]["assessment"]["disposition"] == "approved"
    assert fallback["review_evidence"]["attempts"][0]["output"] == approval
    assert fallback["review_evidence"]["usage"] == [usage]
    assert reviewer.consume_usage_records() == []


@pytest.mark.parametrize("ignored_root", [".provider", ".venv"])
def test_delivery_verification_rejects_ignored_readonly_provider_mutation(
    tmp_path: Path,
    ignored_root: str,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    (tmp_path / ".gitignore").write_text(f"{ignored_root}/\n", encoding="utf-8")
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'

    def mutate_ignored_path() -> None:
        target = next((tmp_path / ".sikula" / "worktrees" / "delivery-verification").glob("candidate-*"))
        (target / ignored_root).mkdir()
        (target / ignored_root / "state.json").write_text("changed\n", encoding="utf-8")

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(_SideEffectReadonlyLLM(approval, mutate_ignored_path), config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.readonly_mutation"


def test_delivery_verification_classifies_reviewer_workspace_setup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    validation = DeliveryVerificationValidationResult(passed=True, reused=True, executed=True)
    monkeypatch.setattr(
        "core.delivery_verify.run_delivery_verification_validation",
        lambda *args, **kwargs: validation,
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    llm = _WorkspaceSetupFailingReadonlyLLM([])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.reviewer_workspace_unavailable"
    assert result.validation_reused is True
    assert result.validation_executed is True
    assert result.semantic_status == "blocked"
    assert llm.calls == []


def test_delivery_verification_rejects_candidate_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret\n", encoding="utf-8")
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    try:
        (tmp_path / "escape").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not permit symlink creation")
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    llm = _ReadonlyLLM([approval])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.workspace_audit_unavailable"
    assert llm.calls == []


def test_delivery_verification_rejects_narrow_reviewer_read_scope_before_provider(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {
        **_config(tmp_path),
        "sandbox": {"allowed_read_paths": ["src"]},
        "run_build": False,
        "run_tests": False,
        "run_checks": False,
    }
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    llm = _ReadonlyLLM([approval])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "blocked"
    assert result.stop_code == "delivery_verification.not_ready"
    assert {issue.code for issue in result.errors} == {"delivery_verification.read_scope_unsupported"}
    assert llm.calls == []


def test_delivery_verification_allows_internal_symlink_with_full_reviewer_scope(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    (tmp_path / "src" / "real").mkdir(parents=True)
    (tmp_path / "src" / "real" / "module.py").write_text("VALUE = 1\n", encoding="utf-8")
    try:
        (tmp_path / "src" / "alias").symlink_to("real", target_is_directory=True)
    except OSError:
        pytest.skip("filesystem does not permit symlink creation")
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {
        **_config(tmp_path),
        "sandbox": {"allowed_read_paths": ["."]},
        "run_build": False,
        "run_tests": False,
        "run_checks": False,
    }
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'
    llm = _ReadonlyLLM([approval])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is True
    assert result.status == "passed"
    assert len(llm.calls) == 1


def test_delivery_verification_rejects_persistent_ignored_validation_output(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    (tmp_path / ".gitignore").write_text(".persistent/\n", encoding="utf-8")
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {
        **_config(tmp_path),
        "run_build": False,
        "run_tests": False,
        "run_checks": False,
        "delivery": {
            "verification": {
                "final_checks": [
                    {
                        "name": "persistent-output",
                        "command": "mkdir -p .persistent && printf changed > .persistent/output.txt",
                    }
                ]
            }
        },
    }
    approval = '{"schema_version":1,"disposition":"approved","summary":"Complete and coherent.","findings":[]}'
    llm = _ReadonlyLLM([approval])

    result = verify_delivery_plan(
        plan_path,
        config,
        state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
        semantic_reviewer=DeliveryIntegrationReviewAgent(llm, config),
        security_reviewer=None,
        project_root=tmp_path,
    )

    assert result.succeeded is False
    assert result.status == "failed"
    assert result.stop_code == "delivery_verification.validation_workspace_mutated"
    assert llm.calls == []


def test_delivery_verification_interruption_is_durable_and_propagated(tmp_path: Path) -> None:
    base = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    unit_commit = _git_commit_all(tmp_path, "delivery unit")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "demo"),
        DeliveryProgress(
            schema_version=1,
            plan_id="demo",
            units=[make_delivery_unit_progress("unit", "done", commit=unit_commit)],
            assembly_base_commit=base,
        ),
    )
    config = {**_config(tmp_path), "run_build": False, "run_tests": False, "run_checks": False}

    def interrupt() -> None:
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        verify_delivery_plan(
            plan_path,
            config,
            state_store=JsonStateStore(tmp_path / ".sikula" / "state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(_SideEffectReadonlyLLM("unused", interrupt), config),
            security_reviewer=None,
            project_root=tmp_path,
        )

    status = get_delivery_status(plan_path, project_root=tmp_path)
    assert status.verification_status == "interrupted"
    assert status.verification is not None
    assert status.verification.stop_code == "delivery_verification.interrupted"


def test_delivery_verify_projection_matches_published_schema_shape() -> None:
    schema = json.loads(
        (Path(__file__).parents[1] / "docs" / "schemas" / "delivery-verification-result.v1.schema.json").read_text(
            encoding="utf-8"
        )
    )
    projection = _result_projection()

    assert set(schema["required"]) <= set(projection)
    assert set(projection) <= set(schema["properties"])
    assert projection["schema_version"] == schema["properties"]["schema_version"]["const"]
    assert projection["command"] == schema["properties"]["command"]["const"]
    assert projection["privacy_mode"] == schema["properties"]["privacy_mode"]["const"]
    assert projection["status"] in schema["properties"]["status"]["enum"]


def test_delivery_verify_parser_accepts_reviewer_overrides() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_parser(subparsers)

    args = parser.parse_args(
        [
            "delivery",
            "verify",
            "plan.yaml",
            "--json",
            "--agent-model",
            "reviewer=gpt-5.5",
            "--agent-provider",
            "security_reviewer=claude",
        ]
    )

    assert args.delivery_command == "verify"
    assert args.json is True
    assert args.agent_model == ["reviewer=gpt-5.5"]
    assert args.agent_provider == ["security_reviewer=claude"]


@pytest.mark.parametrize("command", ["status", "finalize"])
def test_delivery_verification_followup_parsers_accept_reviewer_overrides(command: str) -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_parser(subparsers)

    args = parser.parse_args(
        [
            "delivery",
            command,
            "plan.yaml",
            "--agent-model",
            "reviewer=gpt-5.5",
            "--agent-provider",
            "security_reviewer=claude",
        ]
    )

    assert args.delivery_command == command
    assert args.agent_model == ["reviewer=gpt-5.5"]
    assert args.agent_provider == ["security_reviewer=claude"]


@pytest.mark.parametrize(
    ("command", "delegate"),
    [
        ("cmd_delivery_status", "cmd_delivery_status"),
        ("cmd_delivery_finalize", "cmd_delivery_finalize"),
    ],
)
def test_delivery_verification_followups_apply_reviewer_overrides(
    monkeypatch: pytest.MonkeyPatch,
    command: str,
    delegate: str,
) -> None:
    captured: list[dict] = []
    monkeypatch.setattr(sikula_module.cli_delivery, delegate, lambda args, cfg: captured.append(cfg))
    args = argparse.Namespace(
        agent_model=["reviewer=gpt-5.5"],
        agent_provider=["security_reviewer=claude"],
        agent_timeout=None,
    )

    getattr(sikula_module, command)(args, _config(Path(".")))

    assert captured[0]["agents"]["reviewer"]["llm"]["model"] == "gpt-5.5"
    assert captured[0]["agents"]["security_reviewer"]["llm"]["provider"] == "claude"


def test_cmd_delivery_verify_prints_structured_projection(capsys: pytest.CaptureFixture[str]) -> None:
    result = _result_projection_object()
    context = SimpleNamespace(verify_plan=lambda args, cfg: result)
    args = argparse.Namespace(
        plan_file="plan.yaml",
        json=True,
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )

    cmd_delivery_verify(args, {}, context)

    assert json.loads(capsys.readouterr().out) == result.to_dict()


def _result_projection() -> dict:
    return _result_projection_object().to_dict()


def _result_projection_object() -> DeliveryVerifyResult:
    return DeliveryVerifyResult(
        plan_path="/private/project/.sikula/delivery/demo/plan.yaml",
        project_root="/private/project",
        valid=True,
        ready=True,
        succeeded=True,
        status="passed",
        gate_id=_SHA,
        candidate_commit=_COMMIT,
        candidate_tree=_TREE,
        attempt=1,
        semantic_status="approved",
    )


def test_delivery_verify_projection_bounds_long_plan_path() -> None:
    relative = "/".join(["nested"] * 100 + ["plan.yaml"])
    result = replace(
        _result_projection_object(),
        plan_path=f"/private/project/{relative}",
    ).to_dict()

    assert result["plan_path"] == "<redacted>"
    assert len(result["plan_path"]) <= 500


def test_verification_readiness_rejects_oversized_source_before_execution(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    data = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    source_path = tmp_path / data["source_task"]["path"]
    source_text = "x" * (MAX_DELIVERY_VERIFICATION_SOURCE_BYTES + 1)
    source_path.write_text(source_text, encoding="utf-8")
    data["source_task"]["sha256"] = "sha256:" + sha256(source_text.encode("utf-8")).hexdigest()
    plan_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    status = get_delivery_status(plan_path)

    readiness = check_delivery_verification_readiness(status, _config(tmp_path))

    assert readiness.ready is False
    assert {issue.code for issue in readiness.errors} == {"delivery_verification.hierarchy_required"}


@pytest.mark.parametrize("payload_kind", ["reviewer_rules", "security_context", "final_checks"])
def test_verification_readiness_bounds_complete_review_packet(tmp_path: Path, payload_kind: str) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, risk_tags=["privacy"] if payload_kind == "security_context" else None)
    config = _config(tmp_path)
    oversized = "x" * (MAX_DELIVERY_VERIFICATION_PACKET_BYTES + 1)
    if payload_kind == "reviewer_rules":
        rules_path = tmp_path / ".sikula" / "reviewer-rules.md"
        rules_path.write_text(oversized, encoding="utf-8")
        config["reviewer"] = {"extra_rules": rules_path.relative_to(tmp_path).as_posix()}
    elif payload_kind == "security_context":
        config["security"] = {"context": oversized}
    else:
        config["delivery"] = {"verification": {"final_checks": [{"name": "large-policy", "command": oversized}]}}

    readiness = check_delivery_verification_readiness(get_delivery_status(plan_path), config)

    assert readiness.ready is False
    assert any(issue.code == "delivery_verification.hierarchy_required" for issue in readiness.errors)


def test_verification_readiness_rejects_rules_outside_reviewer_read_scope(tmp_path: Path) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "private-rules.md").write_text("Private rule.\n", encoding="utf-8")
    config = {
        **_config(tmp_path),
        "sandbox": {"allowed_read_paths": ["src"]},
        "reviewer": {"extra_rules": "private-rules.md"},
    }

    readiness = check_delivery_verification_readiness(get_delivery_status(plan_path), config)

    assert readiness.ready is False
    assert {issue.code for issue in readiness.errors} == {
        "delivery_verification.read_scope_unsupported",
        "delivery_verification.review_rules_unavailable",
    }


def test_verification_identity_changes_with_plan_or_config(tmp_path: Path) -> None:
    commit = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    status = get_delivery_status(plan_path)
    config = _config(tmp_path)

    first = build_delivery_verification_identity(status, config, candidate_commit=commit)
    config["run_tests"] = False
    second = build_delivery_verification_identity(status, config, candidate_commit=commit)
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "# comment\n", encoding="utf-8")
    changed_status = get_delivery_status(plan_path)
    third = build_delivery_verification_identity(changed_status, config, candidate_commit=commit)

    assert first.candidate_commit == commit
    assert first.candidate_tree
    assert first.gate_id != second.gate_id
    assert second.gate_id != third.gate_id


def test_verification_identity_binds_loaded_config_source(tmp_path: Path) -> None:
    commit = _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path))
    config = {
        **_config(tmp_path),
        "_config_source_fingerprint": "sha256:" + "1" * 64,
    }

    first = build_delivery_verification_identity(status, config, candidate_commit=commit)
    config["_config_source_fingerprint"] = "sha256:" + "2" * 64
    second = build_delivery_verification_identity(status, config, candidate_commit=commit)

    assert first.config_fingerprint != second.config_fingerprint
    assert first.gate_id != second.gate_id


def test_verification_identity_uses_the_bytes_parsed_into_status(tmp_path: Path) -> None:
    commit = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    status = get_delivery_status(plan_path)
    captured_fingerprint = status.plan_fingerprint
    plan_path.write_text(plan_path.read_text(encoding="utf-8") + "# concurrent update\n", encoding="utf-8")

    identity = build_delivery_verification_identity(status, _config(tmp_path), candidate_commit=commit)

    assert identity.plan_fingerprint == captured_fingerprint
    assert identity.plan_fingerprint != "sha256:" + sha256(plan_path.read_bytes()).hexdigest()


def test_security_sensitive_identity_changes_with_security_review_policy(tmp_path: Path) -> None:
    commit = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path, risk_tags=["privacy"])
    status = get_delivery_status(plan_path)
    config = _config(tmp_path)

    first = build_delivery_verification_identity(status, config, candidate_commit=commit)
    changed_config = {
        **config,
        "security": {"context": "Apply the strengthened privacy boundary."},
        "security_reviewer": {"extra_rules": ".sikula/security-rules.md"},
    }
    second = build_delivery_verification_identity(status, changed_config, candidate_commit=commit)

    assert first.gate_id != second.gate_id
    assert first.config_fingerprint != second.config_fingerprint


@pytest.mark.parametrize(
    ("risk_tags", "agent_name"),
    [
        ([], "reviewer"),
        (["privacy"], "security_reviewer"),
    ],
)
def test_verification_identity_changes_with_required_reviewer_llm(
    tmp_path: Path,
    risk_tags: list[str],
    agent_name: str,
) -> None:
    commit = _git_init(tmp_path)
    status = get_delivery_status(_write_plan(tmp_path, risk_tags=risk_tags))
    config = _config(tmp_path)

    first = build_delivery_verification_identity(status, config, candidate_commit=commit)
    changed_config = {
        **config,
        "agents": {agent_name: {"llm": {"provider": "gemini", "model": "gemini-review"}}},
    }
    second = build_delivery_verification_identity(status, changed_config, candidate_commit=commit)

    assert first.gate_id != second.gate_id
    assert first.config_fingerprint != second.config_fingerprint


def test_integration_review_parser_accepts_exact_final_control_object() -> None:
    output = (
        "The candidate is coherent.\n"
        '{"schema_version":1,"disposition":"approved",'
        '"summary":"No blocking integration issues found.","findings":[]}'
    )

    assessment = parse_delivery_integration_review(output, known_unit_ids={"unit"})

    assert assessment.approved is True
    assert assessment.findings == []


def test_integration_reviewer_rejects_exact_oversized_prompt_before_provider(tmp_path: Path) -> None:
    llm = _ReadonlyLLM([])
    reviewer = DeliveryIntegrationReviewAgent(llm, _config(tmp_path))

    with pytest.raises(DeliveryIntegrationReviewAgentError) as exc_info:
        reviewer.review(
            cwd=tmp_path,
            review_kind="semantic",
            source_task="x" * (MAX_DELIVERY_VERIFICATION_PACKET_BYTES + 1),
            plan_context={},
            validation_summary={},
            candidate_commit=_COMMIT,
            candidate_tree=_TREE,
            known_unit_ids=set(),
        )

    assert exc_info.value.code == "delivery_verification.hierarchy_required"
    assert llm.calls == []


@pytest.mark.parametrize(
    "output",
    [
        "",
        'Decision: {"schema_version":1,"disposition":"approved","summary":"Clear.","findings":[]}',
        '{"schema_version":1,"disposition":"approved","summary":"Clear.","findings":[]} trailing',
        '{"schema_version":1,"disposition":"approved","summary":"Clear.",'
        '"findings":[{"code":"x","summary":"Issue.","unit_ids":["unit"]}]}',
        '{"schema_version":1,"disposition":"repair_required","summary":"Needs repair.","findings":[]}',
        '{"schema_version":1,"disposition":"repair_required","summary":"Needs repair.",'
        '"findings":[{"code":"x","summary":"Issue.","unit_ids":["unknown"]}]}',
        '{"schema_version":1,"disposition":[],"summary":"Invalid.","findings":[]}',
        '{"schema_version":true,"disposition":"approved","summary":"Invalid.","findings":[]}',
        '{"schema_version":1.0,"disposition":"approved","summary":"Invalid.","findings":[]}',
    ],
)
def test_integration_review_parser_fails_closed(output: str) -> None:
    with pytest.raises(DeliveryIntegrationReviewParseError):
        parse_delivery_integration_review(output, known_unit_ids={"unit"})


def test_integration_review_agent_retries_one_malformed_response(tmp_path: Path) -> None:
    llm = _ReadonlyLLM(
        [
            "APPROVED",
            '{"schema_version":1,"disposition":"approved",'
            '"summary":"No blocking integration issues found.","findings":[]}',
        ]
    )
    agent = DeliveryIntegrationReviewAgent(llm, {})

    result = agent.review(
        cwd=tmp_path,
        review_kind="semantic",
        source_task="# Source",
        plan_context={"units": [{"id": "unit"}]},
        validation_summary={"status": "passed"},
        candidate_commit=_COMMIT,
        candidate_tree=_TREE,
        known_unit_ids={"unit"},
    )

    assert result.assessment.approved is True
    assert len(result.attempts) == 2
    assert "previous response was rejected" in llm.calls[1][0]


def test_integration_review_agent_blocks_after_second_protocol_error(tmp_path: Path) -> None:
    llm = _ReadonlyLLM(["APPROVED", "LGTM"])
    agent = DeliveryIntegrationReviewAgent(llm, {})

    with pytest.raises(DeliveryIntegrationReviewAgentError) as exc_info:
        agent.review(
            cwd=tmp_path,
            review_kind="security",
            source_task="# Source",
            plan_context={"units": [{"id": "unit"}]},
            validation_summary={"status": "passed"},
            candidate_commit=_COMMIT,
            candidate_tree=_TREE,
            known_unit_ids={"unit"},
        )

    assert exc_info.value.code == "delivery_verification.review_json_invalid"
    assert len(exc_info.value.attempts) == 2


@pytest.mark.parametrize(
    ("review_kind", "agent_name", "rule_text"),
    [
        ("semantic", "reviewer", "Verify the project-specific consistency invariant."),
        ("security", "security_reviewer", "Verify the project-specific privacy invariant."),
    ],
)
def test_integration_review_agent_applies_project_specific_rules(
    tmp_path: Path,
    review_kind: str,
    agent_name: str,
    rule_text: str,
) -> None:
    rules_path = tmp_path / f"{agent_name}-rules.md"
    rules_path.write_text(rule_text, encoding="utf-8")
    config = {agent_name: {"extra_rules": rules_path.name}}
    llm = _ReadonlyLLM(['{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'])

    DeliveryIntegrationReviewAgent(llm, config).review(
        cwd=tmp_path,
        review_kind=review_kind,
        source_task="# Source",
        plan_context={"units": [{"id": "unit"}]},
        validation_summary={"status": "passed"},
        candidate_commit=_COMMIT,
        candidate_tree=_TREE,
        known_unit_ids={"unit"},
    )

    assert rule_text in llm.calls[0][0]
    assert "## Project-specific rules" in llm.calls[0][0]


def test_integration_review_agent_rejects_missing_project_specific_rules(tmp_path: Path) -> None:
    llm = _ReadonlyLLM([])
    agent = DeliveryIntegrationReviewAgent(
        llm,
        {"reviewer": {"extra_rules": "missing-reviewer-rules.md"}},
    )

    with pytest.raises(DeliveryIntegrationReviewAgentError) as exc_info:
        agent.review(
            cwd=tmp_path,
            review_kind="semantic",
            source_task="# Source",
            plan_context={"units": [{"id": "unit"}]},
            validation_summary={"status": "passed"},
            candidate_commit=_COMMIT,
            candidate_tree=_TREE,
            known_unit_ids={"unit"},
        )

    assert exc_info.value.code == "delivery_verification.review_rules_unavailable"
    assert llm.calls == []


def test_integration_review_agent_rejects_narrow_read_scope_before_provider(tmp_path: Path) -> None:
    config = {"sandbox": {"allowed_read_paths": ["src"]}}
    llm = _ReadonlyLLM([])

    with pytest.raises(DeliveryIntegrationReviewAgentError) as exc_info:
        DeliveryIntegrationReviewAgent(llm, config).review(
            cwd=tmp_path,
            review_kind="semantic",
            source_task="# Source",
            plan_context={"units": [{"id": "unit"}]},
            validation_summary={"status": "passed"},
            candidate_commit=_COMMIT,
            candidate_tree=_TREE,
            known_unit_ids={"unit"},
        )

    assert exc_info.value.code == "delivery_verification.read_scope_unsupported"
    assert llm.calls == []


def test_integration_review_prompt_declares_configured_read_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    config = {"sandbox": {"allowed_read_paths": ["."]}}
    llm = _ReadonlyLLM(['{"schema_version":1,"disposition":"approved","summary":"Complete.","findings":[]}'])

    DeliveryIntegrationReviewAgent(llm, config).review(
        cwd=tmp_path,
        review_kind="semantic",
        source_task="# Source",
        plan_context={"units": [{"id": "unit"}]},
        validation_summary={"status": "passed"},
        candidate_commit=_COMMIT,
        candidate_tree=_TREE,
        known_unit_ids={"unit"},
    )

    assert 'configured paths: ["."]' in llm.calls[0][0]


@pytest.mark.skipif(os.name == "nt", reason="POSIX directory modes are not portable to Windows")
def test_verification_worktree_preserves_existing_ancestor_permissions(tmp_path: Path) -> None:
    commit = _git_init(tmp_path)
    sikula_dir = tmp_path / ".sikula"
    worktrees_dir = sikula_dir / "worktrees"
    worktrees_dir.mkdir(parents=True)
    sikula_dir.chmod(0o750)
    worktrees_dir.chmod(0o751)

    with detached_delivery_verification_worktree(tmp_path, commit) as worktree:
        assert worktree.is_dir()
        assert stat.S_IMODE((worktrees_dir / "delivery-verification").stat().st_mode) == 0o700

    assert stat.S_IMODE(sikula_dir.stat().st_mode) == 0o750
    assert stat.S_IMODE(worktrees_dir.stat().st_mode) == 0o751


def test_verification_worktree_preserves_nested_project_root(tmp_path: Path) -> None:
    _git_init(tmp_path)
    project_root = tmp_path / "apps" / "service"
    project_root.mkdir(parents=True)
    (project_root / "project-marker.txt").write_text("nested project\n", encoding="utf-8")
    (tmp_path / "repository-marker.txt").write_text("repository root\n", encoding="utf-8")
    commit = _git_commit_all(tmp_path, "add nested project")

    with detached_delivery_verification_worktree(project_root, commit) as candidate_root:
        assert candidate_root.parts[-2:] == ("apps", "service")
        assert (candidate_root / "project-marker.txt").read_text(encoding="utf-8") == "nested project\n"
        assert not (candidate_root / "repository-marker.txt").exists()


def test_delivery_verification_ignores_git_replace_objects(tmp_path: Path) -> None:
    original_commit = _git_init(tmp_path)
    (tmp_path / "README.md").write_text("# Replacement\n", encoding="utf-8")
    replacement_commit = _git_commit_all(tmp_path, "replacement content")
    subprocess.run(["git", "replace", original_commit, replacement_commit], cwd=tmp_path, check=True)
    no_replace_env = {**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"}
    original_tree = subprocess.run(
        ["git", "rev-parse", "--verify", f"{original_commit}^{{tree}}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
        env=no_replace_env,
    ).stdout.strip()

    assert _git_object(tmp_path, f"{original_commit}^{{tree}}") == original_tree
    with detached_delivery_verification_worktree(tmp_path, original_commit) as candidate_root:
        assert (candidate_root / "README.md").read_text(encoding="utf-8") == "# Demo\n"


def test_delivery_verification_skips_unused_security_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = {
        **_config(tmp_path),
        "agents": {"security_reviewer": {"llm": {"provider": "unsupported"}}},
    }
    status = SimpleNamespace(status="done")
    readiness = SimpleNamespace(required=True, ready=True, security_required=False)
    clients: list[object] = []
    captured: dict[str, object] = {}
    expected = object()

    monkeypatch.setattr("core.delivery_progress.get_delivery_status", lambda *args, **kwargs: status)
    monkeypatch.setattr(
        "core.delivery_verification.check_delivery_verification_readiness",
        lambda *args, **kwargs: readiness,
    )
    monkeypatch.setattr(
        "core.llm_client.create_llm_client",
        lambda llm_config: clients.append(llm_config) or _ReadonlyLLM([]),
    )

    def verify(*args, **kwargs):
        captured.update(kwargs)
        return expected

    monkeypatch.setattr("core.delivery_verify.verify_delivery_plan", verify)
    args = argparse.Namespace(
        plan_file="plan.yaml",
        agent_model=None,
        agent_provider=None,
        agent_timeout=None,
    )

    result = _run_delivery_verification(args, config)

    assert result is expected
    assert len(clients) == 1
    assert captured["semantic_reviewer"] is not None
    assert captured["security_reviewer"] is None


def test_verification_validation_runs_configured_pipeline_once(tmp_path: Path, monkeypatch) -> None:
    _git_init(tmp_path)
    _write_plan(tmp_path)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "plan"], cwd=tmp_path, check=True, capture_output=True)
    config = _config(tmp_path)
    config["run_presync"] = True
    config["build"]["checks"] = [{"name": "ruff", "command": "ruff check ."}]
    tool = _BuildTool()
    monkeypatch.setattr(
        "core.delivery_verification_validation.create_build_tool",
        lambda sandbox, root, project_config: tool,
    )

    result = run_delivery_verification_validation(tmp_path, config, reusable=None)

    assert result.passed is True
    assert result.executed is True
    assert result.reused is False
    assert tool.calls == ["presync", "sync", "build", "test", "check:ruff"]
    assert result.to_review_dict(config)["policy"] == {
        "build_tool": "python",
        "run_presync": True,
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "commands": [
            {"phase": "build", "name": "compile", "command": "python -m compileall ."},
            {"phase": "test", "name": "tests", "command": "pytest"},
            {"phase": "check", "name": "ruff", "command": "ruff check ."},
        ],
        "final_checks": [],
    }


def test_verification_validation_runs_presync_without_build(tmp_path: Path, monkeypatch) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "plan"], cwd=tmp_path, check=True, capture_output=True)
    config = _config(tmp_path)
    config.update({"run_presync": True, "run_build": False})
    tool = _BuildTool()
    monkeypatch.setattr(
        "core.delivery_verification_validation.create_build_tool",
        lambda sandbox, root, project_config: tool,
    )

    reusable = reusable_delivery_validation(
        get_delivery_status(plan_path),
        config,
        JsonStateStore(tmp_path / ".sikula" / "state"),
        candidate_tree="unused",
    )
    result = run_delivery_verification_validation(tmp_path, config, reusable=reusable)

    assert reusable is None
    assert result.passed is True
    assert result.executed is True
    assert tool.calls == ["presync"]


def test_verification_validation_uses_disabled_runtime_phase_defaults(tmp_path: Path, monkeypatch) -> None:
    _git_init(tmp_path)
    config = {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "build": {"compile_command": "compile", "test_command": "test"},
    }
    tool = _BuildTool()
    monkeypatch.setattr(
        "core.delivery_verification_validation.create_build_tool",
        lambda sandbox, root, project_config: tool,
    )

    result = run_delivery_verification_validation(tmp_path, config, reusable=None)

    assert result.passed is True
    assert result.executed is False
    assert tool.calls == []
    policy = result.to_review_dict(config)["policy"]
    assert policy["run_build"] is False
    assert policy["run_tests"] is False
    assert policy["run_checks"] is False
    explicit_disabled = {**config, "run_build": False, "run_tests": False, "run_checks": False}
    assert delivery_verification_config_fingerprint(config) == delivery_verification_config_fingerprint(
        explicit_disabled
    )
    build_disabled = {**config, "run_tests": True, "run_checks": True}
    assert delivery_verification_config_fingerprint(config) == delivery_verification_config_fingerprint(build_disabled)


def test_verification_final_checks_without_base_validation_do_not_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-m", "plan"], cwd=tmp_path, check=True, capture_output=True)
    config = _config(tmp_path)
    config.update({"run_presync": False, "run_build": False, "run_tests": False, "run_checks": False})
    config["delivery"] = {
        "verification": {
            "final_checks": [{"name": "contracts", "command": "check contracts"}],
        }
    }
    tool = _BuildTool()
    monkeypatch.setattr(
        "core.delivery_verification_validation.create_build_tool",
        lambda sandbox, root, project_config: tool,
    )

    reusable = reusable_delivery_validation(
        get_delivery_status(plan_path),
        config,
        JsonStateStore(tmp_path / ".sikula" / "state"),
        candidate_tree="unused",
    )
    result = run_delivery_verification_validation(tmp_path, config, reusable=reusable)

    assert reusable is None
    assert result.passed is True
    assert result.reused is False
    assert result.executed is True
    assert tool.calls == ["check:contracts"]


def test_verification_removes_only_injected_environment_files_before_review(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "root"
    worktree = tmp_path / "worktree"
    root.mkdir()
    worktree.mkdir()
    (root / "local.properties").write_text("sdk.dir=/private/sdk\n", encoding="utf-8")
    monkeypatch.setattr(
        "core.delivery_verify.build_tool_class",
        lambda config: SimpleNamespace(env_files=lambda: ["local.properties"]),
    )

    copied = _copy_environment_files(root, worktree, {})

    assert copied == [Path("local.properties")]
    assert (worktree / "local.properties").is_file()

    _remove_copied_environment_files(worktree, copied)

    assert not (worktree / "local.properties").exists()
    assert (root / "local.properties").is_file()


def test_verification_audit_is_owner_only_and_rejects_symlinks(tmp_path: Path) -> None:
    audit_path = tmp_path / "private" / "verification.jsonl"

    _append_audit(audit_path, {"event": "test"}, project_root=tmp_path)

    if os.name != "nt":
        assert stat.S_IMODE(audit_path.stat().st_mode) == 0o600

    target = tmp_path / "external.jsonl"
    target.write_text("original\n", encoding="utf-8")
    audit_path.unlink()
    try:
        audit_path.symlink_to(target)
    except OSError:
        pytest.skip("symlink creation is unavailable")

    with pytest.raises(OSError):
        _append_audit(audit_path, {"event": "must-not-write"}, project_root=tmp_path)

    assert target.read_text(encoding="utf-8") == "original\n"


def test_verification_audit_rejects_symlinked_parent(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-audit-target"
    outside.mkdir()
    linked_parent = tmp_path / "linked"
    try:
        linked_parent.symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink creation is unavailable")

    with pytest.raises(OSError):
        _append_audit(
            linked_parent / "verification.jsonl",
            {"event": "must-not-write"},
            project_root=tmp_path,
        )

    assert not (outside / "verification.jsonl").exists()


def test_verification_validation_rejects_source_visible_mutation(tmp_path: Path, monkeypatch) -> None:
    _git_init(tmp_path)
    tool = _BuildTool(mutate=tmp_path / "README.md")
    monkeypatch.setattr(
        "core.delivery_verification_validation.create_build_tool",
        lambda sandbox, root, project_config: tool,
    )

    result = run_delivery_verification_validation(tmp_path, _config(tmp_path), reusable=None)

    assert result.passed is False
    assert result.stop_code == "delivery_verification.validation_workspace_mutated"


def test_verification_validation_reuses_exact_tree_child_evidence(tmp_path: Path) -> None:
    commit = _git_init(tmp_path)
    plan_path = _write_plan(tmp_path)
    status = get_delivery_status(plan_path)
    config = _config(tmp_path)
    store = JsonStateStore(tmp_path / ".sikula" / "state")
    child = store.create("child")
    child.done = True
    child.result_commit = commit
    child.build_status = "success"
    child.test_status = "success"
    child.check_status = "skipped"
    child.config_snapshot = {
        "project_build_tool": "python",
        "run_presync": False,
        "run_build": True,
        "run_tests": True,
        "run_checks": True,
        "build": config["build"],
    }
    store.save(child)
    done_unit = replace(
        status.units[0],
        status="done",
        child_task_id=child.task_id,
        commit=commit,
    )
    status = replace(status, units=[done_unit], status="done")
    candidate_tree = subprocess.run(
        ["git", "rev-parse", f"{commit}^{{tree}}"],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    result = reusable_delivery_validation(
        status,
        config,
        store,
        candidate_tree=candidate_tree,
    )

    assert result is not None
    assert result.passed is True
    assert result.reused is True
    assert result.reused_child_task_id == child.task_id
