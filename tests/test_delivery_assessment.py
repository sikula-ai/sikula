from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from core.delivery_authoring import (
    DeliveryAssessmentDraft,
    DeliveryAssessmentUnitDraft,
    DeliveryAuthoringParseError,
    parse_delivery_assessment_output,
)
import sikula
from sikula_cli.delivery import (
    DeliveryAssessmentContext,
    _delivery_assessment_next_command,
    cmd_delivery_assess,
    register_parser,
)


def _cfg(root: Path) -> dict:
    return {
        "project": {"root_path": str(root), "build_tool": "python"},
        "build": {"test_command": "python3 -m pytest tests/"},
    }


def _args(
    task_file: str | Path,
    *,
    config: str | None = None,
    json_output: bool = False,
    agent_model: list[str] | None = None,
    agent_provider: list[str] | None = None,
    agent_timeout: list[str] | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        task_file=str(task_file),
        config=config,
        json=json_output,
        agent_model=agent_model,
        agent_provider=agent_provider,
        agent_timeout=agent_timeout,
    )


def _write_task(root: Path, rel_path: str = "tasks/feature.md") -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# Feature\n\nPRIVATE TASK BODY\n", encoding="utf-8")
    return path


def _draft(
    mode: str = "single_run",
    *,
    reason_codes: list[str] | None = None,
    units: list[DeliveryAssessmentUnitDraft] | None = None,
    audit_path: str | None = None,
) -> DeliveryAssessmentDraft:
    defaults = {
        "single_run": ["single_cohesive_surface"],
        "delivery_plan": ["multiple_independent_surfaces"],
        "needs_clarification": ["scope_unclear"],
    }
    draft = DeliveryAssessmentDraft(
        recommended_mode=mode,
        reason_codes=reason_codes or defaults[mode],
        units=units or [],
    )
    if audit_path is not None:
        object.__setattr__(draft, "audit_path", audit_path)
    return draft


def _context(
    draft: DeliveryAssessmentDraft | None = None,
    *,
    calls: list[dict] | None = None,
    failure: Exception | None = None,
) -> DeliveryAssessmentContext:
    def run_assessment_assistant(**kwargs):
        if calls is not None:
            calls.append(kwargs)
        if failure is not None:
            raise failure
        return draft or _draft()

    return DeliveryAssessmentContext(run_assessment_assistant=run_assessment_assistant)


def test_parse_delivery_assessment_accepts_single_run() -> None:
    result = parse_delivery_assessment_output(
        json.dumps(
            {
                "recommended_mode": "single_run",
                "reason_codes": ["single_cohesive_surface", "single_validation_boundary"],
                "units": [],
            }
        )
    )

    assert result.recommended_mode == "single_run"
    assert result.reason_codes == ["single_cohesive_surface", "single_validation_boundary"]
    assert result.units == []


def test_parse_delivery_assessment_accepts_platform_neutral_dependency_outline() -> None:
    result = parse_delivery_assessment_output(
        json.dumps(
            {
                "recommended_mode": "delivery_plan",
                "reason_codes": ["multiple_platforms", "dependency_order_required"],
                "units": [
                    {
                        "id": "shared-behavior",
                        "title": "Define shared behavior",
                        "depends_on": [],
                        "stream": "product",
                        "component": "shared",
                        "platform": "shared",
                    },
                    {
                        "id": "platform-a",
                        "title": "Implement platform A",
                        "depends_on": ["shared-behavior"],
                        "component": "client-a",
                        "platform": "platform-a",
                    },
                    {
                        "id": "platform-b",
                        "title": "Implement platform B",
                        "depends_on": ["shared-behavior"],
                        "component": "client-b",
                        "platform": "platform-b",
                    },
                ],
            }
        )
    )

    assert result.recommended_mode == "delivery_plan"
    assert [unit.id for unit in result.units] == ["shared-behavior", "platform-a", "platform-b"]
    assert result.units[1].depends_on == ["shared-behavior"]
    assert result.units[1].platform == "platform-a"
    assert result.units[2].platform == "platform-b"


def test_parse_delivery_assessment_accepts_needs_clarification() -> None:
    result = parse_delivery_assessment_output(
        '{"recommended_mode":"needs_clarification","reason_codes":["validation_unclear"],"units":[]}'
    )

    assert result.recommended_mode == "needs_clarification"
    assert result.reason_codes == ["validation_unclear"]
    assert result.units == []


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        pytest.param("not json", "delivery_authoring.json_invalid", id="malformed_json"),
        pytest.param(
            '{"recommended_mode":"single_run","reason_codes":["single_cohesive_surface"],'
            '"units":[],"raw_output":"secret"}',
            "delivery_authoring.unknown_field",
            id="unknown_top_level",
        ),
        pytest.param(
            '{"recommended_mode":"automatic","reason_codes":["single_cohesive_surface"],"units":[]}',
            "delivery_assessment.mode_invalid",
            id="unsupported_mode",
        ),
        pytest.param(
            '{"recommended_mode":"single_run","reason_codes":[],"units":[]}',
            "delivery_assessment.reason_codes_required",
            id="empty_reasons",
        ),
        pytest.param(
            '{"recommended_mode":"single_run","reason_codes":["unknown"],"units":[]}',
            "delivery_assessment.reason_code_invalid",
            id="unknown_reason",
        ),
        pytest.param(
            '{"recommended_mode":"single_run","reason_codes":["multiple_platforms"],"units":[]}',
            "delivery_assessment.reason_code_mode_mismatch",
            id="reason_mode_mismatch",
        ),
        pytest.param(
            '{"recommended_mode":"single_run","reason_codes":'
            '["single_cohesive_surface","single_cohesive_surface"],"units":[]}',
            "delivery_assessment.reason_code_duplicate",
            id="duplicate_reason",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["multiple_components"],"units":[]}',
            "delivery_assessment.units_required",
            id="missing_delivery_units",
        ),
        pytest.param(
            '{"recommended_mode":"single_run","reason_codes":["single_cohesive_surface"],'
            '"units":[{"id":"one","title":"One","depends_on":[]}]}',
            "delivery_assessment.units_forbidden",
            id="single_run_units",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["multiple_components"],'
            '"units":[{"id":"api","title":"API","depends_on":[]},'
            '{"id":"API","title":"API two","depends_on":[]}]}',
            "delivery_assessment.unit_id_duplicate",
            id="case_colliding_ids",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["multiple_components"],'
            '"units":[{"id":"../api","title":"API","depends_on":[]},'
            '{"id":"web","title":"Web","depends_on":[]}]}',
            "delivery_authoring.unit_id_invalid",
            id="unsafe_id",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["dependency_order_required"],'
            '"units":[{"id":"api","title":"API","depends_on":["missing"]},'
            '{"id":"web","title":"Web","depends_on":[]}]}',
            "delivery_assessment.dependency_unknown",
            id="unknown_dependency",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["dependency_order_required"],'
            '"units":[{"id":"api","title":"API","depends_on":["api"]},'
            '{"id":"web","title":"Web","depends_on":[]}]}',
            "delivery_assessment.dependency_self",
            id="self_dependency",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["dependency_order_required"],'
            '"units":[{"id":"api","title":"API","depends_on":["web","web"]},'
            '{"id":"web","title":"Web","depends_on":[]}]}',
            "delivery_assessment.dependency_duplicate",
            id="duplicate_dependency",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["dependency_order_required"],'
            '"units":[{"id":"api","title":"API","depends_on":["web"]},'
            '{"id":"web","title":"Web","depends_on":["api"]}]}',
            "delivery_assessment.dependency_cycle",
            id="dependency_cycle",
        ),
        pytest.param(
            '{"recommended_mode":"delivery_plan","reason_codes":["multiple_components"],'
            '"units":[{"id":"api","title":"API","depends_on":[],"task_path":"secret.md"},'
            '{"id":"web","title":"Web","depends_on":[]}]}',
            "delivery_authoring.unknown_field",
            id="path_field",
        ),
    ],
)
def test_parse_delivery_assessment_rejects_invalid_output(payload: str, code: str) -> None:
    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_assessment_output(payload)

    assert exc_info.value.code == code
    assert "secret" not in str(exc_info.value).casefold()


def test_parse_delivery_assessment_rejects_unbounded_labels() -> None:
    payload = json.dumps(
        {
            "recommended_mode": "delivery_plan",
            "reason_codes": ["multiple_components"],
            "units": [
                {"id": "api", "title": "x" * 1001, "depends_on": []},
                {"id": "web", "title": "Web", "depends_on": []},
            ],
        }
    )

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_assessment_output(payload)

    assert exc_info.value.code == "delivery_assessment.label_invalid"


def test_parse_delivery_assessment_rejects_control_characters_in_labels() -> None:
    payload = json.dumps(
        {
            "recommended_mode": "delivery_plan",
            "reason_codes": ["multiple_components"],
            "units": [
                {"id": "api", "title": "API\ninjected", "depends_on": []},
                {"id": "web", "title": "Web", "depends_on": []},
            ],
        }
    )

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_assessment_output(payload)

    assert exc_info.value.code == "delivery_assessment.label_invalid"


def test_parse_delivery_assessment_rejects_surrogate_code_points_in_labels() -> None:
    payload = (
        '{"recommended_mode":"delivery_plan","reason_codes":["multiple_components"],'
        '"units":[{"id":"api","title":"\\ud800","depends_on":[]},'
        '{"id":"web","title":"Web","depends_on":[]}]}'
    )

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_assessment_output(payload)

    assert exc_info.value.code == "delivery_assessment.label_invalid"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("title", "Read /Users/example/private/task.md"),
        ("component", r"client at C:\Users\example\private"),
        ("platform", r"\\server\private\project"),
    ],
)
def test_parse_delivery_assessment_rejects_absolute_paths_in_labels(field: str, value: str) -> None:
    first_unit = {"id": "api", "title": "API", "depends_on": []}
    first_unit[field] = value
    payload = json.dumps(
        {
            "recommended_mode": "delivery_plan",
            "reason_codes": ["multiple_components"],
            "units": [
                first_unit,
                {"id": "web", "title": "Web / client", "depends_on": []},
            ],
        }
    )

    with pytest.raises(DeliveryAuthoringParseError) as exc_info:
        parse_delivery_assessment_output(payload)

    assert exc_info.value.code == "delivery_assessment.label_invalid"
    assert value not in str(exc_info.value)


@pytest.mark.parametrize("route", ["/users/{id}", "/home", "/v1/files"])
def test_parse_delivery_assessment_accepts_http_route_in_title(route: str) -> None:
    result = parse_delivery_assessment_output(
        json.dumps(
            {
                "recommended_mode": "delivery_plan",
                "reason_codes": ["multiple_components"],
                "units": [
                    {
                        "id": "api",
                        "title": f"Implement GET {route}",
                        "depends_on": [],
                    },
                    {"id": "web", "title": "Web client", "depends_on": ["api"]},
                ],
            }
        )
    )

    assert result.units[0].title == f"Implement GET {route}"


def test_delivery_assessment_parser_registers_platform_neutral_command() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_parser(subparsers)

    args = parser.parse_args(
        [
            "delivery",
            "assess",
            ".sikula/tasks/feature.md",
            "--json",
            "--agent-model",
            "delivery_preparer=gpt-5.5",
            "--agent-provider",
            "delivery_preparer=claude",
            "--agent-timeout",
            "delivery_preparer=1200",
        ]
    )

    assert args.command == "delivery"
    assert args.delivery_command == "assess"
    assert args.task_file == ".sikula/tasks/feature.md"
    assert args.json is True
    assert args.agent_model == ["delivery_preparer=gpt-5.5"]
    assert args.agent_provider == ["delivery_preparer=claude"]
    assert args.agent_timeout == ["delivery_preparer=1200"]


def test_cmd_delivery_assess_projects_mixed_platform_recommendation_without_model_labels_or_state_writes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    calls: list[dict] = []
    model_labels = (
        "private-shared-id",
        "PRIVATE TASK BODY",
        "private-stream",
        "private-platform-a-id",
        "Private Platform A",
        "private-component",
        "private-platform",
        "private-platform-b-id",
        "Private Platform B",
    )
    draft = _draft(
        "delivery_plan",
        reason_codes=["multiple_platforms", "dependency_order_required"],
        units=[
            DeliveryAssessmentUnitDraft(
                model_labels[0],
                model_labels[1],
                [],
                stream=model_labels[2],
            ),
            DeliveryAssessmentUnitDraft(
                model_labels[3],
                model_labels[4],
                [model_labels[0]],
                component=model_labels[5],
                platform=model_labels[6],
            ),
            DeliveryAssessmentUnitDraft(
                model_labels[7],
                model_labels[8],
                [model_labels[0]],
            ),
        ],
        audit_path=".sikula/contract-reports/feature.delivery-assess.auto-llm.jsonl",
    )
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args("tasks/feature.md", json_output=True),
        _cfg(tmp_path),
        _context(draft, calls=calls),
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload == {
        "audit_path": ".sikula/contract-reports/feature.delivery-assess.auto-llm.jsonl",
        "errors": [],
        "message": "Use a delivery plan with 3 proposed independently reviewable units.",
        "next_command": "sikula delivery prepare tasks/feature.md",
        "ready": True,
        "reason_codes": ["multiple_platforms", "dependency_order_required"],
        "reasons": [
            {"code": "multiple_platforms", "message": "The task spans multiple project platforms."},
            {
                "code": "dependency_order_required",
                "message": "The expected outcomes require explicit dependency ordering.",
            },
        ],
        "recommended_mode": "delivery_plan",
        "status": "ready",
        "task_file": "tasks/feature.md",
        "unit_count": 3,
    }
    assert all(value not in output for value in model_labels)
    assert str(tmp_path) not in output
    assert calls[0]["task_path"] == (tmp_path / "tasks" / "feature.md").resolve()
    assert calls[0]["project_root"] == tmp_path.resolve()
    assert not (tmp_path / ".sikula" / "delivery").exists()
    assert not (tmp_path / ".sikula" / "state").exists()
    assert not (tmp_path / ".sikula" / "worktrees").exists()

    cmd_delivery_assess(
        _args("tasks/feature.md"),
        _cfg(tmp_path),
        _context(draft),
    )
    text_output = capsys.readouterr().out
    assert "Proposed unit count: 3" in text_output
    assert all(value not in text_output for value in model_labels)


@pytest.mark.parametrize(
    ("mode", "reason", "expected_command"),
    [
        ("single_run", "single_cohesive_surface", "sikula contract prepare tasks/feature.md"),
        (
            "needs_clarification",
            "scope_unclear",
            "sikula task refine tasks/feature.md --auto --output tasks/feature.v2.md",
        ),
    ],
)
def test_cmd_delivery_assess_accepts_non_delivery_recommendations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    reason: str,
    expected_command: str,
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args("tasks/feature.md", json_output=True),
        _cfg(tmp_path),
        _context(_draft(mode, reason_codes=[reason])),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ready"
    assert payload["recommended_mode"] == mode
    assert payload["unit_count"] == 0
    assert payload["next_command"] == expected_command


@pytest.mark.parametrize(
    ("mode", "reason", "expected_command"),
    [
        ("single_run", "single_cohesive_surface", "sikula contract prepare ./-feature.md"),
        ("delivery_plan", "multiple_independent_surfaces", "sikula delivery prepare ./-feature.md"),
        (
            "needs_clarification",
            "scope_unclear",
            "sikula task refine ./-feature.md --auto --output ./-feature.v2.md",
        ),
    ],
)
def test_cmd_delivery_assess_preserves_cli_safe_prefix_for_leading_dash_task(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    mode: str,
    reason: str,
    expected_command: str,
) -> None:
    _write_task(tmp_path, "-feature.md")
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args("./-feature.md", json_output=True),
        _cfg(tmp_path),
        _context(
            _draft(
                mode,
                reason_codes=[reason],
                units=(
                    [
                        DeliveryAssessmentUnitDraft("foundation", "Foundation", []),
                        DeliveryAssessmentUnitDraft("feature", "Feature", ["foundation"]),
                    ]
                    if mode == "delivery_plan"
                    else None
                ),
            )
        ),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["task_file"] == "-feature.md"
    assert payload["next_command"] == expected_command


def test_cmd_delivery_assess_suggests_path_relative_to_invocation_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    invocation_dir = tmp_path / "subdir"
    invocation_dir.mkdir()
    monkeypatch.chdir(invocation_dir)

    cmd_delivery_assess(
        _args("../tasks/feature.md", json_output=True),
        _cfg(tmp_path),
        _context(_draft("single_run")),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["task_file"] == "tasks/feature.md"
    assert payload["next_command"] == "sikula contract prepare ../tasks/feature.md"


def test_cmd_delivery_assess_preserves_explicit_config_in_suggested_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args(
            "tasks/feature.md",
            config=str(tmp_path / "config" / "custom.yaml"),
            json_output=True,
        ),
        _cfg(tmp_path),
        _context(
            _draft(
                "delivery_plan",
                units=[DeliveryAssessmentUnitDraft("a", "A", []), DeliveryAssessmentUnitDraft("b", "B", [])],
            )
        ),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_command"] == ("sikula --config config/custom.yaml delivery prepare tasks/feature.md")


def test_cmd_delivery_assess_omits_suggestion_for_unsafe_explicit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args(
            "tasks/feature.md",
            config="config/unsafe\nname.yaml",
            json_output=True,
        ),
        _cfg(tmp_path),
        _context(_draft("single_run")),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_command"] is None


def test_cmd_delivery_assess_omits_suggestion_for_external_explicit_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "project"
    _write_task(project_root)
    external_config = tmp_path / "private" / "config.yaml"
    external_config.parent.mkdir()
    external_config.write_text("project:\n  root_path: ../project\n", encoding="utf-8")
    monkeypatch.chdir(project_root)

    cmd_delivery_assess(
        _args(
            "tasks/feature.md",
            config=str(external_config),
            json_output=True,
        ),
        _cfg(project_root),
        _context(_draft("single_run")),
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["next_command"] is None
    assert str(external_config) not in output


def test_delivery_assess_omits_windows_command_for_shell_metacharacters(tmp_path: Path) -> None:
    with patch("sikula_cli.delivery.os.name", "nt"):
        assert (
            _delivery_assessment_next_command(
                "single_run",
                "tasks/a & calc &.md",
                tmp_path,
            )
            is None
        )
        assert (
            _delivery_assessment_next_command(
                "single_run",
                "tasks/feature.md",
                tmp_path,
            )
            == "sikula contract prepare tasks/feature.md"
        )


def test_cmd_delivery_assess_omits_suggested_command_outside_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    project_root = tmp_path / "project"
    task_path = _write_task(project_root)
    invocation_dir = tmp_path / "outside"
    invocation_dir.mkdir()
    monkeypatch.chdir(invocation_dir)

    cmd_delivery_assess(
        _args(task_path, json_output=True),
        _cfg(project_root),
        _context(_draft("single_run")),
    )

    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["task_file"] == "tasks/feature.md"
    assert payload["next_command"] is None
    assert str(project_root) not in output


def test_cmd_delivery_assess_suggests_first_available_task_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, ".sikula/tasks/feature.refined.md")
    _write_task(tmp_path, ".sikula/tasks/feature.v2.md")
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args(".sikula/tasks/feature.refined.md", json_output=True),
        _cfg(tmp_path),
        _context(_draft("needs_clarification")),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_command"] == (
        "sikula task refine .sikula/tasks/feature.refined.md --auto --output .sikula/tasks/feature.v3.md"
    )


def test_cmd_delivery_assess_skips_dangling_symlink_task_revision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path, ".sikula/tasks/feature.refined.md")
    revision = tmp_path / ".sikula" / "tasks" / "feature.v2.md"
    revision.symlink_to(tmp_path / "missing-target.md")
    monkeypatch.chdir(tmp_path)

    cmd_delivery_assess(
        _args(".sikula/tasks/feature.refined.md", json_output=True),
        _cfg(tmp_path),
        _context(_draft("needs_clarification")),
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["next_command"] == (
        "sikula task refine .sikula/tasks/feature.refined.md --auto --output .sikula/tasks/feature.v3.md"
    )


def test_cmd_delivery_assess_blocks_invalid_overrides_before_calling_assistant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_assess(
            _args("tasks/feature.md", json_output=True, agent_model=["analyst=model"]),
            _cfg(tmp_path),
            _context(calls=calls),
        )

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == "delivery_assessment.agent_override_invalid"
    assert calls == []


@pytest.mark.parametrize(
    ("task_file", "expected_code"),
    [
        ("tasks/missing.md", "delivery_assessment.task_missing"),
        ("../outside.md", "delivery_assessment.task_outside_project"),
    ],
)
def test_cmd_delivery_assess_blocks_unsafe_task_paths(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    task_file: str,
    expected_code: str,
) -> None:
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_assess(_args(task_file, json_output=True), _cfg(tmp_path), _context())

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == expected_code
    assert str(tmp_path.parent) not in json.dumps(payload)


@pytest.mark.parametrize(
    ("task_file", "tasks_config"),
    [
        (".sikula/state/private.json", {}),
        (".sikula/contract-reports/private.jsonl", {}),
        (".sikula/worktrees/task/project/private.md", {}),
        (".git/private.md", {}),
        ("vendor/repository/.git/private.md", {}),
        ("private-state/private.json", {"state_dir": "private-state"}),
        (
            "private-reports/private.jsonl",
            {"contract_report_dir": "private-reports"},
        ),
    ],
)
def test_cmd_delivery_assess_rejects_private_artifact_task_files_before_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    task_file: str,
    tasks_config: dict[str, str],
) -> None:
    _write_task(tmp_path, task_file)
    calls: list[dict] = []
    cfg = _cfg(tmp_path)
    cfg["tasks"] = tasks_config
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_assess(
            _args(task_file, json_output=True),
            cfg,
            _context(calls=calls),
        )

    assert exc_info.value.code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["errors"][0]["code"] == "delivery_assessment.task_runtime_artifact"
    assert calls == []


@pytest.mark.parametrize("control", ["\n", "\x1b"])
def test_cmd_delivery_assess_rejects_control_characters_in_task_display_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    control: str,
) -> None:
    task_file = f"tasks/unsafe{control}name.md"
    if os.name != "nt":
        _write_task(tmp_path, task_file)
    calls: list[dict] = []
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_assess(_args(task_file), _cfg(tmp_path), _context(calls=calls))

    output = capsys.readouterr().out
    assert exc_info.value.code == 1
    assert "Delivery assessment: <unknown>" in output
    assert "delivery_assessment.task_path_unsafe" in output
    assert task_file not in output
    assert calls == []


def test_cmd_delivery_assess_redacts_provider_failure_and_surfaces_safe_audit_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _write_task(tmp_path)
    error = RuntimeError("SECRET PROVIDER FAILURE")
    setattr(error, "audit_path", ".sikula/contract-reports/feature.delivery-assess.auto-llm.jsonl")
    monkeypatch.chdir(tmp_path)

    with pytest.raises(SystemExit) as exc_info:
        cmd_delivery_assess(
            _args("tasks/feature.md", json_output=True),
            _cfg(tmp_path),
            _context(failure=error),
        )

    assert exc_info.value.code == 1
    output = capsys.readouterr().out
    payload = json.loads(output)
    assert payload["status"] == "blocked"
    assert payload["audit_path"] == ".sikula/contract-reports/feature.delivery-assess.auto-llm.jsonl"
    assert payload["errors"][0]["code"] == "delivery_assessment.authoring_failed"
    assert "SECRET" not in output


def test_sikula_cmd_delivery_assess_wrapper_delegates_to_cli(tmp_path: Path) -> None:
    args = _args("tasks/feature.md")
    cfg = _cfg(tmp_path)

    with patch("sikula.cli_delivery.cmd_delivery_assess", return_value="assessed") as assess:
        result = sikula.cmd_delivery_assess(args, cfg)

    assert result == "assessed"
    assess.assert_called_once()
    called_args = assess.call_args.args
    assert called_args[:2] == (args, cfg)
    assert isinstance(called_args[2], DeliveryAssessmentContext)
    assert called_args[2].run_assessment_assistant is sikula._run_delivery_assessment


def test_run_delivery_assessment_records_local_audit_without_source_artifacts(tmp_path: Path) -> None:
    task_path = _write_task(tmp_path, ".sikula/tasks/feature.md")
    args = _args(task_path)
    cfg = _cfg(tmp_path)
    project_context = {"validation_commands": ["python3 -m pytest tests/"]}
    calls: list[dict] = []

    class FakeAgent:
        def assess_delivery_mode(self, **kwargs):
            calls.append(kwargs)
            kwargs["audit_recorder"](
                {
                    "phase": "delivery_assessment",
                    "raw_output": "PRIVATE PROVIDER OUTPUT",
                    "parsed": {"status": "parsed", "recommended_mode": "single_run"},
                }
            )
            return _draft()

    with (
        patch("sikula._create_delivery_preparation_agent", return_value=FakeAgent()),
        patch("sikula._prepare_project_context_from_config", return_value=project_context),
    ):
        result = sikula._run_delivery_assessment(
            args=args,
            cfg=cfg,
            task_path=task_path.resolve(),
            project_root=tmp_path.resolve(),
        )

    assert result.recommended_mode == "single_run"
    assert result.audit_path == ".sikula/contract-reports/feature.delivery-assess.auto-llm.jsonl"
    assert calls[0]["task_description"] == "# Feature\n\nPRIVATE TASK BODY\n"
    assert calls[0]["project_context"] is project_context
    audit_path = tmp_path / ".sikula" / "contract-reports" / "feature.delivery-assess.auto-llm.jsonl"
    record = json.loads(audit_path.read_text(encoding="utf-8").splitlines()[0])
    assert record["generated_by"] == "sikula.delivery_assess"
    assert record["record"]["raw_output"] == "PRIVATE PROVIDER OUTPUT"
    assert not (tmp_path / ".sikula" / "delivery").exists()
    assert not (tmp_path / ".sikula" / "state").exists()


def test_run_delivery_assessment_separates_same_named_task_audits(tmp_path: Path) -> None:
    task_paths = [
        _write_task(tmp_path, ".sikula/tasks/a/feature.md"),
        _write_task(tmp_path, ".sikula/tasks/b/feature.md"),
    ]
    cfg = _cfg(tmp_path)

    class FakeAgent:
        def assess_delivery_mode(self, **kwargs):
            kwargs["audit_recorder"](
                {
                    "phase": "delivery_assessment",
                    "raw_output": f"PRIVATE OUTPUT FOR {kwargs['task_path']}",
                    "parsed": {"status": "parsed", "recommended_mode": "single_run"},
                }
            )
            return _draft()

    audit_paths: list[str] = []
    with (
        patch("sikula._create_delivery_preparation_agent", return_value=FakeAgent()),
        patch("sikula._prepare_project_context_from_config", return_value={}),
    ):
        for task_path in task_paths:
            result = sikula._run_delivery_assessment(
                args=_args(task_path),
                cfg=cfg,
                task_path=task_path.resolve(),
                project_root=tmp_path.resolve(),
            )
            audit_paths.append(result.audit_path)

    assert audit_paths[0] == ".sikula/contract-reports/feature.delivery-assess.auto-llm.jsonl"
    assert audit_paths[1] is not None
    assert audit_paths[1].startswith(".sikula/contract-reports/feature-")
    assert audit_paths[1].endswith(".delivery-assess.auto-llm.jsonl")
    assert audit_paths[0] != audit_paths[1]

    records = [
        json.loads((tmp_path / audit_path).read_text(encoding="utf-8").splitlines()[0]) for audit_path in audit_paths
    ]
    assert [record["task"]["path"] for record in records] == [
        ".sikula/tasks/a/feature.md",
        ".sikula/tasks/b/feature.md",
    ]


def test_delivery_assessment_uses_hashed_audit_when_base_is_not_utf8(tmp_path: Path) -> None:
    task_path = _write_task(tmp_path)
    report_dir = tmp_path / ".sikula" / "contract-reports"
    report_dir.mkdir(parents=True)
    base_audit = report_dir / "feature.delivery-assess.auto-llm.jsonl"
    base_audit.write_bytes(b"\xff\xfe")
    cfg = _cfg(tmp_path)
    cfg["_config_path"] = str(tmp_path / ".sikula" / "config.yaml")
    cfg["tasks"] = {"contract_report_dir": ".sikula/contract-reports"}

    audit_path = sikula._prepare_auto_preparation_audit_path(
        task_path.resolve(),
        cfg,
        generated_by="sikula.delivery_assess",
    )

    assert audit_path != base_audit
    assert audit_path.parent == report_dir
    assert audit_path.name.startswith("feature-")
    assert audit_path.name.endswith(".delivery-assess.auto-llm.jsonl")


def test_main_dispatches_delivery_assess_through_runtime_config(tmp_path: Path) -> None:
    task_path = _write_task(tmp_path)
    cfg = _cfg(tmp_path)
    argv = [
        "sikula",
        "delivery",
        "assess",
        str(task_path),
        "--json",
        "--agent-model",
        "delivery_preparer=gpt-5.5",
    ]

    with patch("sys.argv", argv):
        with patch("sikula._load_runtime_config", return_value=cfg) as load_config:
            with patch("sikula.cmd_delivery_assess") as assess:
                sikula.main()

    load_config.assert_called_once_with(None, required=True)
    assess.assert_called_once()
    args, called_cfg = assess.call_args.args
    assert called_cfg is cfg
    assert args.delivery_command == "assess"
    assert args.task_file == str(task_path)
    assert args.json is True
    assert args.agent_model == ["delivery_preparer=gpt-5.5"]
