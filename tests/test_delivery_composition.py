from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import yaml

from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgent
from core.delivery_composition import (
    build_composition_context,
    composition_evidence_path,
    composition_example,
    parse_composition,
)
from core.delivery_verification_review import DeliveryIntegrationReviewParseError
from core.delivery_progress import (
    delivery_progress_path,
    get_delivery_status,
    make_delivery_unit_progress,
    read_delivery_progress,
    write_delivery_progress,
)
from core.delivery_verification import build_delivery_verification_snapshot
from core.delivery_verification_scope import DeliveryVerificationScope
from core.delivery_verification_validation import DeliveryVerificationValidationResult
from core.delivery_verify import DeliveryVerifyResult, verify_delivery_plan
from core.state import JsonStateStore
from tests.test_delivery_checkpoints import checkpoint_plan as checkpoint_plan, _git, _verify_node
from tests.test_delivery_checkpoint_applicability import _advance
from tests.test_delivery_repair import _LLM, _assessment, _CONTRACT


@pytest.fixture
def composition_plan(checkpoint_plan: tuple[Path, dict], request: pytest.FixtureRequest) -> tuple[Path, dict]:
    path, cfg = checkpoint_plan
    data = yaml.safe_load(path.read_text())
    security_required = bool(getattr(request, "param", False))
    if security_required:
        data["units"][0]["risk_tags"] = ["privacy"]
    extras = []
    for index in range(4):
        key = f"part-{index}"
        unit = deepcopy(data["units"][0])
        unit.update(id=key, task_path=f"{key}.md", title="Storage implementation detail " * 32)
        data["units"].insert(0, unit)
        extras.append(key)
        (path.parent / f"{key}.md").write_text(_CONTRACT)
    data["checkpoints"][0]["unit_ids"].extend(extras)
    data["units"][-1]["depends_on"].extend(extras)
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    _git(path.parent, "add", ".")
    _git(path.parent, "commit", "-m", "larger checkpoint")
    commit = _git(path.parent, "rev-parse", "HEAD")
    progress_path = delivery_progress_path(path.parent, "cache")
    progress, _ = read_delivery_progress(progress_path, plan_id="cache")
    write_delivery_progress(
        progress_path,
        replace(
            progress,
            assembly_base_commit=commit,
            units=[make_delivery_unit_progress(key, "done", commit=commit) for key in ["read", "write", *extras]],
        ),
    )
    if security_required:
        assert _run(
            path, cfg, _LLM(_assessment("approved")), security_llm=_LLM(_security_approval()), node_id="storage"
        ).succeeded
    else:
        assert _verify_node(path, cfg)[0].succeeded
    return path, cfg


def _context(path: Path, cfg: dict) -> dict:
    status = get_delivery_status(path)
    snapshot = build_delivery_verification_snapshot(status, cfg, candidate_commit=status.assembled_commit)
    return build_composition_context(status, snapshot, cfg)


def _run(
    path: Path, cfg: dict, llm: _LLM, *, security_llm: _LLM | None = None, node_id: str = "root"
) -> DeliveryVerifyResult:
    with patch(
        "core.delivery_verify.run_delivery_verification_validation",
        return_value=DeliveryVerificationValidationResult(True, False, True),
    ):
        return verify_delivery_plan(
            path,
            cfg,
            project_root=path.parent,
            state_store=JsonStateStore(path.parent / ".sikula/state"),
            semantic_reviewer=DeliveryIntegrationReviewAgent(llm, cfg),
            security_reviewer=DeliveryIntegrationReviewAgent(security_llm, cfg) if security_llm else None,
            node_id=node_id,
        )


def _security_approval() -> str:
    payload = json.loads(_assessment("approved"))
    payload["obligation_results"] = []
    return json.dumps(payload)


@pytest.mark.parametrize("change", ["same_tree", "unrelated", "shared_dependency"])
def test_final_composition_consumes_checkpoint_and_closes_all_outcomes(
    composition_plan: tuple[Path, dict], change: str
) -> None:
    path, cfg = composition_plan
    origin = get_delivery_status(path).checkpoint_verifications["storage"]
    _advance(path, change)
    context = _context(path, cfg)
    llm = _LLM(composition_example(context))
    result = _run(path, cfg, llm)
    assert result.succeeded, result
    assert len(llm.calls) == 1
    assert "Storage implementation detail" not in llm.calls[0]
    assert '"checkpoint_composition"' in llm.calls[0]
    status = get_delivery_status(path)
    assert status.checkpoint_verifications["storage"] == origin
    assert status.verification.obligation_satisfied_count == 1
    assert status.verification.composition_evidence_fingerprint
    full_prompt = DeliveryIntegrationReviewAgent(_LLM(), cfg)._prompt(
        cwd=path.parent,
        review_kind="semantic",
        source_task=(path.parent / "source.md").read_text(),
        plan_context=DeliveryVerificationScope.from_plan(status.plan).plan_context(),
        validation_summary=DeliveryVerificationValidationResult(True, False, True).to_review_dict(
            cfg, project_root=path.parent
        ),
        candidate_commit=status.verification.candidate_commit,
        candidate_tree=status.verification.candidate_tree,
        known_obligation_ids={"consistent-read"},
    )
    assert len(llm.calls[0].encode()) < len(full_prompt.encode())
    assert _run(path, cfg, _LLM()).succeeded


@pytest.mark.parametrize("trigger", ["uncertainty", "cross_group_finding"])
def test_uncertain_checkpoint_autonomously_falls_back_to_full_review(
    composition_plan: tuple[Path, dict], trigger: str
) -> None:
    path, cfg = composition_plan
    _advance(path, "shared_dependency")
    control = json.loads(composition_example(_context(path, cfg)))
    if trigger == "uncertainty":
        control["checkpoint_results"][0]["outcome"] = "verification_required"
    else:
        control["disposition"] = "repair_required"
        control["findings"] = [
            {
                "code": "integration.gap",
                "summary": "Consumer integration is incomplete.",
                "unit_ids": ["consumer"],
                "obligation_ids": [],
            }
        ]
    llm = _LLM(json.dumps(control), _assessment("repair_required"))
    result = _run(path, cfg, llm)
    assert result.stop_code == "delivery_verification.repair_required", result
    assert len(llm.calls) == 2
    assert "Storage implementation detail" in llm.calls[1]
    assert get_delivery_status(path).verification.repair_input_fingerprint


def test_interrupted_composition_is_not_repeated(composition_plan: tuple[Path, dict]) -> None:
    path, cfg = composition_plan
    _advance(path)
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, _LLM(KeyboardInterrupt()))
    assert get_delivery_status(path).verification.composition_attempted
    llm = _LLM(_assessment("approved"))
    assert _run(path, cfg, llm).succeeded
    assert len(llm.calls) == 1 and '"checkpoint_composition"' not in llm.calls[0]


def test_missing_composition_proof_preempts_provider(composition_plan: tuple[Path, dict]) -> None:
    path, cfg = composition_plan
    _advance(path)
    assert _run(path, cfg, _LLM(composition_example(_context(path, cfg)))).succeeded
    record = get_delivery_status(path).verification
    composition_evidence_path(
        delivery_progress_path(path.parent, "cache").parent, record.composition_evidence_fingerprint
    ).unlink()
    llm = _LLM()
    result = _run(path, cfg, llm)
    assert not result.succeeded and not llm.calls


def test_composed_finalization_requires_the_referenced_child_artifact(composition_plan: tuple[Path, dict]) -> None:
    from core.delivery_checkpoint_evidence import checkpoint_evidence_path
    from core.delivery_checkpoint_applicability import validate_root_evidence
    from core.delivery_finalize import finalize_delivery_plan

    path, cfg = composition_plan
    _advance(path)
    assert _run(path, cfg, _LLM(composition_example(_context(path, cfg)))).succeeded
    record = get_delivery_status(path).checkpoint_verifications["storage"]
    checkpoint_evidence_path(delivery_progress_path(path.parent, "cache").parent, record).unlink()
    result = finalize_delivery_plan(path, project_config=cfg)
    assert not result.finalized
    assert any(issue.code == "delivery_checkpoint.required" for issue in result.errors), result
    status = get_delivery_status(path)
    with pytest.raises(ValueError):
        validate_root_evidence(status, status.verification)


def test_accepted_composition_survives_interruption_before_final_pass(composition_plan: tuple[Path, dict]) -> None:
    path, cfg = composition_plan
    _advance(path)
    with patch("core.delivery_verify.store_root_evidence", side_effect=KeyboardInterrupt):
        with pytest.raises(KeyboardInterrupt):
            _run(path, cfg, _LLM(composition_example(_context(path, cfg))))
    record = get_delivery_status(path).verification
    assert record.composition_evidence_fingerprint and record.status == "interrupted"
    llm = _LLM()
    assert _run(path, cfg, llm).succeeded
    assert not llm.calls
    public = json.dumps(get_delivery_status(path).to_dict())
    assert "composition_evidence" not in public and "checkpoint_results" not in public


def test_readonly_violation_with_audit_failure_remains_terminal(composition_plan: tuple[Path, dict]) -> None:
    from core.llm_client import LLMReadOnlyViolation
    from core.delivery_verify import _safe_append_audit

    path, cfg = composition_plan
    _advance(path)

    def audit(path: Path, value: dict, **kwargs) -> bool:
        return False if value.get("event") == "review_failed" else _safe_append_audit(path, value, **kwargs)

    with patch("core.delivery_verify._safe_append_audit", side_effect=audit):
        result = _run(path, cfg, _LLM(LLMReadOnlyViolation("provider boundary")))
    assert result.stop_code == "delivery_verification.readonly_mutation"
    llm = _LLM()
    assert _run(path, cfg, llm).stop_code == "delivery_verification.readonly_mutation"
    assert not llm.calls


def test_external_stop_is_not_retried_automatically_or_cached_as_permanent_authority(
    composition_plan: tuple[Path, dict],
) -> None:
    path, cfg = composition_plan
    _advance(path)
    control = json.loads(composition_example(_context(path, cfg)))
    control["disposition"] = "external_dependency_gap"
    control["checkpoint_results"][0]["outcome"] = "verification_required"
    control["findings"] = [
        {
            "code": "dependency.unavailable",
            "summary": "Required external input is unavailable.",
            "unit_ids": ["read"],
            "obligation_ids": ["consistent-read"],
        }
    ]
    llm = _LLM(json.dumps(control))
    result = _run(path, cfg, llm)
    assert result.stop_code == "delivery_verification.external_dependency_gap" and len(llm.calls) == 1
    assert not get_delivery_status(path).verification.composition_evidence_fingerprint
    # After resolving the external prerequisite, an explicit retry can reassess it.
    llm = _LLM(_assessment("approved"))
    assert _run(path, cfg, llm).succeeded
    assert len(llm.calls) == 1 and '"checkpoint_composition"' not in llm.calls[0]


@pytest.fixture
def packet() -> dict:
    return {
        "obligations": [{"id": "direct"}],
        "checkpoint_composition": {
            "children": [{"id": "storage", "exact_tree": False, "obligations": [{"id": "inherited"}]}]
        },
    }


@pytest.mark.parametrize(
    "damage",
    [
        "missing_child",
        "duplicate_child",
        "extra_child",
        "missing_citation",
        "private_rationale",
        "wrong_outcome",
        "missing_direct",
        "extra_direct",
        "list",
        "duplicate_key",
    ],
)
def test_composition_parser_rejects_incomplete_or_ungrounded_results(packet: dict, damage: str) -> None:
    control = json.loads(composition_example(packet))
    if damage == "missing_child":
        control["checkpoint_results"] = []
    elif damage == "duplicate_child":
        control["checkpoint_results"] *= 2
    elif damage == "extra_child":
        control["checkpoint_results"][0]["id"] = "unknown"
    elif damage == "missing_citation":
        control["checkpoint_results"][0]["evidence"] = []
    elif damage == "private_rationale":
        control["checkpoint_results"][0]["rationale"] = "/Users/operator/private.md"
    elif damage == "wrong_outcome":
        control["checkpoint_results"][0]["outcome"] = "passed"
    elif damage == "missing_direct":
        control["obligation_results"] = []
    elif damage == "extra_direct":
        control["obligation_results"].append({"id": "inherited", "outcome": "satisfied"})
    output = "[]" if damage == "list" else json.dumps(control)
    if damage == "duplicate_key":
        output = output.replace('"schema_version": 2', '"schema_version": 2, "schema_version": 2')
    with pytest.raises(DeliveryIntegrationReviewParseError):
        parse_composition(output, packet, {"consumer"})


def test_composition_parser_closes_direct_and_inherited_obligations(packet: dict) -> None:
    result = parse_composition(composition_example(packet), packet, {"consumer"})
    assert result.assessment.approved and not result.fallback
    assert {item.id: item.outcome for item in result.assessment.obligation_results} == {
        "direct": "satisfied",
        "inherited": "satisfied",
    }


@pytest.mark.parametrize(
    "disposition", ["external_dependency_gap", "human_review_required", "scope_amendment_required"]
)
def test_authoritative_stops_do_not_fall_back(packet: dict, disposition: str) -> None:
    control = json.loads(composition_example(packet))
    control["disposition"] = disposition
    control["findings"] = [
        {
            "code": "authority.unavailable",
            "summary": "Accepted authority requires external input.",
            "unit_ids": [],
            "obligation_ids": ["inherited"],
        }
    ]
    control["checkpoint_results"][0]["outcome"] = "verification_required"
    result = parse_composition(json.dumps(control), packet, {"consumer"})
    assert result.assessment.disposition == disposition and not result.fallback
    assert result.assessment.obligation_results[-1].outcome == "uncertain"


@pytest.mark.parametrize(
    "reason", ["large_delta", "many_paths", "private", "binary", "large_packet", "fan_in", "overlap"]
)
def test_unsupported_composition_selects_full_review_before_calls(
    checkpoint_plan: tuple[Path, dict], reason: str
) -> None:
    path, cfg = checkpoint_plan
    status = get_delivery_status(path)
    origin = SimpleNamespace(
        candidate_commit="a" * 40, candidate_tree="a" * 40, checkpoint_evidence_fingerprint="sha256:" + "a" * 64
    )
    status = replace(status, checkpoint_verifications={"storage": origin})
    scope = DeliveryVerificationScope.from_plan(status.plan)
    snapshot = SimpleNamespace(
        scope=scope, identity=SimpleNamespace(candidate_commit="b" * 40, candidate_tree="b" * 40)
    )
    metadata = ":100644 100644 " + "a" * 40 + " " + "b" * 40 + " M\0"
    names = metadata + "src/cache.py\0"
    diff = "Changed storage dependency."
    if reason == "many_paths":
        names = "".join(metadata + f"src/file-{i}.py\0" for i in range(33))
    elif reason == "private":
        names = metadata + ".env\0"
    elif reason == "binary":
        diff = "Binary files a/cache and b/cache differ"
    elif reason == "large_packet":
        status = replace(status, plan=replace(status.plan, title="x" * (128 * 1024)))
        snapshot.scope = DeliveryVerificationScope.from_plan(status.plan)
    elif reason in {"fan_in", "overlap"}:
        status = replace(
            status, plan=replace(status.plan, checkpoints=status.plan.checkpoints * (9 if reason == "fan_in" else 2))
        )
    outputs = [names, ValueError("delta budget")] if reason == "large_delta" else [names, diff]
    with (
        patch(
            "core.delivery_composition.load_checkpoint_evidence",
            return_value=SimpleNamespace(covers=lambda scope: True),
        ),
        patch("core.delivery_composition._git_bounded", side_effect=outputs),
    ):
        assert build_composition_context(status, snapshot, cfg) is None


def test_later_repair_owner_keeps_obligation_direct(checkpoint_plan: tuple[Path, dict]) -> None:
    path, cfg = checkpoint_plan
    status = get_delivery_status(path)
    obligations = [replace(item, unit_ids=[*item.unit_ids, "consumer"]) for item in status.plan.obligations]
    status = replace(
        status,
        plan=replace(status.plan, obligations=obligations),
        checkpoint_verifications={"storage": SimpleNamespace()},
    )
    snapshot = SimpleNamespace(scope=DeliveryVerificationScope.from_plan(status.plan))
    with patch(
        "core.delivery_composition.load_checkpoint_evidence", return_value=SimpleNamespace(covers=lambda scope: True)
    ):
        # With no fully-owned child obligation left, use the full final review.
        assert build_composition_context(status, snapshot, cfg) is None


def test_git_delta_reader_rejects_oversize_without_returning_a_prefix(tmp_path: Path) -> None:
    from core.delivery_composition import _git_bounded

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / "code.py").write_text("x = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "original")
    (tmp_path / "code.py").write_text("x = 2\n" * 1000)
    with pytest.raises(ValueError, match="budget"):
        _git_bounded(tmp_path, ["diff", "--no-ext-diff", "--no-textconv"], 128)


def test_composition_rejects_renamed_private_origin(checkpoint_plan: tuple[Path, dict]) -> None:
    path, cfg = checkpoint_plan
    root = path.parent
    _git(root, "config", "diff.renames", "true")
    private = root / ".env"
    private.write_text("PRIVATE_TEST_FIXTURE=not-a-real-secret\n")
    _git(root, "add", ".env")
    _git(root, "commit", "-m", "private fixture")
    origin_commit = _git(root, "rev-parse", "HEAD")
    origin_tree = _git(root, "rev-parse", "HEAD^{tree}")
    _git(root, "mv", ".env", "settings.txt")
    _git(root, "commit", "-m", "rename fixture")
    status = get_delivery_status(path)
    origin = SimpleNamespace(
        candidate_commit=origin_commit, candidate_tree=origin_tree, checkpoint_evidence_fingerprint="sha256:" + "a" * 64
    )
    status = replace(status, checkpoint_verifications={"storage": origin})
    snapshot = SimpleNamespace(
        scope=DeliveryVerificationScope.from_plan(status.plan),
        identity=SimpleNamespace(
            candidate_commit=_git(root, "rev-parse", "HEAD"), candidate_tree=_git(root, "rev-parse", "HEAD^{tree}")
        ),
    )
    with patch(
        "core.delivery_composition.load_checkpoint_evidence", return_value=SimpleNamespace(covers=lambda scope: True)
    ):
        assert build_composition_context(status, snapshot, cfg) is None


def _changed_context(path: Path, cfg: dict, origin_commit: str) -> dict | None:
    status = get_delivery_status(path)
    origin = SimpleNamespace(
        candidate_commit=origin_commit,
        candidate_tree=_git(path.parent, "rev-parse", origin_commit + "^{tree}"),
        checkpoint_evidence_fingerprint="sha256:" + "a" * 64,
    )
    status = replace(status, checkpoint_verifications={"storage": origin})
    snapshot = SimpleNamespace(
        scope=DeliveryVerificationScope.from_plan(status.plan),
        identity=SimpleNamespace(
            candidate_commit=_git(path.parent, "rev-parse", "HEAD"),
            candidate_tree=_git(path.parent, "rev-parse", "HEAD^{tree}"),
        ),
    )
    with patch(
        "core.delivery_composition.load_checkpoint_evidence", return_value=SimpleNamespace(covers=lambda scope: True)
    ):
        return build_composition_context(status, snapshot, cfg)


@pytest.mark.parametrize("kind", ["environment", "state", "report", "absolute_report"])
@pytest.mark.parametrize("change", ["modified", "deleted", "renamed_from", "renamed_to"])
def test_composition_excludes_configured_private_deltas_before_reading_content(
    checkpoint_plan: tuple[Path, dict], kind: str, change: str
) -> None:
    from core.delivery_composition import _git_bounded

    path, cfg = checkpoint_plan
    root = path.parent
    if kind == "environment":
        cfg["project"]["build_tool"] = "gradle-android"
        private = root / "local.properties"
    else:
        private = root / "private-evidence" / "record.txt"
        key = "state_dir" if kind == "state" else "contract_report_dir"
        cfg["tasks"] = {key: str(private.parent) if kind == "absolute_report" else "private-evidence"}
    private.parent.mkdir(parents=True, exist_ok=True)
    original = root / "public.txt" if change == "renamed_to" else private
    original.write_text("PRIVATE_TEST_FIXTURE=not-a-real-secret\n")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "original fixture")
    origin = _git(root, "rev-parse", "HEAD")
    if change == "deleted":
        private.unlink()
    elif change == "renamed_from":
        _git(root, "mv", private.relative_to(root).as_posix(), "public.txt")
    elif change == "renamed_to":
        _git(root, "mv", "public.txt", private.relative_to(root).as_posix())
    else:
        private.write_text("PRIVATE_TEST_FIXTURE=changed-not-a-real-secret\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "changed fixture")
    with patch("core.delivery_composition._git_bounded", wraps=_git_bounded) as read_git:
        assert _changed_context(path, cfg, origin) is None
    # Only metadata may be read; no historical or current private content enters the packet.
    assert len(read_git.call_args_list) == 1
    assert "--raw" in read_git.call_args.args[1]


@pytest.mark.parametrize("change", ["added", "modified", "deleted"])
@pytest.mark.parametrize("ignore", ["config", "gitmodules"])
def test_composition_rejects_gitlinks_even_when_git_ignores_them(
    checkpoint_plan: tuple[Path, dict], change: str, ignore: str
) -> None:
    from core.delivery_composition import _git_bounded

    path, cfg = checkpoint_plan
    root = path.parent
    first_pointer = _git(root, "rev-parse", "HEAD")
    (root / ".gitmodules").write_text(
        '[submodule "dependency"]\n\tpath = dependency\n\turl = ./unused\n\tignore = all\n'
        if ignore == "gitmodules"
        else '[submodule "dependency"]\n\tpath = dependency\n\turl = ./unused\n'
    )
    if ignore == "config":
        _git(root, "config", "diff.ignoreSubmodules", "all")
    _git(root, "add", ".gitmodules")
    if change != "added":
        _git(root, "update-index", "--add", "--cacheinfo", "160000," + first_pointer + ",dependency")
    _git(root, "commit", "-m", "original dependency fixture")
    origin = _git(root, "rev-parse", "HEAD")
    if change == "deleted":
        _git(root, "update-index", "--force-remove", "dependency")
    else:
        _git(root, "update-index", "--add", "--cacheinfo", "160000," + origin + ",dependency")
    _git(root, "commit", "-m", "changed dependency fixture")
    with patch("core.delivery_composition._git_bounded", wraps=_git_bounded) as read_git:
        assert _changed_context(path, cfg, origin) is None
    assert len(read_git.call_args_list) == 1
    assert "--raw" in read_git.call_args.args[1]


def test_composition_includes_deleted_public_content(checkpoint_plan: tuple[Path, dict]) -> None:
    path, cfg = checkpoint_plan
    source = path.parent / "old-api.py"
    source.write_text("old_behavior = True\n")
    _git(path.parent, "add", ".")
    _git(path.parent, "commit", "-m", "old public API")
    origin = _git(path.parent, "rev-parse", "HEAD")
    source.unlink()
    _git(path.parent, "add", "-A")
    _git(path.parent, "commit", "-m", "remove public API")
    context = _changed_context(path, cfg, origin)
    assert context is not None
    assert "-old_behavior = True" in context["checkpoint_composition"]["children"][0]["delta"]


def test_composition_format_retry_checks_readonly_boundary_first(tmp_path: Path, packet: dict) -> None:
    from agents.delivery_integration_review_agent import DeliveryIntegrationReviewAgentError
    from core.delivery_verify import _run_review

    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "user.email", "test@example.com")
    (tmp_path / ".gitignore").write_text(".sikula/state/\n")
    source = tmp_path / "code.py"
    source.write_text("value = 1\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "candidate")
    identity = SimpleNamespace(
        candidate_commit=_git(tmp_path, "rev-parse", "HEAD"), candidate_tree=_git(tmp_path, "rev-parse", "HEAD^{tree}")
    )

    class MutatingLLM(_LLM):
        def run_readonly_agent(self, prompt: str, cwd: Path) -> str:
            source.write_text("value = 2\n")
            return super().run_readonly_agent(prompt, cwd)

    cfg = {"project": {"root_path": str(tmp_path)}, "sandbox": {"allowed_read_paths": ["."]}}
    llm = MutatingLLM("malformed", "malformed")
    with pytest.raises(DeliveryIntegrationReviewAgentError) as raised:
        _run_review(
            DeliveryIntegrationReviewAgent(llm, cfg),
            worktree=tmp_path,
            kind="semantic",
            source_task="Preserve behavior.",
            plan_context=packet,
            validation=DeliveryVerificationValidationResult(True, False, True),
            identity=identity,
            known_unit_ids={"consumer"},
            known_obligation_ids={"direct", "inherited"},
            evidence_path=tmp_path / ".sikula/state/delivery/test/verification.jsonl",
            audit_root=tmp_path,
        )
    assert raised.value.code == "delivery_verification.readonly_mutation"
    assert len(llm.calls) == 1


def test_malformed_composition_has_bounded_calls_then_full_fallback(composition_plan: tuple[Path, dict]) -> None:
    path, cfg = composition_plan
    _advance(path)
    llm = _LLM("malformed", "malformed", _assessment("approved"))
    result = _run(path, cfg, llm)
    assert result.succeeded, result
    assert len(llm.calls) == 3
    assert '"checkpoint_composition"' in llm.calls[0] and '"checkpoint_composition"' in llm.calls[1]
    assert '"checkpoint_composition"' not in llm.calls[2]
    record = get_delivery_status(path).verification
    audit = (path.parent / record.evidence_path).read_text()
    assert '"event": "composition_call"' in audit
    assert "prior_attempts" in audit


@pytest.mark.parametrize("composition_plan", [True], indirect=True)
def test_composed_semantics_does_not_skip_security_and_survives_its_interruption(
    composition_plan: tuple[Path, dict],
) -> None:
    path, cfg = composition_plan
    _advance(path)
    semantic = _LLM(composition_example(_context(path, cfg)))
    security = _LLM(KeyboardInterrupt())
    with pytest.raises(KeyboardInterrupt):
        _run(path, cfg, semantic, security_llm=security)
    assert len(semantic.calls) == len(security.calls) == 1
    assert "Storage implementation detail" in security.calls[0]
    assert '"checkpoint_composition"' not in security.calls[0]
    semantic, security = _LLM(), _LLM(_security_approval())
    result = _run(path, cfg, semantic, security_llm=security)
    assert result.succeeded, result
    assert not semantic.calls and len(security.calls) == 1


@pytest.mark.parametrize("shape", ["larger", "oversized", "fallback_oversized"])
def test_rendered_prompt_preflight_does_not_reserve_unusable_composition(tmp_path: Path, shape: str) -> None:
    from core.delivery_verify import _composition_review

    full, composed = "full prompt", "longer composed prompt"
    if shape == "oversized":
        full, composed = "f" * 100, "č" * (512 * 1024)
    elif shape == "fallback_oversized":
        full, composed = "f" * (512 * 1024), "composed prompt"
    reviewer = Mock()
    reviewer._prompt.side_effect = [full, composed]
    scope = SimpleNamespace(node_id="root", obligation_ids={"direct"}, plan_context=lambda: {})
    snapshot = SimpleNamespace(
        scope=scope, identity=SimpleNamespace(candidate_commit="a" * 40, candidate_tree="b" * 40)
    )
    running = SimpleNamespace(composition_attempted=False, composition_evidence_fingerprint=None)
    with (
        patch("core.delivery_composition.build_composition_context", return_value={}),
        patch("core.delivery_verify._persist_composition_control") as reserve,
    ):
        result, unchanged = _composition_review(
            status=SimpleNamespace(plan=SimpleNamespace(checkpoints=["storage"])),
            snapshot=snapshot,
            running=running,
            reviewer=reviewer,
            worktree=tmp_path,
            root=tmp_path,
            source_task="source",
            validation=DeliveryVerificationValidationResult(True, False, True),
            evidence_path=tmp_path / "audit",
            project_config={},
        )
    assert result is None and unchanged is running
    reserve.assert_not_called()
    reviewer.review.assert_not_called()
