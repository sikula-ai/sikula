from __future__ import annotations

from dataclasses import dataclass, field
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from typing import Any

from core.delivery_obligations import delivery_authority_fragments
from core.delivery_plan import DeliveryPlanCheckResult, DeliveryPlanIssue, is_private_delivery_source_task_path
from core.delivery_progress import DeliveryStatusResult
from core.delivery_verification_model import delivery_verification_covers_obligations
from core.delivery_verification_review import delivery_integration_review_control_example
from core.delivery_verification_validation import delivery_validation_review_policy
from core.worktree import delivery_verification_git_env
from tools.base_tool import Sandbox
from tools.build_factory import build_tool_class
from tools.file_tool import FileTool


DELIVERY_VERIFICATION_JSON_SCHEMA_VERSION = 1
DELIVERY_VERIFICATION_PRIVACY_MODE = "public_metadata"
MAX_DELIVERY_VERIFICATION_SOURCE_BYTES = 128 * 1024
MAX_DELIVERY_VERIFICATION_PLAN_BYTES = 256 * 1024
MAX_DELIVERY_VERIFICATION_ACTIVE_UNITS = 256
MAX_DELIVERY_VERIFICATION_PACKET_BYTES = 512 * 1024
_DELIVERY_VERIFICATION_PROMPT_OVERHEAD_BYTES = 64 * 1024
_SUPPORTED_BUILD_TOOLS = frozenset({"cargo", "gradle-android", "gradle-jvm", "maven", "node", "python", "xcodebuild"})
_SUPPORTED_PROVIDERS = frozenset({"antigravity", "claude", "codex", "gemini", "opencode"})
_SECURITY_SENSITIVE_RISK_TAGS = frozenset(
    {
        "auth_permissions",
        "execution_boundary",
        "external_execution_boundary",
        "privacy",
        "security_boundary",
    }
)


def delivery_verification_allowed_read_paths(project_config: dict[str, Any]) -> list[str]:
    """Return validated project-relative read roots for integration reviewers."""

    sandbox = project_config.get("sandbox", {})
    if not isinstance(sandbox, dict):
        raise ValueError("sandbox configuration must be an object")
    configured = sandbox.get("allowed_read_paths", ["."])
    if not isinstance(configured, list):
        raise ValueError("sandbox.allowed_read_paths must be a list")
    paths: list[str] = []
    for value in configured:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("sandbox.allowed_read_paths entries must be non-empty strings")
        path = Path(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("sandbox.allowed_read_paths entries must stay within the project")
        paths.append(value)
    return paths


def delivery_verification_provider_read_scope_supported(allowed_read_paths: list[str]) -> bool:
    """Return whether autonomous reviewers may see exactly the configured read authority."""

    return any(Path(value) == Path(".") for value in allowed_read_paths)


def delivery_verification_source_task_is_private(
    root: Path,
    source_path: Path,
    source_metadata: str,
    project_config: dict[str, Any],
) -> bool:
    """Return whether a delivery source path is outside the provider-safe authority boundary."""

    if is_private_delivery_source_task_path(source_metadata):
        return True
    tasks = project_config.get("tasks", {})
    if not isinstance(tasks, dict):
        tasks = {}
    private_roots: list[Path] = []
    try:
        resolved_source = source_path.resolve(strict=True)
        resolved_root = root.resolve(strict=True)
        resolved_source.relative_to(resolved_root)
        for key, default in (
            ("state_dir", ".sikula/state"),
            ("contract_report_dir", ".sikula/contract-reports"),
        ):
            raw = tasks.get(key, default)
            if not isinstance(raw, str):
                raw = default
            configured = Path(raw)
            private_roots.append(
                configured.resolve(strict=False)
                if configured.is_absolute()
                else (resolved_root / configured).resolve(strict=False)
            )
        private_roots.extend(
            (resolved_root / relative).resolve(strict=False)
            for relative in (".git", ".sikula/state", ".sikula/worktrees", ".sikula/contract-reports")
        )
        for relative in build_tool_class(project_config).env_files():
            env_path = Path(relative)
            if env_path.is_absolute() or ".." in env_path.parts:
                return True
            if resolved_source == (resolved_root / env_path).resolve(strict=False):
                return True
    except (OSError, RuntimeError, ValueError):
        return True
    for private_root in private_roots:
        try:
            resolved_source.relative_to(private_root)
            return True
        except ValueError:
            continue
    return False


@dataclass(frozen=True)
class DeliveryVerificationReadiness:
    required: bool
    ready: bool
    security_required: bool
    source_bytes: int
    plan_bytes: int
    packet_bytes: int
    active_unit_count: int
    errors: list[DeliveryPlanIssue] = field(default_factory=list)
    warnings: list[DeliveryPlanIssue] = field(default_factory=list)


@dataclass(frozen=True)
class DeliveryVerificationIdentity:
    gate_id: str
    candidate_commit: str
    candidate_tree: str
    source_fingerprint: str
    plan_fingerprint: str
    completed_scope_fingerprint: str
    config_fingerprint: str
    policy_fingerprint: str


def delivery_verification_security_required(status: DeliveryPlanCheckResult | DeliveryStatusResult) -> bool:
    plan = status.plan
    if plan is None:
        return False
    if any(constraint.kind == "security_boundary" for constraint in plan.constraints):
        return True
    return any(tag in _SECURITY_SENSITIVE_RISK_TAGS for unit in plan.units for tag in unit.risk_tags)


def check_delivery_verification_readiness(
    status: DeliveryPlanCheckResult | DeliveryStatusResult,
    project_config: dict[str, Any],
) -> DeliveryVerificationReadiness:
    plan = status.plan
    required = bool(plan and getattr(plan, "requires_final_verification", False))
    if not required:
        return DeliveryVerificationReadiness(
            required=False,
            ready=status.valid,
            security_required=False,
            source_bytes=0,
            plan_bytes=0,
            packet_bytes=0,
            active_unit_count=0,
            errors=list(status.errors),
            warnings=list(status.warnings),
        )

    errors = list(status.errors)
    warnings = list(status.warnings)
    security_required = delivery_verification_security_required(status)
    source_bytes = 0
    source_prompt_bytes = 0
    plan_bytes = 0
    packet_policy_bytes = 0
    active_unit_count = len([unit for unit in plan.units if not unit.superseded]) if plan else 0
    root = Path(status.project_root).resolve() if status.project_root else None
    try:
        allowed_read_paths = delivery_verification_allowed_read_paths(project_config)
    except ValueError:
        allowed_read_paths = []
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.config_invalid",
                "Delivery verification sandbox.allowed_read_paths configuration is invalid.",
            )
        )
    else:
        if not delivery_verification_provider_read_scope_supported(allowed_read_paths):
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_verification.read_scope_unsupported",
                    "Final integration review currently requires sandbox.allowed_read_paths to include '.'.",
                    "sandbox.allowed_read_paths",
                )
            )

    if root is None or plan is None:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.project_unavailable",
                "Delivery verification requires a valid project root and plan.",
            )
        )
    else:
        if len(plan.repositories) != 1 or plan.repositories[0].root != ".":
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_verification.multirepository_unsupported",
                    "This verification milestone supports one repository rooted at the delivery project.",
                    "repositories",
                )
            )
        if plan.source_task is None:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_verification.source_task_missing",
                    "Delivery verification requires an immutable source task.",
                    "source_task",
                )
            )
        else:
            source_path = root / plan.source_task.path
            if not source_path.is_file():
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "delivery_verification.source_task_unavailable",
                        "Delivery verification could not read the authoritative source task.",
                        "source_task.path",
                    )
                )
            elif delivery_verification_source_task_is_private(
                root,
                source_path,
                plan.source_task.path,
                project_config,
            ):
                errors.append(
                    DeliveryPlanIssue(
                        "error",
                        "delivery_verification.source_private",
                        "Delivery verification cannot send a private source task to a reviewer provider.",
                        "source_task.path",
                    )
                )
            else:
                try:
                    source_data = source_path.read_bytes()
                    source_bytes = len(source_data)
                    source_text = source_data.decode("utf-8")
                    source_prompt_bytes = len(
                        json.dumps(
                            [fragment.to_prompt_dict() for fragment in delivery_authority_fragments(source_text)],
                            indent=2,
                            sort_keys=True,
                            ensure_ascii=True,
                        ).encode("utf-8")
                    )
                except (OSError, UnicodeError):
                    errors.append(
                        DeliveryPlanIssue(
                            "error",
                            "delivery_verification.source_task_unavailable",
                            "Delivery verification could not read the authoritative source task.",
                            "source_task.path",
                        )
                    )
        if status.plan_bytes is None or status.plan_fingerprint is None:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_verification.plan_unavailable",
                    "Delivery verification could not bind the parsed delivery plan bytes.",
                )
            )
        else:
            plan_bytes = status.plan_bytes
        packet_policy_bytes = _verification_policy_payload_bytes(
            status,
            project_config,
            root=root,
            security_required=security_required,
            allowed_read_paths=allowed_read_paths,
            errors=errors,
        )

    if source_bytes > MAX_DELIVERY_VERIFICATION_SOURCE_BYTES:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.hierarchy_required",
                "The authoritative source exceeds the bounded final-gate limit; hierarchical verification is required.",
                "source_task",
            )
        )
    if plan_bytes > MAX_DELIVERY_VERIFICATION_PLAN_BYTES:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.hierarchy_required",
                "The delivery plan exceeds the bounded final-gate limit; hierarchical verification is required.",
            )
        )
    if active_unit_count > MAX_DELIVERY_VERIFICATION_ACTIVE_UNITS:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.hierarchy_required",
                "The delivery plan has too many active units for one final gate; hierarchical verification is required.",
                "units",
            )
        )

    plan_context_bytes = (
        len(
            json.dumps(
                delivery_verification_plan_context(status),
                indent=2,
                sort_keys=True,
                ensure_ascii=True,
            ).encode("utf-8")
        )
        if plan is not None
        else 0
    )
    control_object_bytes = len(
        delivery_integration_review_control_example(
            {obligation.id for obligation in plan.obligations} if plan is not None else set()
        ).encode("utf-8")
    )
    packet_bytes = (
        source_prompt_bytes
        + plan_context_bytes
        + packet_policy_bytes
        + control_object_bytes
        + _DELIVERY_VERIFICATION_PROMPT_OVERHEAD_BYTES
    )
    if packet_bytes > MAX_DELIVERY_VERIFICATION_PACKET_BYTES:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.hierarchy_required",
                "The verification packet exceeds the bounded final-gate limit; hierarchical verification is required.",
            )
        )

    build_tool = str(project_config.get("project", {}).get("build_tool") or "gradle-android")
    if build_tool not in _SUPPORTED_BUILD_TOOLS:
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.build_tool_unsupported",
                "The configured build tool is unsupported by delivery verification.",
            )
        )
    for agent_name in ("reviewer", "security_reviewer"):
        if agent_name == "security_reviewer" and not security_required:
            continue
        provider = _effective_provider(project_config, agent_name)
        if provider not in _SUPPORTED_PROVIDERS:
            errors.append(
                DeliveryPlanIssue(
                    "error",
                    "delivery_verification.provider_unsupported",
                    f"The configured {agent_name} provider is unsupported by delivery verification.",
                )
            )

    delivery_config = project_config.get("delivery", {})
    verification_config = delivery_config.get("verification", {}) if isinstance(delivery_config, dict) else None
    final_checks = verification_config.get("final_checks", []) if isinstance(verification_config, dict) else None
    if not isinstance(final_checks, list) or any(
        not isinstance(check, dict)
        or not isinstance(check.get("command"), str)
        or not check["command"].strip()
        or ("name" in check and (not isinstance(check["name"], str) or not check["name"].strip()))
        for check in (final_checks if isinstance(final_checks, list) else [])
    ):
        errors.append(
            DeliveryPlanIssue(
                "error",
                "delivery_verification.config_invalid",
                "Delivery verification final_checks configuration is invalid.",
            )
        )

    return DeliveryVerificationReadiness(
        required=True,
        ready=not errors,
        security_required=security_required,
        source_bytes=source_bytes,
        plan_bytes=plan_bytes,
        packet_bytes=packet_bytes,
        active_unit_count=active_unit_count,
        errors=errors,
        warnings=warnings,
    )


def with_delivery_verification_readiness(
    status: DeliveryStatusResult,
    project_config: dict[str, Any],
) -> DeliveryStatusResult:
    if not status.valid:
        return status
    readiness = check_delivery_verification_readiness(status, project_config)
    if not readiness.required:
        return status
    if readiness.ready:
        verification = status.verification
        if verification is None:
            return status
        try:
            identity = build_delivery_verification_identity(
                status,
                project_config,
                candidate_commit=verification.candidate_commit,
            )
        except (OSError, RuntimeError, ValueError):
            identity = None
        if (
            identity is not None
            and status.assembly_status == "ready"
            and status.assembled_commit == verification.candidate_commit
            and _git_ref_matches(
                Path(status.project_root or "."),
                f"refs/heads/{status.plan.final_branch}",
                verification.candidate_commit,
            )
            and _verification_record_matches_identity(
                verification,
                identity,
                obligation_count=len(status.plan.obligations),
            )
        ):
            return status
        return replace(
            status,
            verification_status="stale",
            next_action="verify the assembled delivery with delivery verify",
        )
    return replace(
        status,
        status="invalid",
        errors=readiness.errors,
        warnings=readiness.warnings,
        next_action="resolve delivery verification readiness errors before running units",
        verification_status="blocked",
    )


def build_delivery_verification_identity(
    status: DeliveryStatusResult,
    project_config: dict[str, Any],
    *,
    candidate_commit: str,
) -> DeliveryVerificationIdentity:
    if status.plan is None or status.plan.source_task is None or status.project_root is None:
        raise ValueError("delivery verification identity requires a valid authoritative plan")
    root = Path(status.project_root).resolve()
    candidate_tree = _git_object(root, f"{candidate_commit}^{{tree}}")
    resolved_commit = _git_object(root, f"{candidate_commit}^{{commit}}")
    if candidate_tree is None or resolved_commit is None:
        raise ValueError("delivery verification candidate cannot be resolved")

    if status.plan_fingerprint is None:
        raise ValueError("delivery verification identity requires bound plan bytes")
    plan_fingerprint = status.plan_fingerprint
    completed_scope_fingerprint = _fingerprint(
        [
            {
                "unit_id": unit.id,
                "commit": unit.commit,
                "handoff_fingerprint": unit.handoff_fingerprint,
            }
            for unit in status.units
            if unit.status == "done" and unit.status != "superseded"
        ]
    )
    config_fingerprint = delivery_verification_config_fingerprint(
        project_config,
        security_required=delivery_verification_security_required(status),
    )
    policy_fingerprint = _fingerprint(status.plan.verification.to_dict() if status.plan.verification else {})
    source_fingerprint = status.plan.source_task.sha256
    gate_id = _fingerprint(
        {
            "candidate_commit": resolved_commit,
            "candidate_tree": candidate_tree,
            "source_fingerprint": source_fingerprint,
            "plan_fingerprint": plan_fingerprint,
            "completed_scope_fingerprint": completed_scope_fingerprint,
            "config_fingerprint": config_fingerprint,
            "policy_fingerprint": policy_fingerprint,
        }
    )
    return DeliveryVerificationIdentity(
        gate_id=gate_id,
        candidate_commit=resolved_commit,
        candidate_tree=candidate_tree,
        source_fingerprint=source_fingerprint,
        plan_fingerprint=plan_fingerprint,
        completed_scope_fingerprint=completed_scope_fingerprint,
        config_fingerprint=config_fingerprint,
        policy_fingerprint=policy_fingerprint,
    )


def delivery_verification_config_fingerprint(
    project_config: dict[str, Any],
    *,
    security_required: bool = False,
) -> str:
    project = dict(project_config.get("project", {}))
    project.pop("root_path", None)
    review_policy = {
        "reviewer_extra_rules": str(project_config.get("reviewer", {}).get("extra_rules") or ""),
        "reviewer_llm": _effective_reviewer_llm_config(project_config, "reviewer"),
        "allowed_read_paths": delivery_verification_allowed_read_paths(project_config),
    }
    if security_required:
        review_policy.update(
            {
                "security_context": str(project_config.get("security", {}).get("context") or ""),
                "security_reviewer_extra_rules": str(
                    project_config.get("security_reviewer", {}).get("extra_rules") or ""
                ),
                "security_reviewer_llm": _effective_reviewer_llm_config(project_config, "security_reviewer"),
            }
        )
    delivery = project_config.get("delivery", {})
    verification = delivery.get("verification", {}) if isinstance(delivery, dict) else {}
    run_build = bool(project_config.get("run_build", False))
    effective = {
        "config_source_fingerprint": project_config.get("_config_source_fingerprint"),
        "project": project,
        "build": project_config.get("build", {}),
        "run_presync": bool(project_config.get("run_presync", False)),
        "run_build": run_build,
        "run_tests": run_build and bool(project_config.get("run_tests", False)),
        "run_checks": run_build and bool(project_config.get("run_checks", False)),
        "verification": verification if isinstance(verification, dict) else {},
        "review_policy": review_policy,
    }
    return _fingerprint(effective)


def _effective_reviewer_llm_config(project_config: dict[str, Any], agent_name: str) -> dict[str, Any]:
    base = project_config.get("llm", {})
    if not isinstance(base, dict):
        base = {}
    agents = project_config.get("agents", {})
    agent = agents.get(agent_name, {}) if isinstance(agents, dict) else {}
    agent_llm = agent.get("llm", {}) if isinstance(agent, dict) else {}
    if not isinstance(agent_llm, dict):
        agent_llm = {}
    defaults = {
        "provider": "codex",
        "model": "gpt-5.3-codex",
        "max_tokens": 16000,
        "temperature": 0.0,
    }
    return {key: agent_llm[key] if key in agent_llm else base.get(key, default) for key, default in defaults.items()}


def _effective_provider(project_config: dict[str, Any], agent_name: str) -> str:
    agent = project_config.get("agents", {}).get(agent_name, {})
    agent_llm = agent.get("llm", {}) if isinstance(agent, dict) else {}
    base = project_config.get("llm", {})
    return str(agent_llm.get("provider") or base.get("provider") or "codex").strip()


def delivery_verification_plan_context(status: DeliveryPlanCheckResult | DeliveryStatusResult) -> dict[str, Any]:
    plan = status.plan
    if plan is None:
        return {}
    return {
        "plan_id": plan.plan_id,
        "title": plan.title,
        "units": [unit.to_authoring_dict() for unit in plan.units if not unit.superseded],
        "constraints": [constraint.to_dict() for constraint in plan.constraints],
        "obligations": [obligation.to_context_dict() for obligation in plan.obligations],
        "source_accounting": [record.to_dict() for record in plan.source_accounting]
        if plan.source_accounting is not None
        else None,
        "components": [component.to_dict() for component in plan.components],
    }


def delivery_verification_prompt_is_bounded(prompt: str) -> bool:
    return len(prompt.encode("utf-8")) <= MAX_DELIVERY_VERIFICATION_PACKET_BYTES


def _verification_policy_payload_bytes(
    status: DeliveryPlanCheckResult | DeliveryStatusResult,
    project_config: dict[str, Any],
    *,
    root: Path,
    security_required: bool,
    allowed_read_paths: list[str],
    errors: list[DeliveryPlanIssue],
) -> int:
    review_authority = {
        "validation_policy": delivery_validation_review_policy(project_config),
        "allowed_read_paths": allowed_read_paths,
    }
    payload_bytes = len(
        json.dumps(review_authority, indent=2, sort_keys=True, ensure_ascii=True, default=str).encode("utf-8")
    )
    payload_bytes += _review_rules_payload_bytes(
        root,
        project_config,
        "reviewer",
        allowed_read_paths,
        errors,
    )
    if security_required:
        security_context = str(project_config.get("security", {}).get("context") or "").strip()
        if security_context:
            payload_bytes += len(("\n\nProject security context:\n" + security_context).encode("utf-8"))
        payload_bytes += _review_rules_payload_bytes(
            root,
            project_config,
            "security_reviewer",
            allowed_read_paths,
            errors,
        )
    return payload_bytes


def _review_rules_payload_bytes(
    root: Path,
    project_config: dict[str, Any],
    agent_name: str,
    allowed_read_paths: list[str],
    errors: list[DeliveryPlanIssue],
) -> int:
    agent_config = project_config.get(agent_name, {})
    configured = agent_config.get("extra_rules") if isinstance(agent_config, dict) else None
    if not configured:
        return 0
    if not isinstance(configured, str):
        _append_rules_unavailable(errors, agent_name)
        return 0
    relative = Path(configured)
    if relative.is_absolute() or ".." in relative.parts:
        _append_rules_unavailable(errors, agent_name)
        return 0
    file_tool = FileTool(
        Sandbox(root, allowed_write_paths=[], allowed_read_paths=allowed_read_paths),
        root,
    )
    result = file_tool.read(configured)
    if not result.success:
        _append_rules_unavailable(errors, agent_name)
        return 0
    content = result.output.strip()
    if not content:
        return 0
    formatted = (
        "\n\n## Project-specific rules\n\n"
        "The following rules are specific to this project and take priority over any conflicting instructions above.\n\n"
        + content
    )
    return len(formatted.encode("utf-8"))


def _append_rules_unavailable(errors: list[DeliveryPlanIssue], agent_name: str) -> None:
    issue = DeliveryPlanIssue(
        "error",
        "delivery_verification.review_rules_unavailable",
        f"Configured {agent_name} integration review rules are unavailable.",
    )
    if issue not in errors:
        errors.append(issue)


def _verification_record_matches_identity(
    record: Any,
    identity: DeliveryVerificationIdentity,
    *,
    obligation_count: int,
) -> bool:
    return delivery_verification_covers_obligations(record, obligation_count) and all(
        getattr(record, key, None) == getattr(identity, key)
        for key in (
            "gate_id",
            "candidate_commit",
            "candidate_tree",
            "source_fingerprint",
            "plan_fingerprint",
            "completed_scope_fingerprint",
            "config_fingerprint",
            "policy_fingerprint",
        )
    )


def _git_object(root: Path, ref: str) -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "--verify", ref],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=delivery_verification_git_env(),
    )
    value = result.stdout.strip().splitlines()[0] if result.returncode == 0 and result.stdout.strip() else None
    return value


def _git_ref_matches(root: Path, ref: str, commit: str) -> bool:
    return _git_object(root, f"{ref}^{{commit}}") == commit


def _fingerprint(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()
