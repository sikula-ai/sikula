from __future__ import annotations

import argparse
import copy
from dataclasses import replace
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
from unittest.mock import patch

import pytest
import yaml

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from agents.delivery_repair_agent import (
    DeliveryRepairAgent,
    DeliveryRepairAuthoringError,
    build_delivery_repair_prompt,
    parse_delivery_repair_draft,
)
from core.delivery_asset_assignment import DeliveryAssetAssignmentError, render_inherited_delivery_assets
from core.delivery_handoff import build_delivery_unit_handoff, delivery_unit_handoff_path, write_delivery_unit_handoff
import core.delivery_repair as repair_module
from core.delivery_repair import coordinate_delivery_repair
from core.delivery_repair_storage import read_repair_state, write_repair_state
from core.delivery_obligations import delivery_authority_fragments
from core.delivery_progress import (
    DeliveryProgress,
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    read_delivery_progress,
    write_delivery_progress,
)
from core.delivery_verify import DeliveryVerifyResult, verify_delivery_plan
from core.delivery_verification import with_delivery_verification_readiness
from core.delivery_verification_validation import DeliveryVerificationValidationResult
from core.state import JsonStateStore, TaskState
from core.delivery_write_scope import resolve_delivery_write_scope
from core.delivery_run_next import preview_delivery_run_next
from core.delivery_run import DeliveryRunResult
from core.llm_client import ClaudeClient, GeminiClient, LLMConfig, LLMConfigurationError, LLMReadOnlyViolation


_CONTRACT = """# Cache consistency

## Goal
Make updates visible through the existing read path in this repository.

## Current behavior
Reads can return previously cached values after a successful update.

## Desired behavior
Invalidate the affected cache key when its stored value changes, within the existing API.

## Acceptance criteria
- Reading a value, updating it, and reading again returns the updated value.
- A rejected update preserves the stored value and does not invalidate unrelated keys.
- Existing successful read and update behavior remains compatible.

## Out of scope
Do not replace external dependencies or change public APIs.

## Security and privacy
Do not log values, credentials, or private source excerpts.

## Reviewer focus
Check that all write paths invalidate the appropriate cache entry and failed writes remain unchanged.

## Verification
- `python -m pytest tests/`
"""


def _git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, check=True, text=True).stdout.strip()


class _LLM:
    def __init__(self, *outputs: str | BaseException) -> None:
        self.outputs = list(outputs)
        self.calls: list[str] = []

    def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
        self.calls.append(prompt)
        output = self.outputs.pop(0)
        if isinstance(output, BaseException):
            raise output
        return output


def _draft(disposition: str = "repair", markdown: str = _CONTRACT) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "disposition": disposition,
            "task_markdown": markdown if disposition == "repair" else None,
        }
    )


def _assessment(disposition: str = "repair_required") -> str:
    approved = disposition == "approved"
    return json.dumps(
        {
            "schema_version": 2,
            "disposition": disposition,
            "summary": "Candidate satisfies the source."
            if approved
            else "The cache retains stale values after an update.",
            "findings": []
            if approved
            else [
                {
                    "code": "stale_cache",
                    "summary": "Updates must invalidate cached values.",
                    "unit_ids": ["read", "write"],
                    "obligation_ids": ["consistent-read"],
                }
            ],
            "obligation_results": [{"id": "consistent-read", "outcome": "satisfied" if approved else "missing"}],
        }
    )


@pytest.fixture
def completed_plan(tmp_path: Path, request: pytest.FixtureRequest) -> tuple[Path, dict]:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text(".sikula/state/\n.sikula/worktrees/\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src/cache.py").write_text("cache = {}\n", encoding="utf-8")
    if getattr(request, "param", None) in {"claude_settings", "gemini_settings"}:
        provider = request.param.removesuffix("_settings")
        settings = tmp_path / f".{provider}/settings.json"
        settings.parent.mkdir()
        settings.write_text("{}\n", encoding="utf-8")
    units = tmp_path / ".sikula/delivery/cache/units"
    units.mkdir(parents=True)
    names = ["read", "write"]
    if getattr(request, "param", None) in {"at_unit_limit", "below_unit_limit"}:
        count = 256 if request.param == "at_unit_limit" else 255
        names += [f"extra-{index}" for index in range(count - len(names))]
    contract = _CONTRACT
    if getattr(request, "param", None) == "oversized_prompt":
        contract += "\n" + "é" * 60_000
    asset_mode = getattr(request, "param", None)
    if asset_mode in {"candidate_asset", "missing_candidate_asset"}:
        contract += "\n## Assets\n\n- Reference asset: `assets/reference.png`\n"
        (tmp_path / "assets").mkdir()
        if asset_mode == "candidate_asset":
            (tmp_path / "assets/reference.png").write_bytes(b"reference image")
    for unit_id in names:
        (units / (unit_id + ".md")).write_text(contract, encoding="utf-8")
    source = "# Consistency\n\nCompleted updates must be visible through reads. Do not replace external dependencies.\n"
    source_path = tmp_path / "source.md"
    source_path.write_text(source, encoding="utf-8")
    fragments = delivery_authority_fragments(source)
    data = {
        "schema_version": 2,
        "plan_id": "cache",
        "title": "Cache behavior",
        "final_branch": "sikula/delivery/cache",
        "verification": {"mode": "final_gate"},
        "source_task": {"path": "source.md", "sha256": "sha256:" + sha256(source.encode()).hexdigest()},
        "repositories": [{"id": "main", "root": "."}],
        "units": [
            {
                "id": name,
                "task_path": f".sikula/delivery/cache/units/{name}.md",
                "depends_on": [],
                "scope_paths": ["src/"],
            }
            for name in names
        ],
        "constraints": [
            {
                "id": "external-ownership",
                "kind": "prohibited_fallback",
                "summary": "Keep externally owned implementations unchanged.",
                "unit_ids": ["read", "write"],
                "disposition": "preserved",
            }
        ],
        "obligations": [
            {
                "id": "consistent-read",
                "summary": "Successful mutations are reflected in subsequent queries.",
                "source_fragment_ids": [f.id for f in fragments],
                "unit_ids": ["read", "write"],
            }
        ],
        "source_accounting": [
            {
                "source_fragment_id": f.id,
                "disposition": "mapped",
                "obligation_ids": ["consistent-read"],
                "constraint_ids": ["external-ownership"],
                "rationale_sha256": "sha256:" + "f" * 64,
            }
            for f in fragments
        ],
    }
    if getattr(request, "param", None) == "null_constraints":
        data["constraints"] = None
        for record in data["source_accounting"]:
            record["constraint_ids"] = []
    elif getattr(request, "param", None) == "aliased_ownership":
        data["obligations"][0]["unit_ids"] = data["constraints"][0]["unit_ids"]
    path = units.parent / "plan.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    if getattr(request, "param", None) == "uncommitted_contracts":
        _git(tmp_path, "add", ".gitignore", "src", "source.md")
    else:
        _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "initial units")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    write_delivery_progress(
        delivery_progress_path(tmp_path, "cache"),
        DeliveryProgress(
            schema_version=1,
            plan_id="cache",
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress(name, "done", commit=commit) for name in names],
        ),
    )
    cfg = {
        "project": {"root_path": str(tmp_path), "build_tool": "python"},
        "sandbox": {"allowed_write_paths": ["src/"], "allowed_read_paths": ["."]},
        "run_build": True,
        "run_tests": True,
        "run_checks": False,
        "build": {"test_command": "python -m pytest tests/"},
    }
    assert get_delivery_status(path).valid
    result = _verify(path, cfg)
    assert result.stop_code == "delivery_verification.repair_required", result
    if asset_mode == "candidate_asset":
        (tmp_path / "assets/reference.png").unlink()
    elif asset_mode == "missing_candidate_asset":
        (tmp_path / "assets/reference.png").write_bytes(b"operator-only reference image")
    return path, cfg


def _verify(path: Path, cfg: dict, disposition: str = "repair_required") -> DeliveryVerifyResult:
    root = Path(cfg["project"]["root_path"])
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        return verify_delivery_plan(
            path,
            cfg,
            state_store=JsonStateStore(root / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(_LLM(_assessment(disposition)), cfg),
            security_reviewer=None,
            project_root=root,
        )


def _repair(path: Path, cfg: dict, llm: _LLM, **kwargs):
    return coordinate_delivery_repair(
        path,
        cfg,
        project_root=Path(cfg["project"]["root_path"]),
        agent_factory=lambda: DeliveryRepairAgent(llm),
        **kwargs,
    )


def test_repair_appends_contract_and_assembly_preserving_completed_units_and_index(
    completed_plan, tmp_path: Path
) -> None:
    path, cfg = completed_plan
    before = yaml.safe_load(path.read_text())
    progress_before = read_delivery_progress(delivery_progress_path(tmp_path, "cache"), plan_id="cache")[0]
    index = (tmp_path / ".git/index").read_bytes()
    head = _git(tmp_path, "rev-parse", "HEAD")
    llm = _LLM(_draft())
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    after = yaml.safe_load(path.read_text())
    assert after["units"][:-1] == before["units"]
    assert after["source_accounting"] == before["source_accounting"]
    assert after["constraints"][0]["unit_ids"] == ["read", "write", result.unit_id]
    assert after["obligations"][0]["unit_ids"] == ["read", "write", result.unit_id]
    assert after["units"][-1]["scope_paths"] == ["src"]
    assert after["units"][-1]["budget"] == {"max_planner_steps": 1}
    progress = read_delivery_progress(delivery_progress_path(tmp_path, "cache"), plan_id="cache")[0]
    assert progress.units == progress_before.units
    assert progress.verification is None
    assert progress.assembled_commit != progress_before.assembled_commit
    assert (
        _git(tmp_path, "show", progress.assembled_commit + ":" + after["units"][-1]["task_path"]) == _CONTRACT.strip()
    )
    assert _git(tmp_path, "rev-parse", "HEAD") == head
    assert (tmp_path / ".git/index").read_bytes() == index
    assert len(llm.calls) == 1
    assert "consistent-read" in llm.calls[0]
    assert get_delivery_status(path).status == "pending"
    again = _repair(path, cfg, llm)
    assert again.issue.code == "delivery_repair.budget_exhausted"
    assert len(llm.calls) == 1


@pytest.mark.parametrize(
    "disposition",
    [
        "external_dependency_gap",
        "scope_amendment_required",
        "human_review_required",
        "evidence_unavailable",
        "security_stop",
    ],
)
def test_authoring_stop_is_durable_and_never_publishes_or_retries(completed_plan, disposition: str) -> None:
    path, cfg = completed_plan
    before = path.read_bytes()
    llm = _LLM(_draft(disposition))
    first = _repair(path, cfg, llm)
    second = _repair(path, cfg, llm)
    assert first.issue.code == "delivery_repair." + disposition
    assert second.issue.code == "delivery_repair." + disposition
    assert path.read_bytes() == before
    assert len(llm.calls) == 1


@pytest.mark.parametrize("change", ["source", "plan", "config", "branch", "missing_input", "tampered_input"])
def test_stale_or_unavailable_input_stops_before_provider(completed_plan, tmp_path: Path, change: str) -> None:
    path, cfg = completed_plan
    if change == "source":
        (tmp_path / "source.md").write_text("New authority\n")
    elif change == "plan":
        path.write_bytes(path.read_bytes() + b"\n")
    elif change == "config":
        cfg["run_checks"] = True
    elif change == "branch":
        _git(tmp_path, "update-ref", "-d", "refs/heads/sikula/delivery/cache")
    else:
        input_path = next((tmp_path / ".sikula/state/delivery/cache").glob("repair-input-*.json"))
        if change == "missing_input":
            input_path.unlink()
        else:
            data = json.loads(input_path.read_text())
            data["assessment"]["findings"][0]["summary"] = "Changed private finding"
            input_path.write_text(json.dumps(data))
    llm = _LLM()
    result = _repair(path, cfg, llm)
    assert not result.ready
    assert result.issue
    assert not llm.calls


def test_repair_dry_run_does_not_write_state_or_invoke_provider(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    llm = _LLM()
    assert _repair(path, cfg, llm, dry_run=True).ready
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert before == after
    assert not llm.calls


def test_invalid_preparer_can_be_corrected_without_stale_repair_state(completed_plan, tmp_path: Path) -> None:
    import sikula

    path, cfg = completed_plan
    invalid_config = copy.deepcopy(cfg)
    invalid_config["agents"] = {"delivery_preparer": {"llm": {"provider": "codxe"}}}
    assert _verify(path, invalid_config).stop_code == "delivery_verification.repair_required"
    factories = []

    def invalid_factory() -> DeliveryRepairAgent:
        factories.append("codxe")
        return sikula._create_delivery_repair_agent(argparse.Namespace(), invalid_config)

    result = coordinate_delivery_repair(path, invalid_config, project_root=tmp_path, agent_factory=invalid_factory)
    assert not result.ready and result.issue is not None
    assert factories == ["codxe"]
    directory = delivery_progress_path(tmp_path, "cache").parent
    assert not (directory / "integration-repair.json").exists()
    assert not (directory / "integration-repair.jsonl").exists()

    refreshed = _resume_final_gate(path, cfg)
    assert refreshed.stop_code == "delivery_verification.repair_required"
    assert get_delivery_status(path).verification.attempt == 3
    llm = _LLM(_draft())
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    state = read_repair_state(tmp_path, directory / "integration-repair.json")
    assert state["phase"] == "published"
    assert state["attempts"] == len(llm.calls) == 1


@pytest.mark.parametrize("completed_plan", ["claude_settings", "gemini_settings"], indirect=True)
def test_rejected_provider_workspace_can_be_corrected_before_authoring(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, cfg = completed_plan
    provider = "claude" if (tmp_path / ".claude/settings.json").exists() else "gemini"
    rejected_cfg = copy.deepcopy(cfg)
    rejected_cfg["agents"] = {"delivery_preparer": {"llm": {"provider": provider}}}
    assert _verify(path, rejected_cfg).stop_code == "delivery_verification.repair_required"
    client_type = ClaudeClient if provider == "claude" else GeminiClient
    client = client_type(LLMConfig(provider=provider))
    monkeypatch.setattr(
        client, "run_readonly_agent", lambda *_: pytest.fail("workspace rejection must precede authoring")
    )
    directory = delivery_progress_path(tmp_path, "cache").parent
    plan_before = path.read_bytes()
    record = get_delivery_status(path).verification
    for _ in range(2):
        result = coordinate_delivery_repair(
            path, rejected_cfg, project_root=tmp_path, agent_factory=lambda: DeliveryRepairAgent(client)
        )
        assert not result.ready
        assert result.issue.code == "delivery_repair.evidence_unavailable"
        assert not (directory / "integration-repair.json").exists()
        assert not (directory / "integration-repair.jsonl").exists()
        assert path.read_bytes() == plan_before
        assert get_delivery_status(path).verification == record

    assert _resume_final_gate(path, cfg).stop_code == "delivery_verification.repair_required"
    llm = _LLM(_draft())
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    state = read_repair_state(tmp_path, directory / "integration-repair.json")
    assert state["phase"] == "published"
    assert state["attempts"] == len(llm.calls) == 1


@pytest.mark.parametrize("failure", ["mutation", "mutation_then_error", "reported_violation"])
def test_workspace_preparation_retains_terminal_readonly_stops(
    completed_plan: tuple[Path, dict], tmp_path: Path, failure: str
) -> None:
    path, cfg = completed_plan
    prepared = []

    class WorkspaceLLM(_LLM):
        def prepare_readonly_agent_workspace(self, cwd: Path) -> None:
            prepared.append(cwd)
            if failure == "reported_violation":
                raise LLMReadOnlyViolation("Private workspace detail")
            (cwd / "src/cache.py").write_text("modified\n", encoding="utf-8")
            if failure == "mutation_then_error":
                raise LLMConfigurationError("Private workspace detail")

    llm = WorkspaceLLM()
    for dry_run in (False, True, False):
        result = _repair(path, cfg, llm, dry_run=dry_run)
        assert result.issue.code == "delivery_repair.readonly_mutation"
        assert "Private workspace" not in result.issue.message
    state = read_repair_state(tmp_path, delivery_progress_path(tmp_path, "cache").parent / "integration-repair.json")
    assert state["phase"] == "blocked" and state["attempts"] == 0
    assert len(prepared) == 1
    assert not llm.calls
    assert (tmp_path / "src/cache.py").read_text() == "cache = {}\n"


@pytest.mark.parametrize("provider", ["claude", "gemini"])
def test_provider_owned_workspace_setup_allows_repair_authoring(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    path, cfg = completed_plan
    cfg = copy.deepcopy(cfg)
    cfg["agents"] = {"delivery_preparer": {"llm": {"provider": provider}}}
    assert _verify(path, cfg).stop_code == "delivery_verification.repair_required"
    client_type = ClaudeClient if provider == "claude" else GeminiClient
    client = client_type(LLMConfig(provider=provider))
    llm = _LLM(_draft())
    monkeypatch.setattr(client, "run_readonly_agent", llm.run_readonly_agent)
    result = coordinate_delivery_repair(
        path, cfg, project_root=tmp_path, agent_factory=lambda: DeliveryRepairAgent(client)
    )
    assert result.ready, result.issue
    state = read_repair_state(tmp_path, delivery_progress_path(tmp_path, "cache").parent / "integration-repair.json")
    assert state["phase"] == "published"
    assert state["attempts"] == len(llm.calls) == 1


def _resume_final_gate(path: Path, cfg: dict) -> DeliveryRunResult:
    from sikula_cli.delivery import DeliveryRunNextContext, _finalize_delivery_run

    root = Path(cfg["project"]["root_path"])
    context = DeliveryRunNextContext(
        run_task=lambda args, config: 0,
        resolve_state_dir=lambda config: root / ".sikula/state",
        state_store=JsonStateStore(root / ".sikula/state"),
        verify_plan=lambda args, config: _verify(path, config),
    )
    return _finalize_delivery_run(
        argparse.Namespace(plan_file=str(path)),
        status=get_delivery_status(path),
        project_root=root,
        max_units=1,
        max_elapsed_minutes=None,
        units_attempted=0,
        units_succeeded=0,
        last_unit=None,
        child_task_id=None,
        cfg=cfg,
        context=context,
    )


@pytest.mark.parametrize("field,value", [("model", "repair-model"), ("agent_timeout", 900)])
def test_preparer_policy_change_refreshes_gate_only_before_authoring(
    completed_plan: tuple[Path, dict], tmp_path: Path, field: str, value: str | int
) -> None:
    from sikula_cli.delivery import _preview_delivery_run

    path, cfg = completed_plan
    original = get_delivery_status(path).verification
    changed = copy.deepcopy(cfg)
    changed["agents"] = {"delivery_preparer": {"llm": {field: value}}}
    assert with_delivery_verification_readiness(get_delivery_status(path), cfg).verification_status == "failed"
    assert with_delivery_verification_readiness(get_delivery_status(path), changed).verification_status == "stale"
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = _preview_delivery_run(argparse.Namespace(plan_file=str(path)), changed, project_root=tmp_path)
    assert preview.ready
    after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    assert after == before
    assert _resume_final_gate(path, changed).stop_code == "delivery_verification.repair_required"
    current = get_delivery_status(path).verification
    assert current.gate_id == original.gate_id
    assert current.attempt == original.attempt + 1
    assert current.repair_input_fingerprint != original.repair_input_fingerprint
    assert _repair(path, changed, _LLM(_draft())).ready


@pytest.mark.parametrize("boundary", ["authoring", "blocked", "missing_input", "tampered_input"])
def test_policy_refresh_cannot_replace_started_repair_or_unavailable_evidence(
    completed_plan: tuple[Path, dict], tmp_path: Path, boundary: str
) -> None:
    path, cfg = completed_plan
    if boundary in {"authoring", "blocked"}:
        output = RuntimeError("provider failed") if boundary == "authoring" else _draft("external_dependency_gap")
        assert not _repair(path, cfg, _LLM(output)).ready
    else:
        input_path = next(delivery_progress_path(tmp_path, "cache").parent.glob("repair-input-*.json"))
        if boundary == "missing_input":
            input_path.unlink()
        else:
            payload = json.loads(input_path.read_text())
            payload["repair_policy_fingerprint"] = "sha256:" + "0" * 64
            input_path.write_text(json.dumps(payload))
    changed = copy.deepcopy(cfg)
    changed["agents"] = {"delivery_preparer": {"llm": {"model": "new-model"}}}
    record = get_delivery_status(path).verification
    assert with_delivery_verification_readiness(get_delivery_status(path), changed).verification_status == "failed"
    assert _resume_final_gate(path, changed).stop_code == "delivery_verification.repair_required"
    assert get_delivery_status(path).verification == record
    llm = _LLM()
    result = _repair(path, changed, llm)
    expected = {
        "authoring": "stale",
        "blocked": "external_dependency_gap",
        "missing_input": "evidence_unavailable",
        "tampered_input": "evidence_unavailable",
    }
    assert result.issue.code == "delivery_repair." + expected[boundary]
    assert not llm.calls


@pytest.mark.parametrize("completed_plan", ["candidate_asset"], indirect=True)
@pytest.mark.parametrize("interruption", [None, "plan", "assembly"])
def test_candidate_assets_survive_operator_absence_and_publication_resume(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interruption: str | None
) -> None:
    path, cfg = completed_plan
    assert not (tmp_path / "assets/reference.png").exists()
    llm = _LLM(_draft())
    assert _repair(path, cfg, llm, dry_run=True).ready
    assert not llm.calls
    if interruption:
        name = "_atomic_replace_if_unchanged" if interruption == "plan" else "assemble_delivery_artifacts"
        original = getattr(repair_module, name)

        def interrupt(*args, **kwargs):
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        monkeypatch.setattr(repair_module, name, interrupt)
        with pytest.raises(KeyboardInterrupt):
            _repair(path, cfg, llm)
        monkeypatch.setattr(repair_module, name, original)
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert _repair(path, cfg, llm, dry_run=True).ready
        after = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        assert before == after
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    state = read_repair_state(tmp_path, delivery_progress_path(tmp_path, "cache").parent / "integration-repair.json")
    assert state["phase"] == "published"
    assert state["attempts"] == len(llm.calls) == 1
    assert "Reference asset: `assets/reference.png`" in state["task_markdown"]
    assert not (tmp_path / "assets/reference.png").exists()


@pytest.mark.parametrize("completed_plan", ["missing_candidate_asset"], indirect=True)
def test_operator_only_asset_blocks_before_reserving_any_attempt(
    completed_plan: tuple[Path, dict], tmp_path: Path
) -> None:
    path, cfg = completed_plan
    assert (tmp_path / "assets/reference.png").exists()
    llm = _LLM()
    for dry_run in (True, False, False):
        result = _repair(path, cfg, llm, dry_run=dry_run)
        assert not result.ready
        assert result.issue.code == "delivery_repair.contract_not_ready"
    directory = delivery_progress_path(tmp_path, "cache").parent
    assert not (directory / "integration-repair.json").exists()
    assert not (directory / "integration-repair.jsonl").exists()
    assert not llm.calls


@pytest.mark.parametrize(
    "completed_plan,limit",
    [("at_unit_limit", "units"), (None, "plan_bytes"), (None, "packet_bytes")],
    indirect=["completed_plan"],
)
def test_enlarged_plan_limits_block_readiness_without_consuming_attempts(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, limit: str
) -> None:
    from core.delivery_verification import check_delivery_verification_readiness

    path, cfg = completed_plan
    readiness = check_delivery_verification_readiness(get_delivery_status(path), cfg)
    assert readiness.ready
    if limit == "units":
        assert readiness.active_unit_count == 256
    else:
        constant = (
            "MAX_DELIVERY_VERIFICATION_PLAN_BYTES"
            if limit == "plan_bytes"
            else "MAX_DELIVERY_VERIFICATION_PACKET_BYTES"
        )
        monkeypatch.setattr("core.delivery_verification." + constant, getattr(readiness, limit))
    assert check_delivery_verification_readiness(get_delivery_status(path), cfg).ready
    plan_before = path.read_bytes()
    progress_before = delivery_progress_path(tmp_path, "cache").read_bytes()
    head_before = _git(tmp_path, "rev-parse", "sikula/delivery/cache")

    for dry_run in (True, False, False):
        result = coordinate_delivery_repair(
            path,
            cfg,
            project_root=tmp_path,
            agent_factory=lambda: pytest.fail("an unverifiable repair must not construct a provider"),
            dry_run=dry_run,
        )
        assert not result.ready
        assert result.issue.code == "delivery_repair.hierarchy_required"
        assert path.read_bytes() == plan_before
        assert delivery_progress_path(tmp_path, "cache").read_bytes() == progress_before
        assert _git(tmp_path, "rev-parse", "sikula/delivery/cache") == head_before
        directory = delivery_progress_path(tmp_path, "cache").parent
        assert not (directory / "integration-repair.json").exists()
        assert not (directory / "integration-repair.jsonl").exists()
        assert not list((path.parent / "units").glob("integration-repair-*.md"))


@pytest.mark.parametrize("completed_plan", ["below_unit_limit"], indirect=True)
def test_repair_can_fill_the_last_available_final_gate_unit(completed_plan: tuple[Path, dict]) -> None:
    from core.delivery_verification import check_delivery_verification_readiness

    path, cfg = completed_plan
    llm = _LLM(_draft())
    assert _repair(path, cfg, llm, dry_run=True).ready
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    readiness = check_delivery_verification_readiness(get_delivery_status(path), cfg)
    assert readiness.ready
    assert readiness.active_unit_count == 256
    assert len(llm.calls) == 1


def test_elapsed_limit_does_not_hide_unverifiable_repair(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sikula_cli.delivery import DeliveryRunNextContext, _run_delivery_plan

    path, cfg = completed_plan
    monkeypatch.setattr("core.delivery_verification.MAX_DELIVERY_VERIFICATION_ACTIVE_UNITS", 2)
    elapsed = iter([0.0])
    monkeypatch.setattr("sikula_cli.delivery.time.monotonic", lambda: next(elapsed, 61.0))
    context = DeliveryRunNextContext(
        run_task=lambda *_: pytest.fail("an unverifiable repair must not execute a child"),
        resolve_state_dir=lambda _: tmp_path / ".sikula/state",
        state_store=JsonStateStore(tmp_path / ".sikula/state"),
        repair_agent_factory=lambda *_: pytest.fail("an unverifiable repair must not construct a provider"),
    )
    args = argparse.Namespace(plan_file=str(path), max_units=None, max_elapsed_minutes=1, reset_failed=False)
    result = _run_delivery_plan(args, cfg, context, project_root=tmp_path)
    assert result.stop_code == "delivery_repair.hierarchy_required"
    assert not result.ready and not result.succeeded
    assert result.units_attempted == 0
    assert not (delivery_progress_path(tmp_path, "cache").parent / "integration-repair.json").exists()


@pytest.mark.parametrize("completed_plan", ["oversized_prompt"], indirect=True)
def test_escaped_repair_prompt_blocks_before_readiness_or_authoring_state(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    contracts = [(path.parent / "units" / (name + ".md")).read_text(encoding="utf-8") for name in ("read", "write")]
    assert all(len(text.encode("utf-8")) < 128 * 1024 for text in contracts)
    assert sum(len(text.encode("utf-8")) for text in contracts) < 512 * 1024
    assert len(json.dumps(contracts, ensure_ascii=True).encode("utf-8")) > 512 * 1024
    before = path.read_bytes()
    llm = _LLM()
    for dry_run in (True, False, False, True):
        result = _repair(path, cfg, llm, dry_run=dry_run)
        assert not result.ready
        assert result.issue.code == "delivery_repair.hierarchy_required"
        assert not llm.calls
        directory = delivery_progress_path(tmp_path, "cache").parent
        assert not (directory / "integration-repair.json").exists()
        assert not (directory / "integration-repair.jsonl").exists()
        assert path.read_bytes() == before


def test_oversized_correction_prompt_does_not_reserve_another_attempt(
    completed_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from core.delivery_verification import MAX_DELIVERY_VERIFICATION_PACKET_BYTES

    path, cfg = completed_plan
    original = repair_module._repair_packet

    def near_limit_packet(*args, **kwargs):
        packet, unit, contracts = original(*args, **kwargs)
        packet["context_padding"] = ""
        padding_bytes = MAX_DELIVERY_VERIFICATION_PACKET_BYTES - len(
            build_delivery_repair_prompt(packet).encode("utf-8")
        )
        assert padding_bytes > 0
        packet["context_padding"] = "x" * padding_bytes
        return packet, unit, contracts

    monkeypatch.setattr(repair_module, "_repair_packet", near_limit_packet)
    llm = _LLM("invalid", _draft())
    result = _repair(path, cfg, llm)
    assert result.issue.code == "delivery_repair.hierarchy_required"
    directory = delivery_progress_path(tmp_path, "cache").parent
    state = read_repair_state(tmp_path, directory / "integration-repair.json")
    assert state["phase"] == "authoring"
    assert state["attempts"] == len(llm.calls) == 1
    assert len(llm.calls[0].encode("utf-8")) == MAX_DELIVERY_VERIFICATION_PACKET_BYTES
    records = [json.loads(line) for line in (directory / "integration-repair.jsonl").read_text().splitlines()]
    assert [record["event"] for record in records] == ["authoring_started", "authoring_finished"]
    assert records[-1]["output"] == "invalid"
    assert records[-1]["attempt"] == 1


def test_repair_agent_independently_rejects_oversized_prompt(tmp_path: Path) -> None:
    llm = _LLM()
    records = []
    with pytest.raises(DeliveryRepairAuthoringError) as exc_info:
        DeliveryRepairAgent(llm).author(
            cwd=tmp_path, packet={"unit_contracts": {"owner": "é" * 100_000}}, audit_recorder=records.append
        )
    assert exc_info.value.code == "delivery_repair.hierarchy_required"
    assert not records and not llm.calls


def test_interrupted_authoring_budget_is_persistent(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    llm = _LLM(KeyboardInterrupt(), KeyboardInterrupt())
    for _ in range(2):
        with pytest.raises(KeyboardInterrupt):
            _repair(path, cfg, llm)
    result = _repair(path, cfg, llm)
    assert result.issue.code == "delivery_repair.authoring_budget_exhausted"
    assert len(llm.calls) == 2
    audit = (tmp_path / ".sikula/state/delivery/cache/integration-repair.jsonl").read_text()
    assert audit.count('"event": "authoring_finished"') == 2
    assert "delivery_repair.interrupted" in audit


@pytest.mark.parametrize("interruption", ["task", "plan", "assembly", "progress"])
def test_publication_resume_does_not_duplicate_work(
    completed_plan, tmp_path: Path, monkeypatch, interruption: str
) -> None:
    path, cfg = completed_plan
    functions = {
        "task": "_atomic_write_new",
        "plan": "_atomic_replace_if_unchanged",
        "assembly": "assemble_delivery_artifacts",
        "progress": "write_delivery_progress",
    }
    name = functions[interruption]
    original = getattr(repair_module, name)

    def interrupt(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    llm = _LLM(_draft())
    monkeypatch.setattr(repair_module, name, interrupt)
    with pytest.raises(KeyboardInterrupt):
        _repair(path, cfg, llm)
    monkeypatch.setattr(repair_module, name, original)
    preview = preview_delivery_run_next(path, project_root=tmp_path)
    assert not preview.ready
    assert any(issue.code == "delivery_repair.pending" for issue in preview.errors)
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    assert len(llm.calls) == 1
    assert len(yaml.safe_load(path.read_text())["units"]) == 3
    history = _git(tmp_path, "log", "--format=%s", "sikula/delivery/cache")
    assert history.count("sikula: apply delivery amendment integration-repair-") == 1


@pytest.mark.parametrize("invocation_directory", ["parent", "sibling"])
@pytest.mark.parametrize("interruption", [None, "plan", "assembly"], ids=["complete", "plan", "assembly"])
def test_relative_plan_path_publishes_and_resumes(
    completed_plan: tuple[Path, dict],
    tmp_path: Path,
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    invocation_directory: str,
    interruption: str | None,
) -> None:
    path, cfg = completed_plan
    cwd = tmp_path / "src" if invocation_directory == "parent" else tmp_path_factory.mktemp("operator")
    relative_path = Path(os.path.relpath(path, cwd))
    assert ".." in relative_path.parts
    monkeypatch.chdir(cwd)
    llm = _LLM(_draft())
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    preview = _repair(relative_path, cfg, llm, dry_run=True)
    assert preview.ready, preview.issue
    assert not llm.calls
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before

    if interruption is not None:
        name = "_atomic_replace_if_unchanged" if interruption == "plan" else "assemble_delivery_artifacts"
        original = getattr(repair_module, name)

        def interrupt(*args, **kwargs) -> None:
            original(*args, **kwargs)
            raise KeyboardInterrupt()

        with monkeypatch.context() as interrupted:
            interrupted.setattr(repair_module, name, interrupt)
            with pytest.raises(KeyboardInterrupt):
                _repair(relative_path, cfg, llm)
        prepared = read_repair_state(
            tmp_path, delivery_progress_path(tmp_path, "cache").with_name("integration-repair.json")
        )
        assert prepared["phase"] == "prepared"
        before_resume = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        preview = _repair(relative_path, cfg, llm, dry_run=True)
        assert preview.ready, preview.issue
        assert len(llm.calls) == 1
        assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before_resume

    result = _repair(relative_path, cfg, llm)
    assert result.ready, result.issue
    assert len(llm.calls) == 1
    plan = yaml.safe_load(path.read_text())
    assert len(plan["units"]) == 3
    unit = plan["units"][-1]
    expected = path.parent / "units" / (result.unit_id + ".md")
    assert unit["task_path"] == expected.relative_to(tmp_path).as_posix()
    assert expected.read_text() == _CONTRACT
    assert _git(tmp_path, "show", "sikula/delivery/cache:" + unit["task_path"]) == _CONTRACT.strip()
    history = _git(tmp_path, "log", "--format=%s", "sikula/delivery/cache")
    assert history.count("sikula: apply delivery amendment integration-repair-") == 1
    assert (tmp_path / ".git/index").read_bytes() == before[Path(".git/index")]
    assert get_delivery_status(relative_path, project_root=tmp_path).status == "pending"


def test_repair_input_is_private_control_data_separate_from_public_projections(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    status = get_delivery_status(path)
    public = json.dumps(status.to_dict())
    assert "Updates must invalidate cached values." not in public
    input_path = next((tmp_path / ".sikula/state/delivery/cache").glob("repair-input-*.json"))
    assert "Updates must invalidate cached values." in input_path.read_text()
    if os.name != "nt":
        assert input_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    "output",
    [
        '{"schema_version":true,"disposition":"repair","task_markdown":"text"}',
        '{"schema_version":1,"disposition":"repair","task_markdown":"\\ud800"}',
        _draft() + " trailing",
        '{"schema_version":1,"schema_version":1,"disposition":"repair","task_markdown":"text"}',
    ],
)
def test_repair_parser_rejects_malformed_output(output: str) -> None:
    with pytest.raises(DeliveryRepairAuthoringError):
        parse_delivery_repair_draft(output)


def test_private_state_rejects_symlink_parents_and_files(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(OSError):
        write_repair_state(tmp_path, link / "state.json", {"secret": "private"})
    with pytest.raises(OSError):
        read_repair_state(tmp_path, link / "state.json")
    assert list(target.iterdir()) == []


def test_malformed_output_is_audited_and_corrected_once(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    llm = _LLM('{"schema_version":1,"disposition":"repair","task_markdown":"\\ud800"}', _draft())
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    assert len(llm.calls) == 2
    records = [
        json.loads(line)
        for line in (tmp_path / ".sikula/state/delivery/cache/integration-repair.jsonl").read_text().splitlines()
    ]
    finished = [record for record in records if record["event"] == "authoring_finished"]
    assert [record["attempt"] for record in finished] == [1, 2]
    assert finished[0]["error"] == "delivery_repair.output_invalid"
    assert finished[0]["output"].endswith('"\\ud800"}')


def test_uncovered_validation_is_corrected_before_publication(completed_plan) -> None:
    path, cfg = completed_plan
    llm = _LLM(_draft(markdown=_CONTRACT.replace("python -m pytest tests/", "python -m pytest external/")), _draft())
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    assert len(llm.calls) == 2
    assert "enabled validation" in llm.calls[1]


def test_following_repair_prompt_sections_produces_a_ready_contract(completed_plan: tuple[Path, dict]) -> None:
    path, cfg = completed_plan

    class SectionFollowingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            instructions = prompt.split("Authoritative bounded packet:", 1)[0]
            sections = [section for section in _CONTRACT.split("\n## ")[1:] if section.splitlines()[0] in instructions]
            self.outputs.append(_draft(markdown="# Cache consistency\n\n## " + "\n## ".join(sections)))
            return super().run_readonly_agent(prompt, cwd)

    llm = SectionFollowingLLM()
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    assert len(llm.calls) == 1


def test_missing_reviewer_focus_gets_actionable_correction(completed_plan: tuple[Path, dict]) -> None:
    path, cfg = completed_plan
    incomplete = (
        _CONTRACT.split("## Reviewer focus", 1)[0] + "## Verification" + _CONTRACT.split("## Verification", 1)[1]
    )

    class CorrectingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            packet = json.loads(prompt.split("Authoritative bounded packet:\n", 1)[1])
            corrected = "reviewer focus" in packet.get("previous_error", "")
            self.outputs.append(_draft(markdown=_CONTRACT if corrected else incomplete))
            return super().run_readonly_agent(prompt, cwd)

    llm = CorrectingLLM()
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    assert len(llm.calls) == 2


@pytest.mark.parametrize("completed_plan", ["null_constraints", "aliased_ownership"], indirect=True)
@pytest.mark.parametrize("interrupted", [False, True])
def test_optional_and_aliased_ownership_publishes_and_resumes_without_reauthoring(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, interrupted: bool
) -> None:
    path, cfg = completed_plan
    before = yaml.safe_load(path.read_text(encoding="utf-8"))
    if before["constraints"] is not None:
        assert before["constraints"][0]["unit_ids"] is before["obligations"][0]["unit_ids"]
    llm = _LLM(_draft())
    original = repair_module._atomic_replace_if_unchanged

    def interrupt(*args, **kwargs) -> None:
        original(*args, **kwargs)
        raise KeyboardInterrupt()

    if interrupted:
        monkeypatch.setattr(repair_module, "_atomic_replace_if_unchanged", interrupt)
        with pytest.raises(KeyboardInterrupt):
            _repair(path, cfg, llm)
        monkeypatch.setattr(repair_module, "_atomic_replace_if_unchanged", original)
    result = _repair(path, cfg, llm)
    assert result.ready, result.issue
    after = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert after["units"][:-1] == before["units"]
    assert after["source_accounting"] == before["source_accounting"]
    if before["constraints"] is None:
        assert after["constraints"] is None
    else:
        assert after["constraints"][0]["unit_ids"] == [*before["constraints"][0]["unit_ids"], result.unit_id]
    assert after["obligations"][0]["unit_ids"] == [*before["obligations"][0]["unit_ids"], result.unit_id]
    assert get_delivery_status(path).valid
    state = read_repair_state(tmp_path, tmp_path / ".sikula/state/delivery/cache/integration-repair.json")
    assert state["phase"] == "published"
    assert state["attempts"] == len(llm.calls) == 1


@pytest.mark.parametrize("change", ["source", "contract", "terminal_gate"])
def test_changed_authority_blocks_correction_before_second_call(
    completed_plan: tuple[Path, dict], tmp_path: Path, change: str
) -> None:
    path, cfg = completed_plan

    class ChangedAuthorityLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            if not self.calls:
                if change == "source":
                    (tmp_path / "source.md").write_text("Changed source authority\n", encoding="utf-8")
                elif change == "contract":
                    (path.parent / "units/read.md").write_text(_CONTRACT + "\nChanged contract\n", encoding="utf-8")
                else:
                    progress_path = delivery_progress_path(tmp_path, "cache")
                    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
                    verification = replace(
                        progress.verification, stop_code="delivery_verification.external_dependency_gap"
                    )
                    write_delivery_progress(progress_path, replace(progress, verification=verification))
            return super().run_readonly_agent(prompt, cwd)

    llm = ChangedAuthorityLLM("{}", _draft())
    result = _repair(path, cfg, llm)
    assert result.issue is not None
    assert len(llm.calls) == 1
    assert len(yaml.safe_load(path.read_text())["units"]) == 2


@pytest.mark.parametrize("interrupted", [False, True])
def test_workspace_mutation_retains_terminal_stop(completed_plan, tmp_path: Path, interrupted: bool) -> None:
    path, cfg = completed_plan

    class MutatingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            (cwd / "src/cache.py").write_text("modified\n")
            return super().run_readonly_agent(prompt, cwd)

    llm = MutatingLLM(KeyboardInterrupt() if interrupted else _draft())
    for _ in range(2):
        result = _repair(path, cfg, llm)
        assert result.issue.code == "delivery_repair.readonly_mutation"
    assert len(llm.calls) == 1
    assert len(yaml.safe_load(path.read_text())["units"]) == 2
    assert (tmp_path / "src/cache.py").read_text() == "cache = {}\n"


def test_provider_readonly_violation_retains_terminal_stop_and_audit(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    before = path.read_bytes()
    llm = _LLM(LLMReadOnlyViolation("Private provider workspace diagnostic"), _draft())
    for dry_run in (False, True, False):
        result = _repair(path, cfg, llm, dry_run=dry_run)
        assert result.issue.code == "delivery_repair.readonly_mutation"
        assert "Private provider" not in result.issue.message
    state = read_repair_state(tmp_path, tmp_path / ".sikula/state/delivery/cache/integration-repair.json")
    assert state["phase"] == "blocked"
    assert state["attempts"] == len(llm.calls) == 1
    assert path.read_bytes() == before
    records = [
        json.loads(line)
        for line in (tmp_path / ".sikula/state/delivery/cache/integration-repair.jsonl").read_text().splitlines()
    ]
    finished = [record for record in records if record["event"] == "authoring_finished"]
    assert len(finished) == 1
    assert finished[0]["error"] == "delivery_repair.readonly_mutation"
    assert finished[0]["prompt"] == llm.calls[0]
    assert finished[0]["output"] is None


@pytest.mark.parametrize("dry_run", [False, True])
def test_changed_contract_is_rejected_before_authoring(completed_plan, tmp_path: Path, dry_run: bool) -> None:
    path, cfg = completed_plan
    (path.parent / "units/read.md").write_text(_CONTRACT + "\nChanged contract\n")
    llm = _LLM()
    result = _repair(path, cfg, llm, dry_run=dry_run)
    assert result.issue.code == "delivery_repair.contract_evidence_changed"
    assert not llm.calls


@pytest.mark.parametrize("blocker", ["task_conflict", "scope_unavailable"])
def test_dry_run_checks_repair_destination_and_owner_scope(completed_plan, tmp_path: Path, blocker: str) -> None:
    path, cfg = completed_plan
    store = JsonStateStore(tmp_path / ".sikula/state")
    progress_path = delivery_progress_path(tmp_path, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    if blocker == "task_conflict":
        unit_id = "integration-repair-" + progress.verification.gate_id[-16:]
        (path.parent / "units" / (unit_id + ".md")).write_text("Occupied destination\n", encoding="utf-8")
    else:
        linked = []
        scope = resolve_delivery_write_scope(project_root=tmp_path, configured_write_paths=[], unit_scope_paths=[])
        for unit in progress.units:
            child = TaskState(
                task_id="child-" + unit.unit_id,
                task_description=_CONTRACT.strip(),
                done=True,
                result_commit=unit.commit,
                delivery_plan_id="cache",
                delivery_unit_id=unit.unit_id,
                delivery_write_scope_schema_version=scope.schema_version,
                delivery_write_scope_mode=scope.mode,
                delivery_declared_write_paths=[],
                delivery_declared_write_exact_file_paths=[],
                delivery_effective_write_paths=[],
                delivery_effective_write_exact_file_paths=[],
            )
            store.save(child)
            linked.append(replace(unit, child_task_id=child.task_id))
        write_delivery_progress(progress_path, replace(progress, units=linked))
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    llm = _LLM()
    preview = _repair(path, cfg, llm, state_store=store, dry_run=True)
    assert not preview.ready
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    result = _repair(path, cfg, llm, state_store=store)
    assert not result.ready
    assert result.issue.code == preview.issue.code
    if blocker == "task_conflict":
        assert result.issue.code == "delivery_repair.task_conflict"
    assert not llm.calls
    assert not (tmp_path / ".sikula/state/delivery/cache/integration-repair.json").exists()


def _link_execution_evidence(path: Path, tmp_path: Path, *, description: str = _CONTRACT.strip()) -> JsonStateStore:
    store = JsonStateStore(tmp_path / ".sikula/state")
    progress_path = delivery_progress_path(tmp_path, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    linked = []
    for unit in progress.units:
        child = TaskState(
            task_id="child-" + unit.unit_id,
            task_description=description,
            done=True,
            result_commit=unit.commit,
            delivery_plan_id="cache",
            delivery_unit_id=unit.unit_id,
            delivery_plan_path=path.relative_to(tmp_path).as_posix(),
        )
        store.save(child)
        linked.append(replace(unit, child_task_id=child.task_id))
    write_delivery_progress(progress_path, replace(progress, units=linked))
    return store


@pytest.fixture
def completed_handoff_plan(completed_plan, tmp_path: Path) -> tuple[Path, dict, JsonStateStore]:
    path, cfg = completed_plan
    store = _link_execution_evidence(path, tmp_path)
    status = get_delivery_status(path)
    progress_path = delivery_progress_path(tmp_path, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    handoffs = {}
    for unit in status.units:
        child = store.load(unit.child_task_id)
        child.delivery_handoff_schema_version = 1
        store.save(child)
        handoff = build_delivery_unit_handoff(
            plan_id="cache", selected_unit=unit, child_task_id=child.task_id, child_state=child
        )
        write_delivery_unit_handoff(delivery_unit_handoff_path(tmp_path, "cache", unit.id), handoff)
        handoffs[unit.id] = handoff
    write_delivery_progress(
        progress_path,
        replace(
            progress,
            units=[
                replace(unit, handoff_schema_version=1, handoff_fingerprint=handoffs[unit.unit_id].fingerprint)
                for unit in progress.units
            ],
        ),
    )
    assert _verify(path, cfg).stop_code == "delivery_verification.repair_required"
    return path, cfg, store


@pytest.mark.parametrize(
    "damage,expected_code",
    [
        ("missing", "missing"),
        ("malformed", "invalid"),
        ("tampered", "invalid"),
        ("mismatched", "mismatch"),
        ("symlink", "invalid"),
        ("schema", "schema_unsupported"),
    ],
)
def test_repair_requires_dependency_handoffs_before_readiness_or_authoring(
    completed_handoff_plan, tmp_path: Path, damage: str, expected_code: str
) -> None:
    path, cfg, store = completed_handoff_plan
    handoff_path = delivery_unit_handoff_path(tmp_path, "cache", "read")
    original_handoff = handoff_path.read_bytes()
    progress_path = delivery_progress_path(tmp_path, "cache")
    original_progress = progress_path.read_bytes()
    if damage == "missing":
        handoff_path.unlink()
    elif damage == "malformed":
        handoff_path.write_text("private malformed handoff", encoding="utf-8")
    elif damage == "tampered":
        data = json.loads(original_handoff)
        data["files_changed"] = ["src/changed.py"]
        handoff_path.write_text(json.dumps(data), encoding="utf-8")
    elif damage == "mismatched":
        handoff_path.write_bytes(delivery_unit_handoff_path(tmp_path, "cache", "write").read_bytes())
    elif damage == "symlink":
        target = tmp_path / "handoff-target.json"
        target.write_bytes(original_handoff)
        handoff_path.unlink()
        try:
            handoff_path.symlink_to(target)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        progress, _ = read_delivery_progress(progress_path, plan_id="cache")
        write_delivery_progress(
            progress_path,
            replace(progress, units=[replace(unit, handoff_schema_version=2) for unit in progress.units]),
        )
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    llm = _LLM(_draft())
    preview = _repair(path, cfg, llm, state_store=store, dry_run=True)
    assert not preview.ready
    assert preview.issue.code == "delivery.dependency_handoff_" + expected_code
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    result = _repair(path, cfg, llm, state_store=store)
    assert result.issue == preview.issue
    assert not result.ready and not llm.calls
    directory = progress_path.parent
    assert not (directory / "integration-repair.json").exists()
    assert not (directory / "integration-repair.jsonl").exists()
    assert path.read_bytes() == before[path.relative_to(tmp_path)]

    if handoff_path.is_symlink():
        handoff_path.unlink()
    handoff_path.write_bytes(original_handoff)
    progress_path.write_bytes(original_progress)
    result = _repair(path, cfg, llm, state_store=store)
    assert result.ready, result.issue
    assert len(llm.calls) == 1


@pytest.mark.parametrize("loss_at", ["workspace", "correction"])
def test_repair_rechecks_handoffs_before_each_authoring_attempt(
    completed_handoff_plan, tmp_path: Path, loss_at: str
) -> None:
    path, cfg, store = completed_handoff_plan
    handoff_path = delivery_unit_handoff_path(tmp_path, "cache", "read")
    original = handoff_path.read_bytes()

    class LosingHandoffLLM(_LLM):
        def prepare_readonly_agent_workspace(self, cwd: Path) -> None:
            if loss_at == "workspace" and handoff_path.exists():
                handoff_path.unlink()

        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            output = super().run_readonly_agent(prompt, cwd)
            if output == "invalid":
                handoff_path.unlink()
            return output

    llm = LosingHandoffLLM("invalid", _draft()) if loss_at == "correction" else LosingHandoffLLM(_draft())
    result = _repair(path, cfg, llm, state_store=store)
    assert result.issue.code == "delivery.dependency_handoff_missing"
    attempts = 1 if loss_at == "correction" else 0
    state_path = delivery_progress_path(tmp_path, "cache").with_name("integration-repair.json")
    state = read_repair_state(tmp_path, state_path)
    if attempts:
        assert state["phase"] == "authoring" and state["attempts"] == attempts
    else:
        assert state is None
    for dry_run in (True, False):
        blocked = _repair(path, cfg, llm, state_store=store, dry_run=dry_run)
        assert blocked.issue.code == result.issue.code
        assert len(llm.calls) == attempts
        assert read_repair_state(tmp_path, state_path) == state

    handoff_path.write_bytes(original)
    # Restore the prerequisite and use the same effective provider configuration.
    resumed = _LLM(_draft())
    result = _repair(path, cfg, resumed, state_store=store)
    assert result.ready, result.issue
    assert len(resumed.calls) == 1
    assert read_repair_state(tmp_path, state_path)["attempts"] == attempts + 1


@pytest.mark.parametrize("loss_at", ["authoring", "prepared", "assembly"])
def test_repair_requires_handoffs_through_publication_and_resume(
    completed_handoff_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, loss_at: str
) -> None:
    path, cfg, store = completed_handoff_plan
    handoff_path = delivery_unit_handoff_path(tmp_path, "cache", "read")
    original = handoff_path.read_bytes()

    class LosingHandoffLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            output = super().run_readonly_agent(prompt, cwd)
            if loss_at == "authoring":
                handoff_path.unlink()
            return output

    llm = LosingHandoffLLM(_draft())
    with monkeypatch.context() as interrupted:
        if loss_at == "prepared":
            replace_plan = repair_module._atomic_replace_if_unchanged

            def interrupt(*args, **kwargs) -> None:
                replace_plan(*args, **kwargs)
                raise KeyboardInterrupt()

            interrupted.setattr(repair_module, "_atomic_replace_if_unchanged", interrupt)
            with pytest.raises(KeyboardInterrupt):
                _repair(path, cfg, llm, state_store=store)
            handoff_path.unlink()
        else:
            if loss_at == "assembly":
                assemble = repair_module.assemble_delivery_artifacts

                def lose_handoff(*args, **kwargs):
                    result = assemble(*args, **kwargs)
                    handoff_path.unlink()
                    return result

                interrupted.setattr(repair_module, "assemble_delivery_artifacts", lose_handoff)
            result = _repair(path, cfg, llm, state_store=store)
            assert result.issue.code == "delivery.dependency_handoff_missing"

    state_path = delivery_progress_path(tmp_path, "cache").with_name("integration-repair.json")
    state = read_repair_state(tmp_path, state_path)
    assert state["phase"] == "prepared" and state["attempts"] == 1
    for dry_run in (True, False):
        before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
        blocked = _repair(path, cfg, llm, state_store=store, dry_run=dry_run)
        assert not blocked.ready and blocked.issue.code == "delivery.dependency_handoff_missing"
        assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
        assert len(llm.calls) == 1

    handoff_path.write_bytes(original)
    result = _repair(path, cfg, llm, state_store=store)
    assert result.ready, result.issue
    assert len(llm.calls) == 1
    assert read_repair_state(tmp_path, state_path)["phase"] == "published"
    assert len(yaml.safe_load(path.read_text())["units"]) == 3


@pytest.mark.parametrize("completed_plan", ["uncommitted_contracts"], indirect=True)
@pytest.mark.parametrize("change", ["unchanged", "contract", "missing_child", "missing_description"])
def test_uncommitted_contract_requires_execution_evidence(completed_plan, tmp_path: Path, change: str) -> None:
    path, cfg = completed_plan
    store = JsonStateStore(tmp_path / ".sikula/state")
    if change != "missing_child":
        store = _link_execution_evidence(
            path, tmp_path, description="" if change == "missing_description" else _CONTRACT.strip()
        )
    if change == "contract":
        (path.parent / "units/read.md").write_text(_CONTRACT + "\nChanged authority\n", encoding="utf-8")
    before = {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()}
    llm = _LLM(_draft())
    preview = _repair(path, cfg, llm, state_store=store, dry_run=True)
    assert {p.relative_to(tmp_path): p.read_bytes() for p in tmp_path.rglob("*") if p.is_file()} == before
    assert not llm.calls
    result = _repair(path, cfg, llm, state_store=store)
    if change == "unchanged":
        assert preview.ready, preview.issue
        assert result.ready, result.issue
        assert len(llm.calls) == 1
    else:
        code = "contract_evidence_changed" if change == "contract" else "child_evidence_unavailable"
        assert preview.issue.code == result.issue.code == "delivery_repair." + code
        assert path.read_bytes() == before[path.relative_to(tmp_path)]
        assert not llm.calls


@pytest.mark.parametrize("phase", ["correction", "publication_resume"])
def test_changed_executed_contract_blocks_further_repair(
    completed_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    path, cfg = completed_plan
    before = path.read_bytes()
    store = _link_execution_evidence(path, tmp_path)

    def change_evidence() -> None:
        child = store.load("child-read")
        child.task_description += "\nChanged execution evidence\n"
        store.save(child)

    class ChangingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            if phase == "correction":
                change_evidence()
            return super().run_readonly_agent(prompt, cwd)

    llm = ChangingLLM("invalid" if phase == "correction" else _draft(), _draft())
    if phase == "publication_resume":
        with monkeypatch.context() as context:

            def interrupt(*args, **kwargs) -> None:
                raise KeyboardInterrupt()

            context.setattr(repair_module, "_atomic_replace_if_unchanged", interrupt)
            with pytest.raises(KeyboardInterrupt):
                _repair(path, cfg, llm, state_store=store)
        change_evidence()
    result = _repair(path, cfg, llm, state_store=store)
    assert result.issue.code == "delivery_repair.child_evidence_unavailable"
    assert len(llm.calls) == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("stop", ["external_dependency_gap", "scope_amendment_required", "human_review_required"])
def test_gate_hard_stop_does_not_author_repair(completed_plan, tmp_path: Path, stop: str) -> None:
    path, cfg = completed_plan
    progress_path = delivery_progress_path(tmp_path, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    record = replace(progress.verification, stop_code="delivery_verification." + stop)
    write_delivery_progress(progress_path, replace(progress, verification=record))
    llm = _LLM()
    result = _repair(path, cfg, llm)
    assert result.issue.code == "delivery_repair.input_unavailable"
    assert not llm.calls


def test_completed_child_scope_bounds_repair_even_with_broader_current_config(completed_plan, tmp_path: Path) -> None:
    path, cfg = completed_plan
    store = JsonStateStore(tmp_path / ".sikula/state")
    progress_path = delivery_progress_path(tmp_path, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    scope = resolve_delivery_write_scope(
        project_root=tmp_path, configured_write_paths=["src/cache.py"], unit_scope_paths=["src/"]
    )
    linked = []
    for unit in progress.units:
        child = TaskState(
            task_id="child-" + unit.unit_id,
            task_description=_CONTRACT,
            done=True,
            result_commit=unit.commit,
            delivery_plan_id="cache",
            delivery_unit_id=unit.unit_id,
            delivery_plan_path=path.relative_to(tmp_path).as_posix(),
            delivery_write_scope_schema_version=scope.schema_version,
            delivery_write_scope_mode=scope.mode,
            delivery_declared_write_paths=list(scope.declared_paths),
            delivery_declared_write_exact_file_paths=list(scope.declared_exact_file_paths),
            delivery_effective_write_paths=list(scope.effective_paths),
            delivery_effective_write_exact_file_paths=list(scope.effective_exact_file_paths),
        )
        store.save(child)
        linked.append(replace(unit, child_task_id=child.task_id))
    write_delivery_progress(progress_path, replace(progress, units=linked))
    # Operator checkout shape must not replace the exact candidate in a dry-run.
    (tmp_path / "src/cache.py").unlink()
    (tmp_path / "src/cache.py").mkdir()
    llm = _LLM(_draft())
    preview = _repair(path, cfg, llm, state_store=store, dry_run=True)
    assert preview.ready, preview.issue
    assert not llm.calls
    result = _repair(path, cfg, llm, state_store=store)
    assert result.ready, result.issue
    assert yaml.safe_load(path.read_text())["units"][-1]["scope_paths"] == ["src/cache.py"]


def test_elapsed_bound_after_authoring_defers_child_execution(
    completed_plan, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sikula_cli.delivery import DeliveryRunNextContext, _run_delivery_plan

    path, cfg = completed_plan
    elapsed = [0.0]
    llm = _LLM(_draft())
    original = repair_module.coordinate_delivery_repair

    def repair_then_elapsed(*args, **kwargs):
        result = original(*args, **kwargs)
        elapsed[0] = 61.0
        return result

    monkeypatch.setattr(repair_module, "coordinate_delivery_repair", repair_then_elapsed)
    monkeypatch.setattr("sikula_cli.delivery.time.monotonic", lambda: elapsed[0])
    context = DeliveryRunNextContext(
        run_task=lambda *_: pytest.fail("elapsed bound must prevent child execution"),
        resolve_state_dir=lambda _: tmp_path / ".sikula/state",
        state_store=JsonStateStore(tmp_path / ".sikula/state"),
        repair_agent_factory=lambda *_: DeliveryRepairAgent(llm),
    )
    args = argparse.Namespace(plan_file=str(path), max_units=None, max_elapsed_minutes=1, reset_failed=False)
    result = _run_delivery_plan(args, cfg, context, project_root=tmp_path)
    assert result.stop_code == "delivery.run.elapsed_limit_reached"
    assert result.succeeded and not result.completed
    assert result.units_attempted == 0
    assert len(llm.calls) == 1


def test_repair_inherits_exact_assets_and_deduplicates_shared_owners(tmp_path: Path) -> None:
    asset = (
        "## Asset manifest\n\n### Reference assets\n\n- Path: `assets/reference.png`\n"
        "  - Usage: reference only; never copy into production.\n"
        "  - SHA-256: `sha256:abc123`\n"
    )
    result = render_inherited_delivery_assets(
        _CONTRACT, inherited_tasks=[_CONTRACT + asset, _CONTRACT + asset], project_root=tmp_path, unit_id="repair"
    )
    assert result == _CONTRACT.rstrip() + "\n\n" + asset


@pytest.mark.parametrize("change", ["authored", "conflicting", "hidden"])
def test_repair_assets_cannot_be_reclassified_or_hidden(tmp_path: Path, change: str) -> None:
    asset = "## Assets\n\n- Reference asset: `assets/reference.png`\n"
    tasks = [_CONTRACT + asset]
    draft = _CONTRACT
    if change == "authored":
        draft += asset.replace("Reference asset", "Delivery asset")
    elif change == "conflicting":
        tasks.append(_CONTRACT + asset.replace("Reference asset", "Delivery asset"))
    else:
        draft += "\n<!--\n"
    with pytest.raises(DeliveryAssetAssignmentError):
        render_inherited_delivery_assets(draft, inherited_tasks=tasks, project_root=tmp_path, unit_id="repair")


@pytest.mark.parametrize("change", ["source", "contract"])
def test_publication_rechecks_authority_after_assembly(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    path, cfg = completed_plan
    original = repair_module.assemble_delivery_artifacts

    def mutate_authority(*args, **kwargs):
        result = original(*args, **kwargs)
        target = tmp_path / "source.md" if change == "source" else path.parent / "units/read.md"
        target.write_text("changed authority\n", encoding="utf-8")
        return result

    monkeypatch.setattr(repair_module, "assemble_delivery_artifacts", mutate_authority)
    llm = _LLM(_draft())
    result = _repair(path, cfg, llm)
    assert result.issue.code == "delivery_repair.stale"
    preview = preview_delivery_run_next(path, project_root=tmp_path)
    assert not preview.ready
    state = read_repair_state(tmp_path, tmp_path / ".sikula/state/delivery/cache/integration-repair.json")
    assert state["phase"] == "prepared"
    assert len(llm.calls) == 1


def test_control_input_failure_preserves_gate_assessment(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, cfg = completed_plan

    def fail_write(*args, **kwargs) -> str:
        raise OSError("unavailable")

    monkeypatch.setattr("core.delivery_repair_input.store_repair_input", fail_write)
    result = _verify(path, cfg)
    assert result.stop_code == "delivery_verification.repair_input_unavailable"
    assert result.semantic_status == "rejected"
    assert result.validation_executed
    assert result.obligation_gap_count == 1
    llm = _LLM()
    assert not _repair(path, cfg, llm).ready
    assert not llm.calls


def test_private_control_state_rejects_duplicate_fields(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"phase":"prepared","phase":"published"}', encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        read_repair_state(tmp_path, path)


def test_audit_failure_blocks_future_authoring(
    completed_plan: tuple[Path, dict], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path, cfg = completed_plan
    before = path.read_bytes()
    original = repair_module._append_audit

    def fail_finish(audit_path: Path, record: dict, **kwargs) -> None:
        if record.get("event") == "authoring_finished":
            raise OSError("unavailable")
        original(audit_path, record, **kwargs)

    monkeypatch.setattr(repair_module, "_append_audit", fail_finish)
    llm = _LLM(_draft())
    assert not _repair(path, cfg, llm).ready
    result = _repair(path, cfg, llm)
    assert result.issue.code == "delivery_repair.audit_unavailable"
    assert len(llm.calls) == 1
    assert path.read_bytes() == before
    deferred = read_repair_state(
        tmp_path, tmp_path / ".sikula/state/delivery/cache/integration-repair-pending-audit.json"
    )
    assert deferred["record"]["event"] == "authoring_finished"
    assert deferred["record"]["output"] == _draft()
    assert deferred["record"]["prompt"] == llm.calls[0]
    assert deferred["record"]["attempt"] == 1


def test_repair_preparer_override_does_not_override_child_agents(monkeypatch: pytest.MonkeyPatch) -> None:
    import sikula

    captured = []
    monkeypatch.setattr("core.llm_client.create_llm_client", lambda config: captured.append(config) or _LLM())
    cfg = {"llm": {"provider": "codex", "model": "default"}, "agents": {}}
    args = argparse.Namespace(agent_model=["delivery_preparer=repair-model"], agent_provider=None, agent_timeout=None)
    effective = sikula._delivery_verification_effective_config(args, cfg)
    sikula._create_delivery_repair_agent(args, effective)
    assert captured[0].model == "repair-model"
    assert effective["llm"]["model"] == "default"
    assert set(effective["agents"]) == {"delivery_preparer"}
    assert cfg["agents"] == {}
