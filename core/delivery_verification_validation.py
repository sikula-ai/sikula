from __future__ import annotations

from dataclasses import dataclass, field
from hashlib import sha256
import json
from pathlib import Path
import subprocess
import time
from typing import Any

from core.delivery_progress import DeliveryStatusResult
from core.state import StateStore
from core.validation_coverage import effective_validation_commands
from core.worktree import current_worktree_changes, delivery_verification_git_env
from tools.base_tool import Sandbox, ToolResult
from tools.build_factory import create_build_tool


@dataclass(frozen=True)
class DeliveryVerificationValidationRecord:
    phase: str
    name: str
    status: str
    source: str
    policy_fingerprint: str
    elapsed_s: float = 0.0
    error_excerpt: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "phase": self.phase,
            "name": self.name,
            "status": self.status,
            "source": self.source,
            "policy_fingerprint": self.policy_fingerprint,
            "elapsed_s": round(self.elapsed_s, 3),
        }
        if self.error_excerpt:
            data["error_excerpt"] = self.error_excerpt
        return data


@dataclass(frozen=True)
class DeliveryVerificationValidationResult:
    passed: bool
    reused: bool
    executed: bool
    records: list[DeliveryVerificationValidationRecord] = field(default_factory=list)
    reused_child_task_id: str | None = None
    stop_code: str | None = None

    def to_review_dict(self, project_config: dict[str, Any], *, project_root: Path | None = None) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reused": self.reused,
            "executed": self.executed,
            "policy": delivery_validation_review_policy(project_config, project_root=project_root),
            "phases": [
                {
                    "phase": record.phase,
                    "name": record.name,
                    "status": record.status,
                    "source": record.source,
                }
                for record in self.records
            ],
        }


def delivery_validation_review_policy(
    project_config: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> dict[str, Any]:
    """Return the bounded effective validation authority shown to integration reviewers."""

    effective_config = project_config
    if project_root is not None:
        effective_config = {
            **project_config,
            "project": {
                **project_config.get("project", {}),
                "root_path": str(project_root),
            },
        }
    run_build = bool(effective_config.get("run_build", False))
    run_tests = run_build and bool(effective_config.get("run_tests", False))
    run_checks = run_build and bool(effective_config.get("run_checks", False))
    commands = [
        command
        for command in effective_validation_commands(
            effective_config,
            run_build=run_build,
            run_tests=run_tests,
            run_checks=run_checks,
        )
        if command["phase"] != "check_autofix"
    ]
    configured_final_checks = _configured_final_checks(effective_config)
    final_checks = configured_final_checks if configured_final_checks is not None else []
    return {
        "build_tool": effective_config.get("project", {}).get("build_tool", "gradle-android"),
        "run_presync": bool(effective_config.get("run_presync", False)),
        "run_build": run_build,
        "run_tests": run_tests,
        "run_checks": run_checks,
        "commands": commands,
        "final_checks": [
            {
                "name": str(check.get("name") or f"final-check-{index}"),
                "command": check["command"],
            }
            for index, check in enumerate(final_checks, start=1)
            if isinstance(check, dict) and isinstance(check.get("command"), str)
        ],
    }


def delivery_validation_policy_fingerprint_from_config(project_config: dict[str, Any]) -> str:
    return _fingerprint(_validation_policy_from_config(project_config))


def reusable_delivery_validation(
    status: DeliveryStatusResult,
    project_config: dict[str, Any],
    state_store: StateStore,
    *,
    candidate_tree: str,
) -> DeliveryVerificationValidationResult | None:
    policy = _validation_policy_from_config(project_config)
    if not policy["run_build"] and not policy["run_presync"]:
        return None
    policy_fingerprint = _fingerprint(policy)
    for unit in reversed(status.units):
        if unit.status != "done" or not unit.child_task_id:
            continue
        try:
            state = state_store.load(unit.child_task_id)
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        if state is None or not state.done or state.failed or not state.result_commit:
            continue
        if _git_tree(Path(status.project_root or "."), state.result_commit) != candidate_tree:
            continue
        snapshot = _last_config_snapshot(state)
        if _validation_policy_from_snapshot(snapshot) != policy:
            continue
        if state.build_status != "success":
            continue
        if policy["run_tests"] and state.test_status != "success":
            continue
        if policy["run_checks"] and policy["checks"] and state.check_status != "success":
            continue
        if any(
            isinstance(record, dict) and record.get("status") in {"blocked", "cleanup_failed"}
            for record in state.validation_artifact_records
        ):
            continue
        records = [
            DeliveryVerificationValidationRecord(
                phase="build",
                name="compile",
                status="passed",
                source="reused",
                policy_fingerprint=policy_fingerprint,
            )
        ]
        if policy["run_tests"]:
            records.append(
                DeliveryVerificationValidationRecord(
                    phase="test",
                    name="tests",
                    status="passed",
                    source="reused",
                    policy_fingerprint=policy_fingerprint,
                )
            )
        if policy["run_checks"]:
            for check in policy["checks"]:
                records.append(
                    DeliveryVerificationValidationRecord(
                        phase="check",
                        name=check["name"],
                        status="passed",
                        source="reused",
                        policy_fingerprint=policy_fingerprint,
                    )
                )
        return DeliveryVerificationValidationResult(
            passed=True,
            reused=True,
            executed=False,
            records=records,
            reused_child_task_id=unit.child_task_id,
        )
    return None


def run_delivery_verification_validation(
    worktree: Path,
    project_config: dict[str, Any],
    *,
    reusable: DeliveryVerificationValidationResult | None,
) -> DeliveryVerificationValidationResult:
    policy = _validation_policy_from_config(project_config)
    policy_fingerprint = _fingerprint(policy)
    records = list(reusable.records) if reusable else []
    executed = False
    sandbox = Sandbox(worktree, allowed_write_paths=["."], allowed_read_paths=["."])
    tool = create_build_tool(sandbox, worktree, project_config)

    baseline_head = _git_head(worktree)
    git_env = delivery_verification_git_env()
    staged, unstaged, untracked, status_error = current_worktree_changes(worktree, git_env=git_env)
    if baseline_head is None or status_error or staged or unstaged or untracked:
        return DeliveryVerificationValidationResult(
            passed=False,
            reused=bool(reusable),
            executed=False,
            records=records,
            stop_code="delivery_verification.validation_workspace_invalid",
        )

    if reusable is None:
        if policy["run_presync"]:
            executed = True
            record, result = _run_phase("sync", "sync", policy_fingerprint, tool.generate_sources)
            records.append(record)
            if not result.success:
                return _failed_validation(records, reused=False, executed=True, code="validation_failed")
        if policy["run_build"]:
            executed = True
            record, result = _run_phase("sync", "sync", policy_fingerprint, tool.sync)
            records.append(record)
            if not result.success:
                return _failed_validation(records, reused=False, executed=True, code="validation_failed")
            record, result = _run_phase("build", "compile", policy_fingerprint, tool.compile_check)
            records.append(record)
            if not result.success:
                return _failed_validation(records, reused=False, executed=True, code="validation_failed")
            if policy["run_tests"]:
                record, result = _run_phase("test", "tests", policy_fingerprint, tool.run_tests)
                records.append(record)
                if not result.success:
                    return _failed_validation(records, reused=False, executed=True, code="validation_failed")
            if policy["run_checks"]:
                raw_checks = [
                    check
                    for check in project_config.get("build", {}).get("checks") or []
                    if isinstance(check, dict) and check.get("command")
                ]
                for check, raw_check in zip(policy["checks"], raw_checks):
                    record, result = _run_phase(
                        "check",
                        check["name"],
                        policy_fingerprint,
                        lambda raw_check=raw_check, check=check: tool.run_check(check["name"], raw_check),
                    )
                    records.append(record)
                    if not result.success:
                        return _failed_validation(records, reused=False, executed=True, code="validation_failed")

    configured_final_checks = _configured_final_checks(project_config)
    if configured_final_checks is None:
        return _failed_validation(
            records,
            reused=bool(reusable),
            executed=executed,
            code="validation_policy_invalid",
        )
    final_checks = configured_final_checks
    if reusable is not None and final_checks:
        executed = True
        record, result = _run_phase("sync", "final-sync", policy_fingerprint, tool.sync)
        records.append(record)
        if not result.success:
            return _failed_validation(
                records,
                reused=True,
                executed=True,
                code="validation_failed",
            )
    for index, check in enumerate(final_checks, start=1):
        if not isinstance(check, dict) or not isinstance(check.get("command"), str):
            return _failed_validation(
                records,
                reused=bool(reusable),
                executed=executed,
                code="validation_policy_invalid",
            )
        executed = True
        name = str(check.get("name") or f"final-check-{index}")
        record, result = _run_phase(
            "final_check",
            name,
            policy_fingerprint,
            lambda check=check, name=name: tool.run_check(name, check),
        )
        records.append(record)
        if not result.success:
            return _failed_validation(
                records,
                reused=bool(reusable),
                executed=True,
                code="validation_failed",
            )

    if _git_head(worktree) != baseline_head:
        return _failed_validation(
            records,
            reused=bool(reusable),
            executed=executed,
            code="validation_ref_changed",
        )
    staged, unstaged, untracked, status_error = current_worktree_changes(worktree, git_env=git_env)
    if status_error or staged or unstaged or untracked:
        return _failed_validation(
            records,
            reused=bool(reusable),
            executed=executed,
            code="validation_workspace_mutated",
        )
    return DeliveryVerificationValidationResult(
        passed=True,
        reused=bool(reusable),
        executed=executed,
        records=records,
        reused_child_task_id=reusable.reused_child_task_id if reusable else None,
    )


def _run_phase(
    phase: str, name: str, policy_fingerprint: str, operation
) -> tuple[DeliveryVerificationValidationRecord, ToolResult]:
    started = time.perf_counter()
    try:
        result = operation()
    except Exception as exc:
        result = ToolResult(success=False, output="", error=type(exc).__name__)
    elapsed = time.perf_counter() - started
    record = DeliveryVerificationValidationRecord(
        phase=phase,
        name=name,
        status="passed" if result.success else "failed",
        source="executed",
        policy_fingerprint=policy_fingerprint,
        elapsed_s=elapsed,
        error_excerpt=_bounded_error(result.error or result.output) if not result.success else None,
    )
    return record, result


def _failed_validation(
    records: list[DeliveryVerificationValidationRecord],
    *,
    reused: bool,
    executed: bool,
    code: str,
) -> DeliveryVerificationValidationResult:
    return DeliveryVerificationValidationResult(
        passed=False,
        reused=reused,
        executed=executed,
        records=records,
        stop_code=f"delivery_verification.{code}",
    )


def _validation_policy_from_config(project_config: dict[str, Any]) -> dict[str, Any]:
    build = project_config.get("build", {})
    run_build = bool(project_config.get("run_build", False))
    return {
        "build_tool": project_config.get("project", {}).get("build_tool", "gradle-android"),
        "run_presync": bool(project_config.get("run_presync", False)),
        "run_build": run_build,
        "run_tests": run_build and bool(project_config.get("run_tests", False)),
        "run_checks": run_build and bool(project_config.get("run_checks", False)),
        "build": build,
        "checks": [
            {"name": str(check.get("name") or f"check-{index}")}
            for index, check in enumerate(build.get("checks") or [], start=1)
            if isinstance(check, dict) and check.get("command")
        ],
    }


def _configured_final_checks(project_config: dict[str, Any]) -> list[Any] | None:
    delivery = project_config.get("delivery", {})
    if not isinstance(delivery, dict):
        return None
    verification = delivery.get("verification", {})
    if not isinstance(verification, dict):
        return None
    final_checks = verification.get("final_checks", [])
    return final_checks if isinstance(final_checks, list) else None


def _validation_policy_from_snapshot(snapshot: Any) -> dict[str, Any] | None:
    if not isinstance(snapshot, dict) or "project_build_tool" not in snapshot:
        return None
    run_build = bool(snapshot.get("run_build", False))
    return {
        "build_tool": snapshot["project_build_tool"],
        "run_presync": bool(snapshot.get("run_presync", False)),
        "run_build": run_build,
        "run_tests": run_build and bool(snapshot.get("run_tests", False)),
        "run_checks": run_build and bool(snapshot.get("run_checks", False)),
        "build": snapshot.get("build", {}),
        "checks": [
            {"name": str(check.get("name") or f"check-{index}")}
            for index, check in enumerate(snapshot.get("build", {}).get("checks") or [], start=1)
            if isinstance(check, dict) and check.get("command")
        ],
    }


def _last_config_snapshot(state: Any) -> Any:
    for record in reversed(state.run_invocation_records or []):
        if isinstance(record, dict) and isinstance(record.get("config_snapshot"), dict):
            return record["config_snapshot"]
    return state.config_snapshot


def _git_tree(root: Path, commit: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{tree}}"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=delivery_verification_git_env(),
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _git_head(root: Path) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=delivery_verification_git_env(),
    )
    return result.stdout.strip() if result.returncode == 0 and result.stdout.strip() else None


def _bounded_error(value: str, limit: int = 4000) -> str:
    return value.strip().replace("\x00", "")[:limit]


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()
