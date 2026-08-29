"""Provider-independent mutation auditing for delivery-unit write scopes."""

from __future__ import annotations

import fnmatch
import logging
import os
import posixpath
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from core.delivery_public_metadata import sanitize_delivery_public_metadata
from core.state import DELIVERY_STOP_UNIT_SCOPE_VIOLATION, StateStore, TaskState
from core.validation_artifacts import (
    DeliveryScopeGitBinding,
    DeliveryScopeSnapshotError,
    delivery_scope_git_binding,
    deserialize_delivery_scope_snapshot,
    detect_validation_artifacts,
    serialize_delivery_scope_snapshot,
    snapshot_delivery_scope_files,
)
from tools.base_tool import BuildTool

log = logging.getLogger(__name__)

DELIVERY_SCOPE_AUDIT_SNAPSHOT = "delivery_scope_audit_before"
DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT = "delivery_scope_attempt_before"
DELIVERY_SCOPE_AUDIT_PENDING_SCHEMA_VERSION = 8
DELIVERY_SCOPE_AUDIT_PATH_LIMIT = 100
DELIVERY_SCOPE_VIOLATION_CODE = DELIVERY_STOP_UNIT_SCOPE_VIOLATION
DELIVERY_SCOPE_TOOL_ACTORS = frozenset(
    {
        "tool:check_autofix",
        "tool:presync",
        "tool:sync",
    }
)

_TEST_PATH_MARKERS = {
    "__tests__",
    "androidtest",
    "spec",
    "specs",
    "test",
    "testfixtures",
    "tests",
    "unittest",
    "unittests",
}
_TEST_FILE_PREFIXES = ("test_", "test-")
_TEST_FILE_SUFFIXES = (
    ".spec.js",
    ".spec.jsx",
    ".spec.ts",
    ".spec.tsx",
    ".test.js",
    ".test.jsx",
    ".test.ts",
    ".test.tsx",
    "_spec.py",
    "_test.py",
    "_tests.py",
    "Spec.java",
    "Spec.kt",
    "Spec.swift",
    "Test.java",
    "Test.kt",
    "Test.swift",
    "Tests.java",
    "Tests.kt",
    "Tests.swift",
)


@dataclass(frozen=True)
class DeliveryScopeAuditPolicy:
    root: Path
    project_root: Path
    project_prefix: str
    production_roots: tuple[tuple[str, bool], ...]
    resolved_production_roots: tuple[tuple[str, bool], ...]
    active_test_write_paths: tuple[str, ...]
    resolved_test_write_paths: tuple[str, ...]
    agent: str


class DeliveryScopeProviderAttemptStopped(RuntimeError):
    """Stop provider retries after a fail-closed delivery scope audit."""


class DeliveryScopeToolMutationStopped(RuntimeError):
    """Stop a deterministic tool phase after a fail-closed delivery scope audit."""


def delivery_scope_audit_recovery_required(state: TaskState, store: StateStore) -> bool:
    """Return whether resume must recover audit evidence before changing scope."""
    if state.delivery_scope_audit_pending is not None:
        return True
    try:
        return any(
            store.load_text_snapshot(state.task_id, name) is not None
            for name in (DELIVERY_SCOPE_AUDIT_SNAPSHOT, DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT)
        )
    except (OSError, TypeError, ValueError):
        return True


def _normalize_artifact_path(path: str) -> str:
    normalized = path.replace("\\", "/") if os.name == "nt" else path
    return normalized.strip("/")


def _native_scope_path(path: object) -> str:
    raw = str(path)
    return raw.replace("\\", "/") if os.name == "nt" else raw


def _path_matches_pattern(path: str, pattern: str) -> bool:
    normalized_path = _normalize_artifact_path(path)
    raw_pattern = _native_scope_path(pattern).strip()
    directory_pattern = raw_pattern.endswith("/")
    normalized_pattern = raw_pattern.strip("/")
    if not normalized_path or not normalized_pattern:
        return False
    if directory_pattern:
        return normalized_path == normalized_pattern or normalized_path.startswith(f"{normalized_pattern}/")
    return normalized_path == normalized_pattern or fnmatch.fnmatch(normalized_path, normalized_pattern)


def _normalize_project_path(path: str) -> str:
    raw = str(path)
    normalized = posixpath.normpath(raw.replace("\\", "/") if os.name == "nt" else raw)
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../") or posixpath.isabs(normalized):
        return ""
    return normalized.strip("/")


def _path_is_under_root(path: str, root: str) -> bool:
    normalized_path = _normalize_project_path(path)
    normalized_root = _normalize_project_path(root)
    if not normalized_path or not normalized_root:
        return False
    return normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/")


def _path_parts(path: str) -> list[str]:
    normalized = _normalize_project_path(path)
    return [part for part in normalized.split("/") if part]


def _is_test_path_marker(part: str) -> bool:
    lower_part = part.lower()
    return (
        lower_part in _TEST_PATH_MARKERS
        or lower_part.endswith(("_test", "_tests", "-test", "-tests"))
        or part.endswith(("Test", "Tests"))
    )


def _path_looks_like_test_artifact(path: str) -> bool:
    parts = _path_parts(path)
    if any(_is_test_path_marker(part) for part in parts[:-1]):
        return True
    if not parts:
        return False
    filename = parts[-1]
    lower_filename = filename.lower()
    return (
        lower_filename.startswith(_TEST_FILE_PREFIXES)
        or lower_filename.endswith(
            tuple(suffix.lower() for suffix in _TEST_FILE_SUFFIXES if suffix.startswith((".", "_")))
        )
        or filename.endswith(tuple(suffix for suffix in _TEST_FILE_SUFFIXES if suffix[0].isupper()))
    )


class DeliveryScopeAudit:
    """Own delivery mutation policies, snapshots, recovery, and violation records."""

    def __init__(
        self,
        *,
        config: object,
        store: StateStore,
        tools: dict[str, object],
        agents: Callable[[], dict[str, object]],
    ) -> None:
        self._config = config
        self._store = store
        self._tools = tools
        self._agents_provider = agents
        self._delivery_scope_roots_cache: tuple[tuple[str, bool], ...] | None = None

    @property
    def _agents(self) -> dict[str, object]:
        return self._agents_provider()

    def _configured_test_write_paths(self) -> list[str]:
        raw = self._config.project_config.get("sandbox", {}).get("allowed_test_write_paths", [])
        if isinstance(raw, str):
            return [raw] if raw.strip() else []
        if isinstance(raw, list):
            return [str(item) for item in raw if str(item).strip()]
        return []

    @staticmethod
    def _path_in_write_roots(path: str, roots: tuple[str, ...] | list[str]) -> bool:
        normalized_path = _normalize_project_path(path)
        if not normalized_path:
            return False
        for raw_root in roots:
            root = _native_scope_path(raw_root).strip()
            normalized_root = _normalize_project_path(root)
            if not root:
                continue
            if posixpath.normpath(root) == ".":
                return True
            if not normalized_root:
                continue
            if any(ch in root for ch in "*?["):
                if _path_matches_pattern(normalized_path, root):
                    return True
                continue
            if _path_is_under_root(normalized_path, normalized_root):
                return True
        return False

    def _validation_artifact_root(self, state: TaskState) -> Path:
        if state.worktree_base:
            return Path(state.worktree_base).resolve()
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=self._config.project_root,
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip()).resolve()
        return self._config.project_root.resolve()

    def _validation_artifact_ignored_roots(self, root: Path) -> tuple[str, ...]:
        artifact_root = root.resolve()
        ignored_roots: list[str] = []
        for internal_path in self._store.internal_paths():
            try:
                relative = Path(internal_path).resolve(strict=False).relative_to(artifact_root)
            except (OSError, ValueError):
                continue
            relative_text = relative.as_posix().rstrip("/")
            if relative_text and relative_text != ".":
                ignored_roots.append(relative_text)
        return tuple(ignored_roots)

    def enabled(self, state: TaskState, name: str) -> bool:
        return self._delivery_scope_audit_enabled(state, name)

    def policy(
        self,
        state: TaskState,
        name: str,
        *,
        active_test_write_paths: tuple[str, ...] | None = None,
    ) -> DeliveryScopeAuditPolicy:
        return self._delivery_scope_audit_policy(
            state,
            name,
            active_test_write_paths=active_test_write_paths,
        )

    def set_pending(
        self,
        state: TaskState,
        name: str,
        *,
        policy: DeliveryScopeAuditPolicy | None = None,
    ) -> None:
        self._set_delivery_scope_audit_pending(state, name, policy=policy)

    def snapshot(
        self,
        state: TaskState,
        name: str,
        *,
        policy: DeliveryScopeAuditPolicy | None = None,
    ) -> dict | None:
        return self._delivery_scope_audit_snapshot(state, name, policy=policy)

    def record_snapshot_failure(self, state: TaskState, name: str) -> bool:
        return self._record_delivery_scope_snapshot_failure(state, name)

    def clear_pending(self, state: TaskState) -> None:
        self._clear_delivery_scope_audit_pending(state)

    def provider_attempt_boundary(
        self,
        state: TaskState,
        name: str,
        policy: DeliveryScopeAuditPolicy,
        attempt: dict[str, object],
    ):
        return self._delivery_scope_provider_attempt_boundary(state, name, policy, attempt)

    def tool_mutation_boundary(self, state: TaskState, phase: str):
        return self._delivery_scope_tool_mutation_boundary(state, phase)

    def audit_after_mutation(
        self,
        state: TaskState,
        name: str,
        before: dict | None,
        policy: DeliveryScopeAuditPolicy | None,
        **kwargs,
    ) -> bool:
        return self._audit_delivery_scope_after_mutation(state, name, before, policy, **kwargs)

    def recover_interrupted(self, state: TaskState) -> bool:
        return self._audit_interrupted_delivery_scope(state)

    def _delivery_scope_audit_enabled(self, state: TaskState, name: str) -> bool:
        return bool(
            (name in {"implementer", "fixer"} or name in DELIVERY_SCOPE_TOOL_ACTORS)
            and state.delivery_plan_id
            and state.delivery_unit_id
            and state.delivery_write_scope_schema_version is not None
        )

    def _delivery_scope_root_is_exact_file(self, path: str) -> bool:
        project_path = self._config.project_root.joinpath(*Path(path).parts)
        if project_path.is_file():
            return True
        try:
            result = subprocess.run(
                ["git", "cat-file", "-t", f"HEAD:./{path}"],
                cwd=self._config.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            return False
        return result.returncode == 0 and result.stdout.strip() == "blob"

    def _delivery_scope_roots(self, state: TaskState) -> tuple[tuple[str, bool], ...]:
        if self._delivery_scope_roots_cache is not None:
            return self._delivery_scope_roots_cache
        roots: list[tuple[str, bool]] = []
        for raw_root in self._config.allowed_write_paths:
            root_text = _native_scope_path(raw_root).strip()
            if posixpath.normpath(root_text) == ".":
                roots.append((".", False))
                continue
            normalized = _normalize_project_path(root_text)
            if not normalized:
                continue
            exact_file = self._delivery_scope_root_is_exact_file(normalized)
            roots.append((normalized, exact_file))
        self._delivery_scope_roots_cache = tuple(roots)
        return self._delivery_scope_roots_cache

    def _delivery_scope_path_allowed(
        self,
        state: TaskState,
        path: str,
        *,
        roots: tuple[tuple[str, bool], ...] | None = None,
    ) -> bool:
        normalized = _normalize_project_path(path)
        if not normalized:
            return False
        for root, exact_file in roots if roots is not None else self._delivery_scope_roots(state):
            if root == ".":
                return True
            if normalized == root:
                return True
            if not exact_file and _path_is_under_root(normalized, root):
                return True
        return False

    @staticmethod
    def _delivery_scope_snapshot_text(snapshot) -> str | None:
        if snapshot is None or snapshot.content is None:
            return None
        return snapshot.content.decode("utf-8", errors="replace")

    def _delivery_scope_fixer_test_change(
        self,
        policy: DeliveryScopeAuditPolicy,
        path: str,
        before,
        after,
    ) -> bool:
        if not self._path_in_write_roots(
            path,
            policy.resolved_test_write_paths,
        ):
            return False
        if _path_looks_like_test_artifact(path):
            return True
        build_tool: BuildTool = self._tools["build"]
        return build_tool.is_test_only_change(
            path,
            self._delivery_scope_snapshot_text(before),
            self._delivery_scope_snapshot_text(after),
        )

    def _delivery_scope_active_test_write_paths(self, state: TaskState, name: str) -> tuple[str, ...]:
        if name != "fixer":
            return ()
        resolver = getattr(self._agents.get(name), "delivery_scope_active_test_write_paths", None)
        if not callable(resolver):
            return ()
        try:
            requested = resolver(state)
        except Exception as exc:
            raise DeliveryScopeSnapshotError(
                "Delivery scope audit could not resolve the fixer's active test-write scope."
            ) from exc
        return self._validated_delivery_scope_test_write_paths(requested)

    def _validated_delivery_scope_test_write_paths(self, requested: object) -> tuple[str, ...]:
        if not isinstance(requested, list) or not all(isinstance(path, str) for path in requested):
            raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid active test-write scope.")
        configured = {
            _native_scope_path(path).strip()
            for path in self._configured_test_write_paths()
            if _native_scope_path(path).strip()
        }
        active = tuple(dict.fromkeys(_native_scope_path(raw_path).strip() for raw_path in requested))
        if any(path not in configured for path in active):
            raise DeliveryScopeSnapshotError(
                "Delivery scope audit received test-write authority outside the configured sandbox."
            )
        return active

    @staticmethod
    def _normalize_delivery_scope_authority_path(path: str) -> str:
        normalized = posixpath.normpath(_native_scope_path(path).strip())
        return "." if normalized in {"", "."} else normalized.rstrip("/")

    def _validated_delivery_scope_production_roots(
        self,
        state: TaskState,
        requested: object,
    ) -> tuple[tuple[str, bool], ...]:
        if not isinstance(requested, list) or not all(isinstance(path, str) for path in requested):
            raise DeliveryScopeSnapshotError("Delivery scope audit received invalid active production authority.")
        configured = {
            self._normalize_delivery_scope_authority_path(path): (path, exact_file)
            for path, exact_file in self._delivery_scope_roots(state)
        }
        active: list[tuple[str, bool]] = []
        for raw_path in requested:
            normalized = self._normalize_delivery_scope_authority_path(raw_path)
            root = configured.get(normalized)
            if root is None:
                raise DeliveryScopeSnapshotError(
                    "Delivery scope audit received production authority outside the effective unit scope."
                )
            if root not in active:
                active.append(root)
        return tuple(active)

    def _delivery_scope_provider_attempt_policy(
        self,
        state: TaskState,
        name: str,
        fallback: DeliveryScopeAuditPolicy,
    ) -> DeliveryScopeAuditPolicy:
        if name != "fixer":
            return fallback
        resolver = getattr(self._agents.get(name), "delivery_scope_attempt_write_authority", None)
        if not callable(resolver):
            return fallback
        try:
            authority = resolver(state)
        except Exception as exc:
            raise DeliveryScopeSnapshotError(
                "Delivery scope audit could not resolve the fixer's provider-call authority."
            ) from exc
        if authority is None:
            return fallback
        if not isinstance(authority, dict) or set(authority) != {
            "production_write_paths",
            "test_write_paths",
        }:
            raise DeliveryScopeSnapshotError("Delivery scope audit received malformed fixer provider-call authority.")
        production_roots = self._validated_delivery_scope_production_roots(
            state,
            authority.get("production_write_paths"),
        )
        test_write_paths = self._validated_delivery_scope_test_write_paths(authority.get("test_write_paths"))
        return self._delivery_scope_audit_policy(
            state,
            name,
            active_production_roots=production_roots,
            active_test_write_paths=test_write_paths,
        )

    def _delivery_scope_audit_policy(
        self,
        state: TaskState,
        name: str,
        *,
        active_production_roots: tuple[tuple[str, bool], ...] | None = None,
        active_test_write_paths: tuple[str, ...] | None = None,
    ) -> DeliveryScopeAuditPolicy:
        root, project_root, prefix = self._delivery_scope_audit_location(state)
        production_roots = (
            active_production_roots if active_production_roots is not None else self._delivery_scope_roots(state)
        )
        test_write_paths = (
            active_test_write_paths
            if active_test_write_paths is not None
            else self._delivery_scope_active_test_write_paths(state, name)
        )
        return DeliveryScopeAuditPolicy(
            root=root,
            project_root=project_root,
            project_prefix=prefix,
            production_roots=production_roots,
            resolved_production_roots=self._resolve_delivery_scope_policy_roots(project_root, production_roots),
            active_test_write_paths=test_write_paths,
            resolved_test_write_paths=self._resolve_delivery_scope_policy_paths(project_root, test_write_paths),
            agent=name,
        )

    def _delivery_scope_audit_location(self, state: TaskState) -> tuple[Path, Path, str]:
        root = self._validation_artifact_root(state).resolve()
        project_root = self._config.project_root.resolve()
        try:
            relative = project_root.relative_to(root)
        except ValueError as exc:
            raise DeliveryScopeSnapshotError(
                "Delivery scope project root is outside the authoritative worktree."
            ) from exc
        prefix = relative.as_posix() if relative.parts else "."
        return root, project_root, prefix

    @staticmethod
    def _resolve_delivery_scope_policy_path(project_root: Path, path: str) -> str:
        if posixpath.normpath(path) == ".":
            return "."
        if any(character in path for character in "*?["):
            normalized = _normalize_project_path(path)
            if not normalized:
                raise DeliveryScopeSnapshotError("Delivery scope audit received an invalid write root.")
            return normalized
        try:
            resolved = project_root.joinpath(*path.split("/")).resolve(strict=False)
            relative = resolved.relative_to(project_root)
        except (OSError, RuntimeError, ValueError) as exc:
            raise DeliveryScopeSnapshotError("Delivery scope audit write authority escapes the project.") from exc
        return relative.as_posix() if relative.parts else "."

    @classmethod
    def _resolve_delivery_scope_policy_roots(
        cls,
        project_root: Path,
        roots: tuple[tuple[str, bool], ...],
    ) -> tuple[tuple[str, bool], ...]:
        return tuple(
            (cls._resolve_delivery_scope_policy_path(project_root, path), exact_file) for path, exact_file in roots
        )

    @classmethod
    def _resolve_delivery_scope_policy_paths(
        cls,
        project_root: Path,
        paths: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(cls._resolve_delivery_scope_policy_path(project_root, path) for path in paths)

    @staticmethod
    def _delivery_scope_project_path(policy: DeliveryScopeAuditPolicy, path: str) -> str | None:
        normalized = _normalize_project_path(path)
        if not normalized:
            return None
        if policy.project_prefix == ".":
            return normalized
        prefix = f"{policy.project_prefix}/"
        if not normalized.startswith(prefix):
            return None
        project_path = normalized[len(prefix) :]
        return project_path or None

    def _delivery_scope_policy_allows_path(
        self,
        state: TaskState,
        policy: DeliveryScopeAuditPolicy,
        path: str,
    ) -> bool:
        production_roots = policy.resolved_production_roots
        test_write_paths = policy.resolved_test_write_paths
        if posixpath.normpath(_normalize_artifact_path(str(path))) == ".":
            return any(root == "." for root, _ in production_roots) or (
                policy.agent == "fixer" and any(posixpath.normpath(root) == "." for root in test_write_paths)
            )
        return self._delivery_scope_path_allowed(state, path, roots=production_roots) or (
            policy.agent == "fixer" and self._path_in_write_roots(path, test_write_paths)
        )

    def _delivery_scope_symlink_allowed(
        self,
        state: TaskState,
        policy: DeliveryScopeAuditPolicy,
        audit_path: str,
        target: str,
    ) -> bool:
        project_path = self._delivery_scope_project_path(policy, audit_path)
        if project_path is None:
            return True
        symlink_path = policy.root.joinpath(*audit_path.split("/"))
        try:
            resolved_target = (symlink_path.parent / target).resolve(strict=False)
            target_project_path = resolved_target.relative_to(policy.project_root).as_posix()
        except (OSError, RuntimeError, ValueError):
            return False
        lexical_scope = self._delivery_scope_path_allowed(
            state,
            project_path,
            roots=policy.production_roots,
        ) or (policy.agent == "fixer" and self._path_in_write_roots(project_path, policy.active_test_write_paths))
        resolved_scope = self._delivery_scope_policy_allows_path(state, policy, project_path)
        if not lexical_scope and not resolved_scope:
            return True
        return self._delivery_scope_policy_allows_path(state, policy, target_project_path)

    def _validate_delivery_scope_policy_bindings(self, policy: DeliveryScopeAuditPolicy) -> None:
        current_production_roots = self._resolve_delivery_scope_policy_roots(
            policy.project_root,
            policy.production_roots,
        )
        current_test_paths = self._resolve_delivery_scope_policy_paths(
            policy.project_root,
            policy.active_test_write_paths,
        )
        if (
            current_production_roots != policy.resolved_production_roots
            or current_test_paths != policy.resolved_test_write_paths
        ):
            raise DeliveryScopeSnapshotError(
                "Delivery scope audit write-root binding changed before the filesystem snapshot."
            )

    def _delivery_scope_retain_content(
        self,
        policy: DeliveryScopeAuditPolicy,
        path: str,
    ) -> bool:
        if policy.agent != "fixer":
            return False
        project_path = self._delivery_scope_project_path(policy, path)
        if (
            project_path is None
            or not self._path_in_write_roots(
                project_path,
                policy.resolved_test_write_paths,
            )
            or _path_looks_like_test_artifact(project_path)
        ):
            return False
        build_tool: BuildTool = self._tools["build"]
        requires_content = getattr(build_tool, "requires_test_only_change_content", None)
        return bool(callable(requires_content) and requires_content(project_path))

    @staticmethod
    def _delivery_scope_snapshot_project_root(policy: DeliveryScopeAuditPolicy, path: str) -> str:
        if policy.project_prefix == ".":
            return path
        if posixpath.normpath(path) == ".":
            return policy.project_prefix
        return f"{policy.project_prefix}/{path}"

    def _delivery_scope_snapshot_symlink_roots(self, policy: DeliveryScopeAuditPolicy) -> tuple[str, ...]:
        # Shell-capable providers can reach links outside their declared roots. Traverse
        # project metadata so an escaping link is rejected before an external write.
        return (self._delivery_scope_snapshot_project_root(policy, "."),)

    def _delivery_scope_ephemeral_path(self, policy: DeliveryScopeAuditPolicy, path: str) -> bool:
        project_path = self._delivery_scope_project_path(policy, path)
        if project_path is None:
            return False
        build_tool: BuildTool = self._tools["build"]
        classifier = getattr(build_tool, "is_ephemeral_build_path", None)
        return bool(callable(classifier) and classifier(project_path))

    def _capture_delivery_scope_snapshot(
        self,
        state: TaskState,
        policy: DeliveryScopeAuditPolicy,
        *,
        git_baseline: str,
        git_dir: str,
        git_common_dir: str,
        git_ignore_fingerprint: str,
        git_ref_fingerprint: str,
        validate_policy_bindings: bool = False,
    ) -> dict:
        if validate_policy_bindings:
            self._validate_delivery_scope_policy_bindings(policy)
        return snapshot_delivery_scope_files(
            policy.root,
            git_baseline=git_baseline,
            git_dir=git_dir,
            git_common_dir=git_common_dir,
            git_ignore_fingerprint=git_ignore_fingerprint,
            git_ref_fingerprint=git_ref_fingerprint,
            ignored_roots=self._validation_artifact_ignored_roots(policy.root),
            include_content=lambda path: self._delivery_scope_retain_content(policy, path),
            validate_symlink=lambda path, target: self._delivery_scope_symlink_allowed(state, policy, path, target),
            symlink_roots=self._delivery_scope_snapshot_symlink_roots(policy),
            exclude_ephemeral=lambda path: self._delivery_scope_ephemeral_path(policy, path),
        )

    def _delivery_scope_audit_snapshot(
        self,
        state: TaskState,
        name: str,
        *,
        policy: DeliveryScopeAuditPolicy | None = None,
    ) -> dict | None:
        if not self._delivery_scope_audit_enabled(state, name):
            return None
        policy = policy or self._delivery_scope_audit_policy(state, name)
        git_baseline = self._delivery_scope_pending_git_baseline(state.delivery_scope_audit_pending)
        git_dir = self._delivery_scope_pending_git_dir(state.delivery_scope_audit_pending)
        git_common_dir = self._delivery_scope_pending_git_common_dir(state.delivery_scope_audit_pending)
        git_ignore_fingerprint = self._delivery_scope_pending_git_ignore_fingerprint(state.delivery_scope_audit_pending)
        git_ref_fingerprint = self._delivery_scope_pending_git_ref_fingerprint(state.delivery_scope_audit_pending)
        if (
            git_baseline is None
            or git_dir is None
            or git_common_dir is None
            or git_ignore_fingerprint is None
            or git_ref_fingerprint is None
        ):
            binding = delivery_scope_git_binding(policy.root)
            git_baseline = binding.baseline
            git_dir = binding.git_dir
            git_common_dir = binding.common_dir
            git_ignore_fingerprint = binding.ignore_fingerprint
            git_ref_fingerprint = binding.ref_fingerprint
        snapshot = self._capture_delivery_scope_snapshot(
            state,
            policy,
            git_baseline=git_baseline,
            git_dir=git_dir,
            git_common_dir=git_common_dir,
            git_ignore_fingerprint=git_ignore_fingerprint,
            git_ref_fingerprint=git_ref_fingerprint,
            validate_policy_bindings=True,
        )
        self._store.save_text_snapshot(
            state.task_id,
            self._delivery_scope_pending_snapshot_name(state.delivery_scope_audit_pending)
            or DELIVERY_SCOPE_AUDIT_SNAPSHOT,
            serialize_delivery_scope_snapshot(snapshot),
        )
        return snapshot

    @staticmethod
    def _bounded_delivery_scope_paths(paths) -> list[str]:
        bounded: list[str] = []
        for path in sorted(str(path) for path in paths)[:DELIVERY_SCOPE_AUDIT_PATH_LIMIT]:
            bounded.append(sanitize_delivery_public_metadata(path) or "<redacted>")
        return bounded

    def _audit_delivery_scope_after_mutation(
        self,
        state: TaskState,
        name: str,
        before: dict | None,
        policy: DeliveryScopeAuditPolicy | None,
        *,
        git_baseline: str | None = None,
        git_dir: str | None = None,
        git_common_dir: str | None = None,
        git_ignore_fingerprint: str | None = None,
        git_ref_fingerprint: str | None = None,
        provider_attempt: dict[str, object] | None = None,
        tool_phase: str | None = None,
    ) -> bool:
        if before is None:
            return False
        try:
            if policy is None:
                raise DeliveryScopeSnapshotError("Delivery scope audit policy is unavailable.")
            baseline = git_baseline or self._delivery_scope_pending_git_baseline(state.delivery_scope_audit_pending)
            bound_git_dir = git_dir or self._delivery_scope_pending_git_dir(state.delivery_scope_audit_pending)
            bound_common_dir = git_common_dir or self._delivery_scope_pending_git_common_dir(
                state.delivery_scope_audit_pending
            )
            bound_ignore_fingerprint = git_ignore_fingerprint or self._delivery_scope_pending_git_ignore_fingerprint(
                state.delivery_scope_audit_pending
            )
            bound_ref_fingerprint = git_ref_fingerprint or self._delivery_scope_pending_git_ref_fingerprint(
                state.delivery_scope_audit_pending
            )
            if (
                baseline is None
                or bound_git_dir is None
                or bound_common_dir is None
                or bound_ignore_fingerprint is None
                or bound_ref_fingerprint is None
            ):
                raise DeliveryScopeSnapshotError("Delivery scope audit Git binding is unavailable.")
            after = self._capture_delivery_scope_snapshot(
                state,
                policy,
                git_baseline=baseline,
                git_dir=bound_git_dir,
                git_common_dir=bound_common_dir,
                git_ignore_fingerprint=bound_ignore_fingerprint,
                git_ref_fingerprint=bound_ref_fingerprint,
            )
            changed = detect_validation_artifacts(before, after)
            changed_paths = [artifact.path for artifact in changed]
            return self._record_delivery_scope_audit(
                state,
                name,
                changed_paths,
                before=before,
                after=after,
                policy=policy,
                provider_attempt=provider_attempt,
                tool_phase=tool_phase,
            )
        except DeliveryScopeSnapshotError:
            return self._record_delivery_scope_snapshot_failure(state, name)

    @contextmanager
    def _delivery_scope_provider_attempt_boundary(
        self,
        state: TaskState,
        name: str,
        policy: DeliveryScopeAuditPolicy,
        attempt: dict[str, object],
    ) -> Iterator[None]:
        if state.delivery_stop_code == DELIVERY_STOP_UNIT_SCOPE_VIOLATION:
            raise DeliveryScopeProviderAttemptStopped(DELIVERY_SCOPE_VIOLATION_CODE)
        previous_pending = state.delivery_scope_audit_pending
        try:
            policy = self._delivery_scope_provider_attempt_policy(state, name, policy)
            binding = delivery_scope_git_binding(policy.root)
            before = self._capture_delivery_scope_snapshot(
                state,
                policy,
                git_baseline=binding.baseline,
                git_dir=binding.git_dir,
                git_common_dir=binding.common_dir,
                git_ignore_fingerprint=binding.ignore_fingerprint,
                git_ref_fingerprint=binding.ref_fingerprint,
                validate_policy_bindings=True,
            )
            self._store.save_text_snapshot(
                state.task_id,
                DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT,
                serialize_delivery_scope_snapshot(before),
            )
            self._set_delivery_scope_audit_pending(
                state,
                name,
                policy=policy,
                binding=binding,
                snapshot_name=DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT,
            )
        except (DeliveryScopeSnapshotError, OSError, TypeError, ValueError) as exc:
            self._record_delivery_scope_snapshot_failure(state, name)
            self._restore_delivery_scope_outer_pending(state, previous_pending)
            raise DeliveryScopeProviderAttemptStopped("delivery_scope_audit_unavailable") from exc

        try:
            yield
        except BaseException as provider_error:
            stopped = self._audit_delivery_scope_after_mutation(
                state,
                name,
                before,
                policy,
                git_baseline=binding.baseline,
                git_dir=binding.git_dir,
                git_common_dir=binding.common_dir,
                git_ignore_fingerprint=binding.ignore_fingerprint,
                git_ref_fingerprint=binding.ref_fingerprint,
                provider_attempt=attempt,
            )
            self._store.save(state)
            self._restore_delivery_scope_outer_pending(state, previous_pending)
            if stopped:
                raise DeliveryScopeProviderAttemptStopped(DELIVERY_SCOPE_VIOLATION_CODE) from provider_error
            raise
        else:
            stopped = self._audit_delivery_scope_after_mutation(
                state,
                name,
                before,
                policy,
                git_baseline=binding.baseline,
                git_dir=binding.git_dir,
                git_common_dir=binding.common_dir,
                git_ignore_fingerprint=binding.ignore_fingerprint,
                git_ref_fingerprint=binding.ref_fingerprint,
                provider_attempt=attempt,
            )
            self._store.save(state)
            self._restore_delivery_scope_outer_pending(state, previous_pending)
            if stopped:
                raise DeliveryScopeProviderAttemptStopped(DELIVERY_SCOPE_VIOLATION_CODE)

    def _restore_delivery_scope_outer_pending(
        self,
        state: TaskState,
        pending: object,
    ) -> None:
        state.delivery_scope_audit_pending = pending
        self._store.save(state)
        self._store.delete_text_snapshot(state.task_id, DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT)

    @contextmanager
    def _delivery_scope_tool_mutation_boundary(
        self,
        state: TaskState,
        phase: str,
    ) -> Iterator[None]:
        name = f"tool:{phase}"
        if name not in DELIVERY_SCOPE_TOOL_ACTORS:
            raise ValueError(f"unsupported delivery scope tool mutation phase: {phase}")
        if not self._delivery_scope_audit_enabled(state, name):
            yield
            return

        pending_set = False
        try:
            policy = self._delivery_scope_audit_policy(state, name, active_test_write_paths=())
            self._set_delivery_scope_audit_pending(state, name, policy=policy)
            pending_set = True
            before = self._delivery_scope_audit_snapshot(state, name, policy=policy)
        except DeliveryScopeSnapshotError as exc:
            self._record_delivery_scope_snapshot_failure(state, name)
            if pending_set:
                self._clear_delivery_scope_audit_pending(state)
            raise DeliveryScopeToolMutationStopped("delivery_scope_audit_unavailable") from exc

        try:
            yield
        except BaseException as tool_error:
            stopped = self._audit_delivery_scope_after_mutation(
                state,
                name,
                before,
                policy,
                tool_phase=phase,
            )
            self._store.save(state)
            self._clear_delivery_scope_audit_pending(state)
            if not isinstance(tool_error, Exception):
                raise
            if stopped:
                raise DeliveryScopeToolMutationStopped(DELIVERY_SCOPE_VIOLATION_CODE) from tool_error
            raise
        else:
            stopped = self._audit_delivery_scope_after_mutation(
                state,
                name,
                before,
                policy,
                tool_phase=phase,
            )
            self._store.save(state)
            self._clear_delivery_scope_audit_pending(state)
            if stopped:
                raise DeliveryScopeToolMutationStopped(DELIVERY_SCOPE_VIOLATION_CODE)

    def _set_delivery_scope_audit_pending(
        self,
        state: TaskState,
        name: str,
        *,
        policy: DeliveryScopeAuditPolicy | None = None,
        binding: DeliveryScopeGitBinding | None = None,
        snapshot_name: str = DELIVERY_SCOPE_AUDIT_SNAPSHOT,
    ) -> None:
        policy = policy or self._delivery_scope_audit_policy(state, name)
        binding = binding or delivery_scope_git_binding(policy.root)
        state.delivery_scope_audit_pending = {
            "schema_version": DELIVERY_SCOPE_AUDIT_PENDING_SCHEMA_VERSION,
            "agent": name,
            "project_prefix": policy.project_prefix,
            "git_baseline": binding.baseline,
            "git_dir": binding.git_dir,
            "git_common_dir": binding.common_dir,
            "git_ignore_fingerprint": binding.ignore_fingerprint,
            "git_ref_fingerprint": binding.ref_fingerprint,
            "snapshot_name": snapshot_name,
            "production_roots": [
                {
                    "path": path,
                    "resolved_path": policy.resolved_production_roots[index][0],
                    "exact_file": exact_file,
                }
                for index, (path, exact_file) in enumerate(policy.production_roots)
            ],
            "active_test_write_paths": [
                {
                    "path": path,
                    "resolved_path": policy.resolved_test_write_paths[index],
                }
                for index, path in enumerate(policy.active_test_write_paths)
            ],
        }
        self._store.save(state)

    def _clear_delivery_scope_audit_pending(self, state: TaskState) -> None:
        state.delivery_scope_audit_pending = None
        self._store.save(state)
        self._store.delete_text_snapshot(state.task_id, DELIVERY_SCOPE_AUDIT_SNAPSHOT)
        self._store.delete_text_snapshot(state.task_id, DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT)

    def _audit_interrupted_delivery_scope(
        self,
        state: TaskState,
    ) -> bool:
        pending = state.delivery_scope_audit_pending
        name = (
            pending.get("agent") if isinstance(pending, dict) and isinstance(pending.get("agent"), str) else "unknown"
        )
        try:
            snapshot_name = self._delivery_scope_pending_snapshot_name(pending)
            persisted = self._store.load_text_snapshot(
                state.task_id,
                snapshot_name or DELIVERY_SCOPE_AUDIT_SNAPSHOT,
            )
            orphaned_attempt = self._store.load_text_snapshot(
                state.task_id,
                DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT,
            )
            if pending is None and persisted is None and orphaned_attempt is None:
                return False
            pending_schema_version = pending.get("schema_version") if isinstance(pending, dict) else None
            if (
                isinstance(pending_schema_version, bool)
                or not self._delivery_scope_audit_enabled(state, name)
                or persisted is None
            ):
                stopped = self._record_delivery_scope_snapshot_failure(state, name)
            else:
                policy = self._delivery_scope_pending_policy(state, name, pending)
                git_baseline = self._delivery_scope_pending_git_baseline(pending)
                git_dir = self._delivery_scope_pending_git_dir(pending)
                git_common_dir = self._delivery_scope_pending_git_common_dir(pending)
                git_ignore_fingerprint = self._delivery_scope_pending_git_ignore_fingerprint(pending)
                git_ref_fingerprint = self._delivery_scope_pending_git_ref_fingerprint(pending)
                if (
                    policy is None
                    or git_baseline is None
                    or git_dir is None
                    or git_common_dir is None
                    or git_ignore_fingerprint is None
                    or git_ref_fingerprint is None
                ):
                    stopped = self._record_delivery_scope_snapshot_failure(state, name)
                else:
                    before = deserialize_delivery_scope_snapshot(persisted)
                    after = self._capture_delivery_scope_snapshot(
                        state,
                        policy,
                        git_baseline=git_baseline,
                        git_dir=git_dir,
                        git_common_dir=git_common_dir,
                        git_ignore_fingerprint=git_ignore_fingerprint,
                        git_ref_fingerprint=git_ref_fingerprint,
                    )
                    changed = detect_validation_artifacts(before, after)
                    stopped = self._record_delivery_scope_audit(
                        state,
                        name,
                        [artifact.path for artifact in changed],
                        before=before,
                        after=after,
                        policy=policy,
                        resume_recovery=True,
                        tool_phase=name.removeprefix("tool:") if name in DELIVERY_SCOPE_TOOL_ACTORS else None,
                    )
        except (DeliveryScopeSnapshotError, OSError, TypeError, ValueError):
            stopped = self._record_delivery_scope_snapshot_failure(state, name)
        self._store.save(state)
        self._clear_delivery_scope_audit_pending(state)
        return stopped

    def _delivery_scope_pending_policy(
        self,
        state: TaskState,
        name: str,
        pending: object,
    ) -> DeliveryScopeAuditPolicy | None:
        if not isinstance(pending, dict):
            return None
        schema_version = pending.get("schema_version")
        if schema_version != DELIVERY_SCOPE_AUDIT_PENDING_SCHEMA_VERSION or set(pending) != {
            "schema_version",
            "agent",
            "project_prefix",
            "git_baseline",
            "git_dir",
            "git_common_dir",
            "git_ignore_fingerprint",
            "git_ref_fingerprint",
            "snapshot_name",
            "production_roots",
            "active_test_write_paths",
        }:
            return None
        parsed_roots = self._parse_delivery_scope_pending_bound_roots(pending.get("production_roots"))
        parsed_test_paths = self._parse_delivery_scope_pending_bound_paths(pending.get("active_test_write_paths"))
        if parsed_roots is None or parsed_test_paths is None:
            return None
        production_roots, resolved_production_roots = parsed_roots
        active_test_write_paths, resolved_test_write_paths = parsed_test_paths
        project_prefix = pending.get("project_prefix")
        root, project_root, current_project_prefix = self._delivery_scope_audit_location(state)
        if project_prefix != current_project_prefix:
            return None
        return DeliveryScopeAuditPolicy(
            root=root,
            project_root=project_root,
            project_prefix=current_project_prefix,
            production_roots=production_roots,
            resolved_production_roots=resolved_production_roots,
            active_test_write_paths=active_test_write_paths,
            resolved_test_write_paths=resolved_test_write_paths,
            agent=name,
        )

    @staticmethod
    def _delivery_scope_pending_git_baseline(pending: object) -> str | None:
        if not isinstance(pending, dict):
            return None
        value = pending.get("git_baseline")
        if not isinstance(value, str) or len(value) not in (40, 64):
            return None
        if any(character not in "0123456789abcdef" for character in value):
            return None
        return value

    @staticmethod
    def _delivery_scope_pending_git_dir(pending: object) -> str | None:
        if not isinstance(pending, dict):
            return None
        value = pending.get("git_dir")
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return None
        return value

    @staticmethod
    def _delivery_scope_pending_git_common_dir(pending: object) -> str | None:
        if not isinstance(pending, dict):
            return None
        value = pending.get("git_common_dir")
        if not isinstance(value, str) or not value or not Path(value).is_absolute():
            return None
        return value

    @staticmethod
    def _delivery_scope_pending_git_ignore_fingerprint(pending: object) -> str | None:
        if not isinstance(pending, dict):
            return None
        value = pending.get("git_ignore_fingerprint")
        if not isinstance(value, str) or len(value) != 64:
            return None
        if any(character not in "0123456789abcdef" for character in value):
            return None
        return value

    @staticmethod
    def _delivery_scope_pending_git_ref_fingerprint(pending: object) -> str | None:
        if not isinstance(pending, dict):
            return None
        value = pending.get("git_ref_fingerprint")
        if not isinstance(value, str) or len(value) != 64:
            return None
        if any(character not in "0123456789abcdef" for character in value):
            return None
        return value

    @staticmethod
    def _delivery_scope_pending_snapshot_name(pending: object) -> str | None:
        if not isinstance(pending, dict):
            return None
        value = pending.get("snapshot_name")
        if value not in {DELIVERY_SCOPE_AUDIT_SNAPSHOT, DELIVERY_SCOPE_ATTEMPT_AUDIT_SNAPSHOT}:
            return None
        return value

    @staticmethod
    def _parse_delivery_scope_pending_bound_roots(
        value: object,
    ) -> tuple[tuple[tuple[str, bool], ...], tuple[tuple[str, bool], ...]] | None:
        if not isinstance(value, list):
            return None
        lexical_roots: list[tuple[str, bool]] = []
        resolved_roots: list[tuple[str, bool]] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"path", "resolved_path", "exact_file"}:
                return None
            path = item.get("path")
            resolved_path = item.get("resolved_path")
            exact_file = item.get("exact_file")
            if not isinstance(path, str) or not isinstance(resolved_path, str) or type(exact_file) is not bool:
                return None
            normalized = "." if posixpath.normpath(path) == "." else _normalize_project_path(path)
            normalized_resolved = (
                "." if posixpath.normpath(resolved_path) == "." else _normalize_project_path(resolved_path)
            )
            if (
                normalized != path
                or normalized_resolved != resolved_path
                or path in seen
                or (path == "." and exact_file)
                or (resolved_path == "." and exact_file)
            ):
                return None
            lexical_roots.append((path, exact_file))
            resolved_roots.append((resolved_path, exact_file))
            seen.add(path)
        return tuple(lexical_roots), tuple(resolved_roots)

    @staticmethod
    def _parse_delivery_scope_pending_bound_paths(value: object) -> tuple[tuple[str, ...], tuple[str, ...]] | None:
        if not isinstance(value, list):
            return None
        lexical_paths: list[str] = []
        resolved_paths: list[str] = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"path", "resolved_path"}:
                return None
            path = item.get("path")
            resolved_path = item.get("resolved_path")
            if not isinstance(path, str) or not isinstance(resolved_path, str):
                return None
            normalized = "." if posixpath.normpath(path) == "." else _normalize_project_path(path)
            normalized_resolved = (
                "." if posixpath.normpath(resolved_path) == "." else _normalize_project_path(resolved_path)
            )
            if normalized != path or normalized_resolved != resolved_path or path in seen:
                return None
            lexical_paths.append(path)
            resolved_paths.append(resolved_path)
            seen.add(path)
        return tuple(lexical_paths), tuple(resolved_paths)

    def _record_delivery_scope_snapshot_failure(self, state: TaskState, name: str) -> bool:
        message = "Delivery scope audit could not establish a trustworthy filesystem snapshot."
        metadata = {"code": "delivery_scope_audit_unavailable", "agent": name}
        if name in DELIVERY_SCOPE_TOOL_ACTORS:
            metadata["audit_boundary"] = "tool_mutation"
            metadata["tool_phase"] = name.removeprefix("tool:")
        state.record_validation(
            "delivery_scope_audit",
            "failed",
            error=message,
            metadata=metadata,
        )
        state.record("orchestrator", "delivery_scope_audit_unavailable", message)
        state.set_delivery_terminal_stop(DELIVERY_STOP_UNIT_SCOPE_VIOLATION, source="orchestrator")
        self._store.save(state)
        return True

    def _record_delivery_scope_audit(
        self,
        state: TaskState,
        name: str,
        changed_paths: list[str],
        *,
        before: dict,
        after: dict,
        policy: DeliveryScopeAuditPolicy,
        resume_recovery: bool = False,
        provider_attempt: dict[str, object] | None = None,
        tool_phase: str | None = None,
    ) -> bool:
        project_paths: list[tuple[str, str]] = []
        outside_project_paths: list[str] = []
        for path in changed_paths:
            project_path = self._delivery_scope_project_path(policy, path)
            if project_path is None:
                outside_project_paths.append(path)
            else:
                project_paths.append((path, project_path))
        production_paths = [
            (audit_path, project_path)
            for audit_path, project_path in project_paths
            if not (
                name == "fixer"
                and self._delivery_scope_fixer_test_change(
                    policy,
                    project_path,
                    before.get(audit_path),
                    after.get(audit_path),
                )
            )
        ]
        violation_paths = [
            project_path
            for _, project_path in production_paths
            if not self._delivery_scope_path_allowed(
                state,
                project_path,
                roots=policy.resolved_production_roots,
            )
        ]
        violation_count = len(violation_paths) + len(outside_project_paths)
        metadata: dict[str, object] = {
            "code": DELIVERY_SCOPE_VIOLATION_CODE if violation_count else "delivery_scope_audit_passed",
            "agent": name,
            "changed_count": len(changed_paths),
            "changed_paths": self._bounded_delivery_scope_paths(project_path for _, project_path in project_paths),
            "production_changed_count": len(production_paths) + len(outside_project_paths),
            "violation_count": violation_count,
            "declared_count": len(state.delivery_declared_write_paths),
            "effective_count": len(policy.production_roots),
        }
        if outside_project_paths:
            metadata["outside_project_count"] = len(outside_project_paths)
            metadata["outside_project_paths"] = self._bounded_delivery_scope_paths(outside_project_paths)
        if resume_recovery:
            metadata["resume_recovery"] = True
        if provider_attempt is not None:
            metadata["audit_boundary"] = "provider_attempt"
            provider = provider_attempt.get("provider")
            attempt = provider_attempt.get("attempt")
            max_attempts = provider_attempt.get("max_attempts")
            if isinstance(provider, str) and provider:
                metadata["provider"] = provider
            if isinstance(attempt, int) and not isinstance(attempt, bool) and attempt > 0:
                metadata["provider_attempt"] = attempt
            if isinstance(max_attempts, int) and not isinstance(max_attempts, bool) and max_attempts > 0:
                metadata["provider_max_attempts"] = max_attempts
        if tool_phase is not None:
            metadata["audit_boundary"] = "tool_mutation"
            metadata["tool_phase"] = tool_phase
        if violation_count:
            metadata.update(
                {
                    "violation_paths": self._bounded_delivery_scope_paths(violation_paths),
                    "declared_paths": self._bounded_delivery_scope_paths(state.delivery_declared_write_paths),
                    "effective_paths": self._bounded_delivery_scope_paths(path for path, _ in policy.production_roots),
                    "recommended_action": "delivery_amend_prepare",
                }
            )
        state.record_validation(
            "delivery_scope_audit",
            "failed" if violation_count else "passed",
            error=(
                "Production changes escaped the effective delivery-unit scope; prepare a delivery amendment."
                if violation_count
                else None
            ),
            metadata=metadata,
        )
        if not violation_count:
            return False

        message = (
            f"{DELIVERY_SCOPE_VIOLATION_CODE}: {violation_count} production path(s) escaped the "
            "effective delivery-unit scope; prepare a delivery amendment before retrying"
        )
        log.error(message)
        state.record("orchestrator", DELIVERY_SCOPE_VIOLATION_CODE, message)
        state.set_delivery_terminal_stop(
            DELIVERY_STOP_UNIT_SCOPE_VIOLATION,
            source="orchestrator",
        )
        self._store.save(state)
        return True
